"""Drift detection — ORM schema snapshot vs live database snapshot."""

from schema_drift.connectors.base import (
    LiveColumnInfo,
    LiveDBSnapshot,
    LiveIndexInfo,
    LiveTableInfo,
    SlowQuery,
    BackupInfo,
)
from schema_drift.drift import DriftIssue, detect_drift
from schema_drift.schema_collectors.base import (
    ColumnDefinition,
    SchemaSnapshot,
    TableDefinition,
)


class TestDriftDetection:
    def _make_code_schema(self, tables):
        return SchemaSnapshot(tables=tables, orm_type="efcore")

    def _make_live_snapshot(self, tables):
        return LiveDBSnapshot(db_type="sqlserver", tables=tables)

    def test_no_drift_matching_schemas(self):
        code = self._make_code_schema([
            TableDefinition(name="Users", columns=[
                ColumnDefinition(name="Id", data_type="int", nullable=False, is_primary_key=True),
            ]),
        ])
        live = self._make_live_snapshot([
            LiveTableInfo(schema_name="dbo", table_name="Users", columns=[
                LiveColumnInfo(name="Id", data_type="int", nullable=False, is_primary_key=True),
            ]),
        ])
        issues = detect_drift(code, live)
        assert len(issues) == 0

    def test_missing_table_in_db(self):
        code = self._make_code_schema([
            TableDefinition(name="Users", columns=[
                ColumnDefinition(name="Id", data_type="int"),
            ]),
        ])
        live = self._make_live_snapshot([])
        issues = detect_drift(code, live)
        assert len(issues) == 1
        assert issues[0].drift_type == "missing_table"
        assert issues[0].severity == "high"

    def test_extra_table_in_db(self):
        code = self._make_code_schema([])
        live = self._make_live_snapshot([
            LiveTableInfo(schema_name="dbo", table_name="AuditLog", columns=[]),
        ])
        issues = detect_drift(code, live)
        assert len(issues) == 1
        assert issues[0].drift_type == "extra_table"
        assert issues[0].severity == "medium"

    def test_missing_column(self):
        code = self._make_code_schema([
            TableDefinition(name="Users", columns=[
                ColumnDefinition(name="Id", data_type="int"),
                ColumnDefinition(name="Email", data_type="string"),
            ]),
        ])
        live = self._make_live_snapshot([
            LiveTableInfo(schema_name="dbo", table_name="Users", columns=[
                LiveColumnInfo(name="Id", data_type="int", nullable=False),
            ]),
        ])
        issues = detect_drift(code, live)
        assert any(i.drift_type == "missing_column" and i.column_name == "Email" for i in issues)

    def test_nullable_mismatch(self):
        code = self._make_code_schema([
            TableDefinition(name="Users", columns=[
                ColumnDefinition(name="Email", data_type="string", nullable=False),
            ]),
        ])
        live = self._make_live_snapshot([
            LiveTableInfo(schema_name="dbo", table_name="Users", columns=[
                LiveColumnInfo(name="Email", data_type="varchar", nullable=True),
            ]),
        ])
        issues = detect_drift(code, live)
        assert any(i.drift_type == "nullable_mismatch" for i in issues)

    def test_skips_framework_tables(self):
        code = self._make_code_schema([])
        live = self._make_live_snapshot([
            LiveTableInfo(schema_name="dbo", table_name="__EFMigrationsHistory", columns=[]),
        ])
        issues = detect_drift(code, live)
        assert len(issues) == 0

    def test_empty_schemas(self):
        code = self._make_code_schema([])
        live = self._make_live_snapshot([])
        issues = detect_drift(code, live)
        assert len(issues) == 0
