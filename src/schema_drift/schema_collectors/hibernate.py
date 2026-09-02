"""Hibernate/JPA schema collector — parses Java @Entity classes."""

import re
from pathlib import Path

from schema_drift.schema_collectors.base import (
    BaseSchemaCollector,
    ColumnDefinition,
    MigrationInfo,
    SchemaSnapshot,
    TableDefinition,
)

RE_ENTITY = re.compile(r"@Entity")
RE_TABLE = re.compile(r'@Table\s*\(\s*name\s*=\s*"(\w+)"')
RE_CLASS = re.compile(r"(?:public\s+)?class\s+(\w+)")
RE_COLUMN = re.compile(
    r'@Column\s*\(([^)]*)\)\s*(?:private|protected|public)\s+(\w[\w<>?]*)\s+(\w+)'
)
RE_FIELD = re.compile(r"(?:private|protected|public)\s+(\w[\w<>?]*)\s+(\w+)\s*;")
RE_ID = re.compile(r"@Id")
RE_GENERATED = re.compile(r"@GeneratedValue")
RE_JOIN_COLUMN = re.compile(r'@JoinColumn\s*\(\s*name\s*=\s*"(\w+)"')
RE_NULLABLE = re.compile(r"nullable\s*=\s*(true|false)")
RE_LENGTH = re.compile(r"length\s*=\s*(\d+)")


class HibernateSchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "hibernate"

    def entity_file_patterns(self) -> list[str]:
        return ["**/*.java", "**/*.kt"]

    def migration_file_patterns(self) -> list[str]:
        return [
            "**/db/migration/*.sql",
            "**/db/migration/*.java",
            "**/flyway/*.sql",
            "**/liquibase/*.xml",
            "**/liquibase/*.sql",
        ]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())
        root = Path(project_path)

        entity_files = self._find_files(project_path, self.entity_file_patterns())
        for path in entity_files:
            content = self._read_file(path)
            if not content or not RE_ENTITY.search(content):
                continue

            table = self._parse_entity(content, str(path.relative_to(root)))
            if table:
                snapshot.tables.append(table)
                snapshot.raw_files_parsed += 1

        migration_files = self._find_files(project_path, self.migration_file_patterns())
        for path in migration_files:
            rel = str(path.relative_to(root))
            migration = MigrationInfo(
                migration_id=path.stem,
                file_path=rel,
                operations=["migration file"],
            )
            snapshot.migrations.append(migration)
            snapshot.raw_files_parsed += 1

        return snapshot

    def _parse_entity(self, content: str, file_path: str) -> TableDefinition | None:
        class_match = RE_CLASS.search(content)
        if not class_match:
            return None

        class_name = class_match.group(1)

        table_match = RE_TABLE.search(content)
        table_name = table_match.group(1) if table_match else class_name.lower() + "s"

        columns = []
        lines = content.split("\n")
        for i, line in enumerate(lines):
            field_match = RE_FIELD.search(line)
            if not field_match:
                continue

            field_type, field_name = field_match.group(1), field_match.group(2)

            # Skip collection fields
            if field_type.startswith(("List", "Set", "Collection")):
                continue

            preceding = "\n".join(lines[max(0, i - 5):i + 1])

            is_pk = bool(RE_ID.search(preceding))
            join_match = RE_JOIN_COLUMN.search(preceding)
            is_fk = bool(join_match)

            nullable = True
            null_match = RE_NULLABLE.search(preceding)
            if null_match:
                nullable = null_match.group(1) == "true"

            max_length = None
            len_match = RE_LENGTH.search(preceding)
            if len_match:
                max_length = int(len_match.group(1))

            columns.append(
                ColumnDefinition(
                    name=field_name,
                    data_type=field_type,
                    nullable=nullable,
                    is_primary_key=is_pk,
                    is_foreign_key=is_fk,
                    max_length=max_length,
                )
            )

        if not columns:
            return None

        return TableDefinition(name=table_name, columns=columns, source_file=file_path)

    def _read_file(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
