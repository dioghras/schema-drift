"""GORM schema collector — parses Go struct gorm tags."""

import re
from pathlib import Path

from schema_drift.schema_collectors.base import (
    BaseSchemaCollector,
    ColumnDefinition,
    MigrationInfo,
    SchemaSnapshot,
    TableDefinition,
)

RE_STRUCT = re.compile(r"type\s+(\w+)\s+struct\s*\{")
RE_FIELD = re.compile(r"\s+(\w+)\s+([\w.*\[\]]+)\s*`([^`]*)`")
RE_GORM_TAG = re.compile(r'gorm:"([^"]*)"')
RE_TABLE_NAME_FUNC = re.compile(r'func\s*\(\w*\s*\*?(\w+)\)\s*TableName\s*\(\)\s*string\s*\{\s*return\s*"(\w+)"')


class GORMSchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "gorm"

    def entity_file_patterns(self) -> list[str]:
        return ["**/*.go"]

    def migration_file_patterns(self) -> list[str]:
        return ["**/migrations/*.go", "**/migrate/*.go", "**/migrations/*.sql"]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())
        root = Path(project_path)

        entity_files = self._find_files(project_path, self.entity_file_patterns())

        # First pass: collect TableName overrides
        table_names: dict[str, str] = {}
        all_contents: dict[Path, str] = {}
        for path in entity_files:
            content = self._read_file(path)
            if not content:
                continue
            all_contents[path] = content
            for m in RE_TABLE_NAME_FUNC.finditer(content):
                table_names[m.group(1)] = m.group(2)

        # Second pass: parse structs with gorm tags
        for path, content in all_contents.items():
            tables = self._parse_file(content, str(path.relative_to(root)), table_names)
            for table in tables:
                snapshot.tables.append(table)
                snapshot.raw_files_parsed += 1

        migration_files = self._find_files(project_path, self.migration_file_patterns())
        for path in migration_files:
            rel = str(path.relative_to(root))
            snapshot.migrations.append(
                MigrationInfo(migration_id=path.stem, file_path=rel, operations=["migration file"])
            )

        return snapshot

    def _parse_file(self, content: str, file_path: str, table_names: dict[str, str]) -> list[TableDefinition]:
        tables = []

        for struct_match in RE_STRUCT.finditer(content):
            struct_name = struct_match.group(1)
            start = struct_match.end()
            # Find the closing brace
            brace_count = 1
            pos = start
            while pos < len(content) and brace_count > 0:
                if content[pos] == "{":
                    brace_count += 1
                elif content[pos] == "}":
                    brace_count -= 1
                pos += 1

            struct_body = content[start:pos]

            # Check if any field has gorm tags
            if "gorm:" not in struct_body:
                continue

            table_name = table_names.get(struct_name, self._to_snake_plural(struct_name))
            columns = []

            for field_match in RE_FIELD.finditer(struct_body):
                field_name = field_match.group(1)
                field_type = field_match.group(2)
                tags = field_match.group(3)

                gorm_match = RE_GORM_TAG.search(tags)
                if not gorm_match:
                    continue

                gorm_tag = gorm_match.group(1)
                tag_parts = {p.split(":")[0].strip(): p.split(":", 1)[1].strip() if ":" in p else ""
                             for p in gorm_tag.split(";")}

                is_pk = "primaryKey" in tag_parts or "primarykey" in tag_parts
                col_name = tag_parts.get("column", self._to_snake(field_name))
                nullable = "not null" not in gorm_tag.lower()

                columns.append(
                    ColumnDefinition(
                        name=col_name,
                        data_type=field_type,
                        nullable=nullable,
                        is_primary_key=is_pk,
                        is_foreign_key=field_name.endswith("ID") and field_name != "ID",
                    )
                )

            if columns:
                tables.append(TableDefinition(name=table_name, columns=columns, source_file=file_path))

        return tables

    def _to_snake(self, name: str) -> str:
        # Handle consecutive uppercase (e.g., "ID" → "id", "RoleID" → "role_id")
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
        return s.lower()

    def _to_snake_plural(self, name: str) -> str:
        snake = self._to_snake(name)
        if snake.endswith("s"):
            return snake
        return snake + "s"

    def _read_file(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
