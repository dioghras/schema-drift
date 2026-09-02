"""Schema drift detection — compares code-defined schema vs live DB schema."""

from dataclasses import dataclass

from schema_drift.connectors.base import LiveDBSnapshot
from schema_drift.schema_collectors.base import SchemaSnapshot


@dataclass
class DriftIssue:
    drift_type: str  # missing_table, extra_table, missing_column, extra_column,
    # type_mismatch, nullable_mismatch
    table_name: str
    column_name: str | None
    code_value: str | None
    db_value: str | None
    severity: str
    description: str


def detect_drift(
    code_schema: SchemaSnapshot,
    live_schema: LiveDBSnapshot,
) -> list[DriftIssue]:
    """Compare code-defined schema against live DB schema."""
    issues: list[DriftIssue] = []

    # Build lookup maps (case-insensitive)
    code_tables = {t.name.lower(): t for t in code_schema.tables}
    db_tables = {t.table_name.lower(): t for t in live_schema.tables}

    # Tables in code but not in DB
    for name, table in code_tables.items():
        if name not in db_tables:
            issues.append(
                DriftIssue(
                    drift_type="missing_table",
                    table_name=table.name,
                    column_name=None,
                    code_value=table.name,
                    db_value=None,
                    severity="high",
                    description=f"Table '{table.name}' defined in code but not found in database.",
                )
            )

    # Tables in DB but not in code
    skip_tables = {
        "__efmigrationshistory",
        "alembic_version",
        "django_migrations",
        "django_content_type",
        "django_admin_log",
        "django_session",
        "auth_permission",
        "auth_group",
        "auth_group_permissions",
        "auth_user",
        "auth_user_groups",
        "auth_user_user_permissions",
    }
    for name, table in db_tables.items():
        if name not in code_tables and name not in skip_tables:
            issues.append(
                DriftIssue(
                    drift_type="extra_table",
                    table_name=table.table_name,
                    column_name=None,
                    code_value=None,
                    db_value=table.table_name,
                    severity="medium",
                    description=f"Table '{table.table_name}' exists in database but not defined in code.",
                )
            )

    # Compare columns for matching tables
    for name in code_tables:
        if name not in db_tables:
            continue

        code_table = code_tables[name]
        db_table = db_tables[name]

        code_cols = {c.name.lower(): c for c in code_table.columns}
        db_cols = {c.name.lower(): c for c in db_table.columns}

        # Columns in code but not in DB
        for col_name, col in code_cols.items():
            if col_name not in db_cols:
                issues.append(
                    DriftIssue(
                        drift_type="missing_column",
                        table_name=code_table.name,
                        column_name=col.name,
                        code_value=col.data_type,
                        db_value=None,
                        severity="high",
                        description=f"Column '{col.name}' in table '{code_table.name}' defined in code but missing from database.",
                    )
                )

        # Columns in DB but not in code
        for col_name, col in db_cols.items():
            if col_name not in code_cols:
                issues.append(
                    DriftIssue(
                        drift_type="extra_column",
                        table_name=db_table.table_name,
                        column_name=col.name,
                        code_value=None,
                        db_value=col.data_type,
                        severity="low",
                        description=f"Column '{col.name}' in table '{db_table.table_name}' exists in database but not in code.",
                    )
                )

        # Compare matching columns
        for col_name in code_cols:
            if col_name not in db_cols:
                continue

            code_col = code_cols[col_name]
            db_col = db_cols[col_name]

            # Nullable mismatch
            if code_col.nullable != db_col.nullable:
                issues.append(
                    DriftIssue(
                        drift_type="nullable_mismatch",
                        table_name=code_table.name,
                        column_name=code_col.name,
                        code_value=f"nullable={code_col.nullable}",
                        db_value=f"nullable={db_col.nullable}",
                        severity="medium",
                        description=f"Column '{code_col.name}' in '{code_table.name}': nullable is {code_col.nullable} in code but {db_col.nullable} in database.",
                    )
                )

    return issues
