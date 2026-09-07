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
RE_ENTITY_CLASS = re.compile(r"@Entity\b")
RE_COLUMN_NAME = re.compile(r'@Column\s*\([^)]*name\s*=\s*"(\w+)"')
RE_TRANSIENT = re.compile(r"@Transient\b")
RE_TO_ONE = re.compile(r"@(?:ManyToOne|OneToOne)\b")

COLLECTION_PREFIXES = ("List", "Set", "Collection", "Map", "SortedSet")


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

            tables = self._parse_entity(content, str(path.relative_to(root)))
            if tables:
                snapshot.tables.extend(tables)
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

    @staticmethod
    def _snake(name: str) -> str:
        """Spring Boot's CamelCaseToUnderscoresNamingStrategy."""
        out = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        out = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", out)
        return out.lower()

    @staticmethod
    def _entity_blocks(content: str) -> list[tuple[str, str, str]]:
        """Yield (class_name, annotations_before, body) for each @Entity class.

        Java files usually hold one public class, but @Entity on a second
        class in the same file was silently dropped: the old parser called
        RE_CLASS.search once and took whatever came first.
        """
        blocks: list[tuple[str, str, str]] = []
        for match in RE_CLASS.finditer(content):
            open_idx = content.find("{", match.end())
            if open_idx == -1:
                continue
            depth, close_idx = 0, -1
            for i in range(open_idx, len(content)):
                if content[i] == "{":
                    depth += 1
                elif content[i] == "}":
                    depth -= 1
                    if depth == 0:
                        close_idx = i
                        break
            if close_idx == -1:
                continue
            preceding = content[: match.start()]
            annotations = preceding[preceding.rfind("}") + 1 :] if "}" in preceding else preceding
            if not RE_ENTITY_CLASS.search(annotations):
                continue
            blocks.append((match.group(1), annotations, content[open_idx + 1 : close_idx]))
        return blocks

    def _parse_entity(self, content: str, file_path: str) -> list[TableDefinition]:
        tables: list[TableDefinition] = []

        for class_name, annotations, body in self._entity_blocks(content):
            table_match = RE_TABLE.search(annotations)
            # Hibernate does not pluralize. The old default lowercased the
            # class and appended "s", so AuditRecord became "auditrecords" —
            # a table no schema has, reported as missing on every run.
            table_name = table_match.group(1) if table_match else self._snake(class_name)

            fields = list(RE_FIELD.finditer(body))
            columns: list[ColumnDefinition] = []

            for i, field in enumerate(fields):
                field_type, field_name = field.group(1), field.group(2)

                # Annotations belong to the field they precede. The old
                # five-line window leaked @Id, nullable and length onto
                # whichever field happened to follow.
                region_start = fields[i - 1].end() if i else 0
                annos = body[region_start : field.start()]

                if RE_TRANSIENT.search(annos):
                    continue
                if field_type.startswith(COLLECTION_PREFIXES):
                    continue

                is_pk = bool(RE_ID.search(annos))
                join_match = RE_JOIN_COLUMN.search(annos)
                is_fk = bool(join_match) or bool(RE_TO_ONE.search(annos))

                column_match = RE_COLUMN_NAME.search(annos)
                if column_match:
                    col_name = column_match.group(1)
                elif join_match:
                    col_name = join_match.group(1)
                elif is_fk:
                    # @ManyToOne with no @JoinColumn: JPA derives <field>_<pk>.
                    col_name = f"{self._snake(field_name)}_id"
                else:
                    col_name = self._snake(field_name)

                # JPA's @Column(nullable) defaults to true; a primary key is
                # never nullable regardless.
                null_match = RE_NULLABLE.search(annos)
                nullable = null_match.group(1) == "true" if null_match else True
                if is_pk:
                    nullable = False

                len_match = RE_LENGTH.search(annos)

                columns.append(
                    ColumnDefinition(
                        name=col_name,
                        data_type=field_type,
                        nullable=nullable,
                        is_primary_key=is_pk,
                        is_foreign_key=is_fk,
                        max_length=int(len_match.group(1)) if len_match else None,
                    )
                )

            if columns:
                tables.append(
                    TableDefinition(
                        name=table_name, columns=columns, source_file=file_path
                    )
                )

        return tables

    def _read_file(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
