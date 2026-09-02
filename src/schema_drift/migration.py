"""Generate reconciling SQL DDL from detected schema drift.

Deterministic, dialect-aware, **additive-only**: code→DB additions (CREATE TABLE,
ADD COLUMN, widen-nullable) are emitted as runnable statements; destructive ops
(DROP table/column, tighten NOT NULL) are emitted commented-out with warnings so a
reviewer can opt in. No API key, no network, no model call — the same drift always
produces the same SQL.
"""

import re
from dataclasses import dataclass, field

from schema_drift.drift import DriftIssue
from schema_drift.schema_collectors.base import (
    ColumnDefinition,
    SchemaSnapshot,
    TableDefinition,
)


@dataclass
class GeneratedMigration:
    dialect: str
    statements: list[str] = field(default_factory=list)  # safe, runnable
    warnings: list[str] = field(default_factory=list)
    sql: str = ""  # full script text (header + statements + commented destructive)
    native: str | None = None  # optional hook: a caller-rendered native ORM migration
    orm_type: str | None = None


# --- Type normalization ---------------------------------------------------

# Lowercased, length-stripped ORM/db type token → canonical bucket. Covers the
# vocabularies of the supported collectors (EF Core C#, SQLAlchemy, Django, Rails,
# Eloquent, GORM, Hibernate) and common DB type names.
_CANONICAL: dict[str, str] = {
    # integer
    "int": "integer", "integer": "integer", "int32": "integer", "smallint": "integer",
    "short": "integer", "integerfield": "integer", "tinyint": "integer", "serial": "integer",
    # big integer
    "long": "biginteger", "bigint": "biginteger", "biginteger": "biginteger",
    "int64": "biginteger", "bigserial": "biginteger", "bigintegerfield": "biginteger",
    # string
    "string": "string", "varchar": "string", "nvarchar": "string", "char": "string",
    "nchar": "string", "str": "string", "charfield": "string", "character varying": "string",
    "character": "string",
    # text
    "text": "text", "longtext": "text", "mediumtext": "text", "textfield": "text",
    "clob": "text", "ntext": "text",
    # boolean
    "bool": "boolean", "boolean": "boolean", "bit": "boolean", "booleanfield": "boolean",
    # float
    "float": "float", "double": "float", "real": "float", "double precision": "float",
    "floatfield": "float",
    # decimal
    "decimal": "decimal", "numeric": "decimal", "money": "decimal", "decimalfield": "decimal",
    # datetime
    "datetime": "datetime", "datetime2": "datetime", "timestamp": "datetime",
    "datetimeoffset": "datetime", "datetimefield": "datetime", "timestamptz": "datetime",
    # date / time
    "date": "date", "datefield": "date", "time": "time", "timefield": "time",
    # uuid
    "guid": "uuid", "uuid": "uuid", "uniqueidentifier": "uuid", "uuidfield": "uuid",
    # json
    "json": "json", "jsonb": "json", "jsonfield": "json",
    # binary
    "byte[]": "binary", "binary": "binary", "varbinary": "binary", "blob": "binary",
    "bytea": "binary", "binaryfield": "binary",
}

_DIALECT_TYPES: dict[str, dict[str, str]] = {
    "postgresql": {
        "integer": "INTEGER", "biginteger": "BIGINT", "string": "VARCHAR", "text": "TEXT",
        "boolean": "BOOLEAN", "float": "DOUBLE PRECISION", "decimal": "NUMERIC",
        "datetime": "TIMESTAMP", "date": "DATE", "time": "TIME", "uuid": "UUID",
        "json": "JSONB", "binary": "BYTEA",
    },
    "mysql": {
        "integer": "INT", "biginteger": "BIGINT", "string": "VARCHAR", "text": "TEXT",
        "boolean": "TINYINT(1)", "float": "DOUBLE", "decimal": "DECIMAL",
        "datetime": "DATETIME", "date": "DATE", "time": "TIME", "uuid": "CHAR(36)",
        "json": "JSON", "binary": "BLOB",
    },
    "sqlserver": {
        "integer": "INT", "biginteger": "BIGINT", "string": "NVARCHAR", "text": "NVARCHAR(MAX)",
        "boolean": "BIT", "float": "FLOAT", "decimal": "DECIMAL",
        "datetime": "DATETIME2", "date": "DATE", "time": "TIME", "uuid": "UNIQUEIDENTIFIER",
        "json": "NVARCHAR(MAX)", "binary": "VARBINARY(MAX)",
    },
}

_VARIABLE_LENGTH = {"string"}  # types that take an (n) length


def _normalize_dialect(db_type: str | None) -> str:
    dt = (db_type or "").lower()
    if dt in ("postgres", "postgresql", "pgsql"):
        return "postgresql"
    if dt in ("mysql", "mariadb"):
        return "mysql"
    if dt in ("sqlserver", "mssql", "sql_server"):
        return "sqlserver"
    return "postgresql"  # safe default


def _canonical_type(data_type: str | None) -> str | None:
    if not data_type:
        return None
    token = data_type.strip().rstrip("?").lower()
    token = re.sub(r"\(.*?\)", "", token).strip()  # strip length/precision
    return _CANONICAL.get(token)


def _sql_type(data_type: str | None, dialect: str, max_length: int | None) -> tuple[str, bool]:
    """Return (sql_type, recognized). Unknown types pass through unchanged."""
    canon = _canonical_type(data_type)
    types = _DIALECT_TYPES[dialect]
    if canon is None:
        return (data_type or "TEXT"), False
    base = types[canon]
    if canon in _VARIABLE_LENGTH:
        return f"{base}({max_length or 255})", True
    return base, True


def _quote(identifier: str, dialect: str) -> str:
    if dialect == "mysql":
        return f"`{identifier}`"
    if dialect == "sqlserver":
        return f"[{identifier}]"
    return f'"{identifier}"'


# --- Statement builders ---------------------------------------------------


def _column_clause(col: ColumnDefinition, dialect: str, warnings: list[str], table: str) -> str:
    sql_type, recognized = _sql_type(col.data_type, dialect, col.max_length)
    if not recognized:
        warnings.append(
            f"Could not map type '{col.data_type}' for {table}.{col.name} — "
            f"emitted as-is; verify against the target dialect."
        )
    null = "NOT NULL" if not col.nullable else "NULL"
    return f"{_quote(col.name, dialect)} {sql_type} {null}".rstrip()


def _create_table(table: TableDefinition, dialect: str, warnings: list[str]) -> str | None:
    if not table.columns:
        warnings.append(
            f"Table '{table.name}' is missing from the database but has no parsed columns "
            f"in code — skipped CREATE TABLE."
        )
        return None
    col_lines = [
        "    " + _column_clause(c, dialect, warnings, table.name) for c in table.columns
    ]
    pk_cols = [c.name for c in table.columns if c.is_primary_key]
    if pk_cols:
        quoted = ", ".join(_quote(c, dialect) for c in pk_cols)
        col_lines.append(f"    PRIMARY KEY ({quoted})")
    body = ",\n".join(col_lines)
    return f"CREATE TABLE {_quote(table.name, dialect)} (\n{body}\n);"


def _add_column(table: str, col: ColumnDefinition, dialect: str, warnings: list[str]) -> str:
    clause = _column_clause(col, dialect, warnings, table)
    keyword = "ADD" if dialect == "sqlserver" else "ADD COLUMN"
    if not col.nullable:
        warnings.append(
            f"Adding NOT NULL column {table}.{col.name} will fail on a populated table — "
            f"add a DEFAULT or backfill first."
        )
    return f"ALTER TABLE {_quote(table, dialect)} {keyword} {clause};"


def _drop_not_null(table: str, col: ColumnDefinition, dialect: str) -> str:
    t, c = _quote(table, dialect), _quote(col.name, dialect)
    if dialect == "postgresql":
        return f"ALTER TABLE {t} ALTER COLUMN {c} DROP NOT NULL;"
    sql_type, _ = _sql_type(col.data_type, dialect, col.max_length)
    if dialect == "mysql":
        return f"ALTER TABLE {t} MODIFY COLUMN {c} {sql_type} NULL;"
    return f"ALTER TABLE {t} ALTER COLUMN {c} {sql_type} NULL;"  # sqlserver


def _find_column(table: TableDefinition | None, column_name: str | None) -> ColumnDefinition | None:
    if not table or not column_name:
        return None
    for c in table.columns:
        if c.name.lower() == column_name.lower():
            return c
    return None


# --- Entry point ----------------------------------------------------------


def generate_migration(
    drift_issues: list[DriftIssue],
    code_schema: SchemaSnapshot,
    db_type: str | None,
) -> GeneratedMigration:
    """Build an additive-only reconciling migration from drift issues."""
    dialect = _normalize_dialect(db_type)
    mig = GeneratedMigration(dialect=dialect, orm_type=code_schema.orm_type or None)
    code_tables = {t.name.lower(): t for t in code_schema.tables}

    destructive: list[str] = []  # commented-out lines

    for issue in drift_issues:
        table = code_tables.get(issue.table_name.lower())

        if issue.drift_type == "missing_table":
            if table is None:
                mig.warnings.append(
                    f"Table '{issue.table_name}' is flagged missing from the database but was "
                    f"not found in parsed code — skipped CREATE TABLE."
                )
                continue
            stmt = _create_table(table, dialect, mig.warnings)
            if stmt:
                mig.statements.append(stmt)

        elif issue.drift_type == "missing_column":
            col = _find_column(table, issue.column_name)
            if col is None:
                mig.warnings.append(
                    f"Missing column {issue.table_name}.{issue.column_name} not found in parsed "
                    f"code — skipped."
                )
                continue
            mig.statements.append(_add_column(issue.table_name, col, dialect, mig.warnings))

        elif issue.drift_type == "nullable_mismatch":
            col = _find_column(table, issue.column_name)
            code_nullable = "nullable=True" in (issue.code_value or "")
            if col is None:
                continue
            if code_nullable:
                # Widening (db NOT NULL → NULL): safe, additive.
                mig.statements.append(_drop_not_null(issue.table_name, col, dialect))
            else:
                # Tightening (db NULL → NOT NULL): risky, leave commented.
                destructive.append(
                    f"-- ALTER TABLE {_quote(issue.table_name, dialect)} "
                    f"ALTER COLUMN {_quote(col.name, dialect)} SET NOT NULL;  "
                    f"-- review: may fail if NULLs exist"
                )
                mig.warnings.append(
                    f"Column {issue.table_name}.{col.name} is NOT NULL in code but nullable in "
                    f"DB — tightening left commented out (backfill NULLs first)."
                )

        elif issue.drift_type == "extra_table":
            destructive.append(
                f"-- DROP TABLE {_quote(issue.table_name, dialect)};  -- destructive: in DB, not in code"
            )
            mig.warnings.append(
                f"Table '{issue.table_name}' exists in the DB but not in code — DROP left "
                f"commented out for review."
            )

        elif issue.drift_type == "extra_column":
            keyword = "DROP COLUMN"
            destructive.append(
                f"-- ALTER TABLE {_quote(issue.table_name, dialect)} {keyword} "
                f"{_quote(issue.column_name, dialect)};  -- destructive: in DB, not in code"
            )
            mig.warnings.append(
                f"Column {issue.table_name}.{issue.column_name} exists in the DB but not in code "
                f"— DROP left commented out for review."
            )

    mig.sql = _render_sql(mig, destructive)
    return mig


def _render_sql(mig: GeneratedMigration, destructive: list[str]) -> str:
    lines = [
        f"-- Generated by schema-drift — schema drift reconciliation ({mig.dialect})",
        "-- Additive-only. Review before running against any database.",
        "",
    ]
    if mig.statements:
        lines.extend(mig.statements)
    else:
        lines.append("-- No additive changes required.")
    if destructive:
        lines.extend([
            "",
            "-- ---------------------------------------------------------------------------",
            "-- Destructive changes below are COMMENTED OUT. Review carefully before enabling.",
            "-- ---------------------------------------------------------------------------",
        ])
        lines.extend(destructive)
    return "\n".join(lines)
