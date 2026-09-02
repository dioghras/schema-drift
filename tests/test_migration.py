"""Tests for additive-only schema-drift migration generation."""

from schema_drift.drift import DriftIssue
from schema_drift.migration import (
    _canonical_type,
    _sql_type,
    generate_migration,
)
from schema_drift.schema_collectors.base import (
    ColumnDefinition,
    SchemaSnapshot,
    TableDefinition,
)


def _schema(*tables, orm_type="sqlalchemy"):
    return SchemaSnapshot(tables=list(tables), orm_type=orm_type)


def _users_table():
    return TableDefinition(
        name="users",
        columns=[
            ColumnDefinition(name="id", data_type="Integer", nullable=False, is_primary_key=True),
            ColumnDefinition(name="email", data_type="String", nullable=False, max_length=255),
            ColumnDefinition(name="bio", data_type="Text", nullable=True),
        ],
    )


# --- Type normalization ---------------------------------------------------


class TestTypeNormalization:
    def test_canonical_across_orms(self):
        assert _canonical_type("int") == "integer"
        assert _canonical_type("Integer") == "integer"
        assert _canonical_type("string") == "string"
        assert _canonical_type("nvarchar") == "string"
        assert _canonical_type("String(50)") == "string"
        assert _canonical_type("DateTime?") == "datetime"  # EF Core nullable marker
        assert _canonical_type("Guid") == "uuid"
        assert _canonical_type("byte[]") == "binary"

    def test_unknown_returns_none(self):
        assert _canonical_type("WeirdCustomType") is None
        assert _canonical_type(None) is None

    def test_sql_type_per_dialect(self):
        assert _sql_type("Integer", "postgresql", None) == ("INTEGER", True)
        assert _sql_type("Integer", "mysql", None) == ("INT", True)
        assert _sql_type("Boolean", "sqlserver", None) == ("BIT", True)
        assert _sql_type("String", "postgresql", 100) == ("VARCHAR(100)", True)
        assert _sql_type("String", "postgresql", None) == ("VARCHAR(255)", True)

    def test_unknown_type_passes_through(self):
        sql, recognized = _sql_type("CustomThing", "postgresql", None)
        assert sql == "CustomThing"
        assert recognized is False


# --- CREATE TABLE (missing_table) -----------------------------------------


class TestMissingTable:
    def test_creates_table_with_pk(self):
        issue = DriftIssue("missing_table", "users", None, "users", None, "high", "")
        mig = generate_migration([issue], _schema(_users_table()), "postgresql")

        assert len(mig.statements) == 1
        sql = mig.statements[0]
        assert sql.startswith('CREATE TABLE "users"')
        assert '"email" VARCHAR(255) NOT NULL' in sql
        assert '"bio" TEXT NULL' in sql
        assert 'PRIMARY KEY ("id")' in sql

    def test_missing_table_without_columns_warns(self):
        issue = DriftIssue("missing_table", "ghost", None, "ghost", None, "high", "")
        # table not present in code schema → no columns
        mig = generate_migration([issue], _schema(), "postgresql")
        assert mig.statements == []
        assert any("ghost" in w for w in mig.warnings)


# --- ADD COLUMN (missing_column) ------------------------------------------


class TestMissingColumn:
    def test_add_column(self):
        issue = DriftIssue(
            "missing_column", "users", "bio", "Text", None, "high", ""
        )
        mig = generate_migration([issue], _schema(_users_table()), "postgresql")
        assert mig.statements == ['ALTER TABLE "users" ADD COLUMN "bio" TEXT NULL;']

    def test_add_not_null_column_warns(self):
        issue = DriftIssue(
            "missing_column", "users", "email", "String", None, "high", ""
        )
        mig = generate_migration([issue], _schema(_users_table()), "postgresql")
        assert 'ADD COLUMN "email" VARCHAR(255) NOT NULL;' in mig.statements[0]
        assert any("NOT NULL" in w for w in mig.warnings)

    def test_sqlserver_uses_add_keyword(self):
        issue = DriftIssue("missing_column", "users", "bio", "Text", None, "high", "")
        mig = generate_migration([issue], _schema(_users_table()), "sqlserver")
        assert mig.statements[0].startswith("ALTER TABLE [users] ADD [bio]")
        assert "ADD COLUMN" not in mig.statements[0]


# --- nullable_mismatch ----------------------------------------------------


class TestNullableMismatch:
    def test_widening_is_safe(self):
        # code nullable=True, db nullable=False → DROP NOT NULL (additive/safe)
        table = TableDefinition(
            name="users",
            columns=[ColumnDefinition(name="bio", data_type="Text", nullable=True)],
        )
        issue = DriftIssue(
            "nullable_mismatch", "users", "bio", "nullable=True", "nullable=False", "medium", ""
        )
        mig = generate_migration([issue], _schema(table), "postgresql")
        assert mig.statements == ['ALTER TABLE "users" ALTER COLUMN "bio" DROP NOT NULL;']

    def test_tightening_is_commented_out(self):
        # code nullable=False, db nullable=True → risky, commented + warned
        table = TableDefinition(
            name="users",
            columns=[ColumnDefinition(name="bio", data_type="Text", nullable=False)],
        )
        issue = DriftIssue(
            "nullable_mismatch", "users", "bio", "nullable=False", "nullable=True", "medium", ""
        )
        mig = generate_migration([issue], _schema(table), "postgresql")
        assert mig.statements == []
        assert "-- ALTER TABLE" in mig.sql
        assert "SET NOT NULL" in mig.sql
        assert any("NOT NULL" in w for w in mig.warnings)


# --- destructive (extra_*) ------------------------------------------------


class TestDestructiveCommentedOut:
    def test_extra_table_commented(self):
        issue = DriftIssue("extra_table", "legacy", None, None, "legacy", "medium", "")
        mig = generate_migration([issue], _schema(), "postgresql")
        assert mig.statements == []
        assert "-- DROP TABLE" in mig.sql
        assert "destructive" in mig.sql
        assert mig.warnings

    def test_extra_column_commented(self):
        issue = DriftIssue("extra_column", "users", "obsolete", None, "varchar", "low", "")
        mig = generate_migration([issue], _schema(_users_table()), "postgresql")
        assert mig.statements == []
        assert '-- ALTER TABLE "users" DROP COLUMN "obsolete";' in mig.sql


# --- end-to-end render ----------------------------------------------------


class TestRender:
    def test_sql_has_header_and_dialect(self):
        issue = DriftIssue("missing_column", "users", "bio", "Text", None, "high", "")
        mig = generate_migration([issue], _schema(_users_table()), "mysql")
        assert mig.dialect == "mysql"
        assert "schema-drift" in mig.sql
        assert "mysql" in mig.sql
        assert "`users`" in mig.statements[0]

    def test_no_changes_message(self):
        mig = generate_migration([], _schema(_users_table()), "postgresql")
        assert "No additive changes required." in mig.sql

    def test_dialect_aliases(self):
        issue = DriftIssue("missing_column", "users", "bio", "Text", None, "high", "")
        mig = generate_migration([issue], _schema(_users_table()), "mssql")
        assert mig.dialect == "sqlserver"
