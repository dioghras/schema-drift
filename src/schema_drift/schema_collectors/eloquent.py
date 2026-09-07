"""Eloquent (Laravel) schema collector — parses PHP migration files."""

import re
from pathlib import Path

from schema_drift.schema_collectors.base import (
    BaseSchemaCollector,
    ColumnDefinition,
    MigrationInfo,
    SchemaSnapshot,
    TableDefinition,
)

RE_SCHEMA_CREATE = re.compile(
    r"""Schema::create\s*\(\s*['"](\w+)['"]\s*,\s*function\s*\(.*?\)\s*\{(.*?)\}\s*\)""",
    re.DOTALL,
)
RE_COLUMN = re.compile(
    r"""\$table->(\w+)\s*\(\s*['"](\w+)['"](?:\s*,\s*(\d+))?\s*\)"""
)
RE_NULLABLE = re.compile(r"->nullable\s*\(\s*\)")
RE_MIGRATION_CLASS = re.compile(r"class\s+(\w+)\s+extends\s+Migration")
RE_DROP_TABLE = re.compile(r"""Schema::drop(?:IfExists)?\s*\(\s*['"](\w+)['"]""")

PK_TYPES = {"id", "bigIncrements", "increments", "uuid"}
FK_TYPES = {"foreignId", "foreignIdFor", "unsignedBigInteger"}

# Laravel's most common declarations take no column name at all, so the
# named-column pattern above skips every one of them — including $table->id(),
# the primary key of essentially every Laravel table. Each entry maps the
# helper to the columns it actually creates: (name, type, nullable).
RE_BARE_HELPER = re.compile(r"\$table->(\w+)\s*\(\s*\)")
BARE_HELPERS: dict[str, list[tuple[str, str, bool]]] = {
    "id": [("id", "bigIncrements", False)],
    "uuid": [("uuid", "uuid", False)],
    "increments": [("id", "increments", False)],
    "bigIncrements": [("id", "bigIncrements", False)],
    # timestamps() and softDeletes() are nullable in Laravel.
    "timestamps": [
        ("created_at", "timestamp", True),
        ("updated_at", "timestamp", True),
    ],
    "timestampsTz": [
        ("created_at", "timestampTz", True),
        ("updated_at", "timestampTz", True),
    ],
    "softDeletes": [("deleted_at", "timestamp", True)],
    "softDeletesTz": [("deleted_at", "timestampTz", True)],
    "rememberToken": [("remember_token", "string", True)],
}


class EloquentSchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "eloquent"

    def entity_file_patterns(self) -> list[str]:
        return ["**/database/migrations/*.php"]

    def migration_file_patterns(self) -> list[str]:
        return ["**/database/migrations/*.php"]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())
        root = Path(project_path)

        migration_files = self._find_files(project_path, self.migration_file_patterns())
        for path in sorted(migration_files):
            content = self._read_file(path)
            if not content:
                continue
            rel = str(path.relative_to(root))

            # Extract tables
            tables = self._parse_tables(content, rel)
            snapshot.tables.extend(tables)

            # Extract migration info
            migration = self._parse_migration(content, rel)
            if migration:
                snapshot.migrations.append(migration)

            if tables or migration:
                snapshot.raw_files_parsed += 1

        return snapshot

    def _parse_tables(self, content: str, file_path: str) -> list[TableDefinition]:
        tables = []

        for table_match in RE_SCHEMA_CREATE.finditer(content):
            table_name = table_match.group(1)
            body = table_match.group(2)
            columns = []

            for col_match in RE_COLUMN.finditer(body):
                col_type = col_match.group(1)
                col_name = col_match.group(2)
                col_length = col_match.group(3)

                # Get the full chain after this column definition
                col_start = col_match.end()
                line_end = body.find(";", col_start)
                chain = body[col_start:line_end] if line_end > col_start else ""

                is_pk = col_type in PK_TYPES
                is_fk = col_type in FK_TYPES or col_name.endswith("_id")
                nullable = bool(RE_NULLABLE.search(chain))

                columns.append(
                    ColumnDefinition(
                        name=col_name,
                        data_type=col_type,
                        nullable=nullable,
                        is_primary_key=is_pk,
                        is_foreign_key=is_fk,
                        max_length=int(col_length) if col_length else None,
                    )
                )

            # Bare helpers, added after the named columns so an explicit
            # declaration of the same name always wins.
            seen = {c.name for c in columns}
            for helper_match in RE_BARE_HELPER.finditer(body):
                for name, col_type, nullable in BARE_HELPERS.get(
                    helper_match.group(1), []
                ):
                    if name in seen:
                        continue
                    seen.add(name)
                    columns.append(
                        ColumnDefinition(
                            name=name,
                            data_type=col_type,
                            nullable=nullable,
                            is_primary_key=col_type
                            in ("id", "bigIncrements", "increments", "uuid"),
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

        for m in RE_SCHEMA_CREATE.finditer(content):
            operations.append(f"CreateTable {m.group(1)}")
        for m in RE_DROP_TABLE.finditer(content):
            operations.append(f"DropTable {m.group(1)}")

        if not operations:
            return None

        timestamp = None
        ts_match = re.match(r".*?(\d{4}_\d{2}_\d{2}_\d{6})", file_path)
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
