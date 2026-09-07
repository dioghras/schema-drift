"""ActiveRecord schema collector — parses db/schema.rb and migrations."""

import re
from pathlib import Path

from schema_drift.schema_collectors.base import (
    BaseSchemaCollector,
    ColumnDefinition,
    MigrationInfo,
    SchemaSnapshot,
    TableDefinition,
)

# Group 2 is the options between the table name and the block — "id: false",
# "primary_key: \"uuid\"" and friends live there and decide whether Rails adds
# a key column of its own.
RE_CREATE_TABLE = re.compile(
    r'create_table\s+"(\w+)"([^\n]*?)do\s*\|t\|(.*?)end', re.DOTALL
)
RE_ID_FALSE = re.compile(r"\bid:\s*false\b")
RE_PRIMARY_KEY_OPT = re.compile(r'\bprimary_key:\s*"(\w+)"')
# t.timestamps takes no column name, so the t.<type> "name" pattern skips it
# entirely — and with it the two columns Rails actually creates.
RE_TIMESTAMPS = re.compile(r"^\s*t\.timestamps\b(.*)$", re.MULTILINE)
RE_COLUMN = re.compile(r't\.(\w+)\s+"(\w+)"(?:\s*,\s*(.+?))?$', re.MULTILINE)
RE_INDEX = re.compile(r'add_index\s+"(\w+)"\s*,\s*\[([^\]]+)\]')
RE_MIGRATION_CLASS = re.compile(r"class\s+(\w+)\s*<\s*ActiveRecord::Migration")
RE_ADD_COLUMN = re.compile(r'add_column\s+:(\w+)\s*,\s*:(\w+)\s*,\s*:(\w+)')
RE_REMOVE_COLUMN = re.compile(r'remove_column\s+:(\w+)\s*,\s*:(\w+)')


class ActiveRecordSchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "activerecord"

    def entity_file_patterns(self) -> list[str]:
        return ["**/db/schema.rb", "**/app/models/**/*.rb"]

    def migration_file_patterns(self) -> list[str]:
        return ["**/db/migrate/*.rb"]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())
        root = Path(project_path)

        # Parse schema.rb first (authoritative)
        schema_files = self._find_files(project_path, ["**/db/schema.rb"])
        for path in schema_files:
            content = self._read_file(path)
            if not content:
                continue
            tables = self._parse_schema_rb(content, str(path.relative_to(root)))
            snapshot.tables.extend(tables)
            snapshot.raw_files_parsed += 1

        # Parse migrations
        migration_files = self._find_files(project_path, self.migration_file_patterns())
        for path in sorted(migration_files):
            content = self._read_file(path)
            if not content:
                continue
            rel = str(path.relative_to(root))
            migration = self._parse_migration(content, rel)
            if migration:
                snapshot.migrations.append(migration)
                snapshot.raw_files_parsed += 1

        return snapshot

    def _parse_schema_rb(self, content: str, file_path: str) -> list[TableDefinition]:
        tables = []

        for table_match in RE_CREATE_TABLE.finditer(content):
            table_name = table_match.group(1)
            options = table_match.group(2) or ""
            body = table_match.group(3)
            columns = []

            # Rails creates a primary key unless told not to. It never appears
            # as a t.<type> line, so reading only those lines produced a table
            # with no key — and a CREATE TABLE without one, for a new database.
            if not RE_ID_FALSE.search(options):
                pk_match = RE_PRIMARY_KEY_OPT.search(options)
                columns.append(
                    ColumnDefinition(
                        name=pk_match.group(1) if pk_match else "id",
                        data_type="bigint",
                        nullable=False,
                        is_primary_key=True,
                    )
                )

            for col_match in RE_COLUMN.finditer(body):
                col_type = col_match.group(1)
                col_name = col_match.group(2)
                options = col_match.group(3) or ""

                # Skip index definitions within create_table
                if col_type == "index":
                    continue

                nullable = "null: false" not in options
                max_length = None
                limit_match = re.search(r"limit:\s*(\d+)", options)
                if limit_match:
                    max_length = int(limit_match.group(1))

                is_fk = col_name.endswith("_id")

                columns.append(
                    ColumnDefinition(
                        name=col_name,
                        data_type=col_type,
                        nullable=nullable,
                        # The key column is synthesised above from the
                        # create_table options, not matched here.
                        is_primary_key=False,
                        is_foreign_key=is_fk,
                        max_length=max_length,
                    )
                )

            # t.timestamps expands to created_at/updated_at, NOT NULL since
            # Rails 5. Both are real columns in every database Rails builds.
            ts_match = RE_TIMESTAMPS.search(body)
            if ts_match:
                # NOT NULL is the default since Rails 5; only an explicit
                # null: true relaxes it.
                ts_nullable = "null: true" in ts_match.group(1)
                for name in ("created_at", "updated_at"):
                    columns.append(
                        ColumnDefinition(
                            name=name,
                            data_type="datetime",
                            nullable=ts_nullable,
                        )
                    )

            if columns:
                tables.append(TableDefinition(name=table_name, columns=columns, source_file=file_path))

        return tables

    def _parse_migration(self, content: str, file_path: str) -> MigrationInfo | None:
        class_match = RE_MIGRATION_CLASS.search(content)
        if not class_match:
            return None

        migration_id = class_match.group(1)
        operations = []

        for m in RE_CREATE_TABLE.finditer(content):
            operations.append(f"CreateTable {m.group(1)}")
        for m in RE_ADD_COLUMN.finditer(content):
            operations.append(f"AddColumn {m.group(1)}.{m.group(2)}")
        for m in RE_REMOVE_COLUMN.finditer(content):
            operations.append(f"RemoveColumn {m.group(1)}.{m.group(2)}")

        if not operations:
            return None

        # Extract timestamp from filename
        timestamp = None
        ts_match = re.match(r".*?(\d{14})", file_path)
        if ts_match:
            timestamp = ts_match.group(1)

        return MigrationInfo(
            migration_id=migration_id,
            file_path=file_path,
            timestamp=timestamp,
            operations=operations,
        )

    def _read_file(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
