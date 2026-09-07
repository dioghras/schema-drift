"""EF Core schema collector — parses .cs entity classes and migrations."""

import re
from pathlib import Path

from schema_drift.schema_collectors.base import (
    BaseSchemaCollector,
    ColumnDefinition,
    IndexDefinition,
    MigrationInfo,
    SchemaSnapshot,
    TableDefinition,
)

# Regex patterns for EF Core entity parsing
RE_CLASS = re.compile(r"public\s+class\s+(\w+)")
RE_TABLE_ATTR = re.compile(r'\[Table\("(\w+)"\)\]')
RE_PROPERTY = re.compile(
    r"public\s+(virtual\s+)?([\w<>?\[\]]+)\s+(\w+)\s*\{"
)
RE_KEY_ATTR = re.compile(r"\[Key\]")
RE_REQUIRED_ATTR = re.compile(r"\[Required\]")
RE_MAXLEN_ATTR = re.compile(r"\[(?:MaxLength|StringLength)\((\d+)\)\]")
RE_FK_ATTR = re.compile(r'\[ForeignKey\("(\w+)"\)\]')
RE_DBSET = re.compile(r"DbSet<(\w+)>\s+(\w+)")
RE_COLUMN_ATTR = re.compile(r'\[Column\("([^"]+)"')
RE_NOT_MAPPED = re.compile(r"\[NotMapped\]")

# Navigation properties are not columns. Collections are recognised by their
# type; a single-reference navigation ("public Category Category") is only
# distinguishable by knowing that Category is itself an entity, which is why
# entity class names are collected in a first pass.
COLLECTION_PREFIXES = (
    "ICollection", "List", "IList", "IEnumerable", "HashSet", "Collection",
)

# Migration patterns
RE_CREATE_TABLE = re.compile(r'\.CreateTable\(\s*name:\s*"(\w+)"')
RE_ADD_COLUMN = re.compile(r'\.AddColumn<\w+>\(\s*name:\s*"(\w+)".*?table:\s*"(\w+)"', re.DOTALL)
RE_CREATE_INDEX = re.compile(r'\.CreateIndex\(\s*name:\s*"(\w+)".*?table:\s*"(\w+)"', re.DOTALL)
RE_DROP_TABLE = re.compile(r'\.DropTable\(\s*name:\s*"(\w+)"')
RE_MIGRATION_ATTR = re.compile(r'\[Migration\("(\w+)"\)\]')


class EFCoreSchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "efcore"

    def entity_file_patterns(self) -> list[str]:
        return ["**/*.cs"]

    def migration_file_patterns(self) -> list[str]:
        return ["**/Migrations/*.cs"]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())

        # Parse entity files
        entity_files = self._find_files(project_path, self.entity_file_patterns())
        dbset_names: dict[str, str] = {}  # ClassName -> TableName from DbSet

        # First pass: find DbContext to get DbSet mappings
        entity_class_names: set[str] = set()
        for path in entity_files:
            content = self._read_file(path)
            if not content:
                continue
            for match in RE_DBSET.finditer(content):
                entity_class, prop_name = match.group(1), match.group(2)
                dbset_names[entity_class] = prop_name
            for name, _, _ in self._class_blocks(content):
                entity_class_names.add(name)

        # Second pass: parse entity classes
        root = Path(project_path)
        for path in entity_files:
            # Skip migration files, designer files
            rel = str(path.relative_to(root))
            if "Migrations" in rel or ".Designer.cs" in rel:
                continue

            content = self._read_file(path)
            if not content:
                continue

            tables = self._parse_entity_file(
                content, rel, dbset_names, entity_class_names
            )
            if tables:
                snapshot.tables.extend(tables)
                snapshot.raw_files_parsed += 1

        # Parse migrations
        migration_files = self._find_files(
            project_path, self.migration_file_patterns()
        )
        for path in migration_files:
            rel = str(path.relative_to(root))
            if ".Designer.cs" in rel or "Snapshot.cs" in rel:
                continue
            content = self._read_file(path)
            if not content:
                continue
            migration = self._parse_migration_file(content, rel)
            if migration:
                snapshot.migrations.append(migration)
                snapshot.raw_files_parsed += 1

        return snapshot

    @staticmethod
    def _class_blocks(content: str) -> list[tuple[str, str, str]]:
        """Split C# source into (class_name, attributes_before, body).

        The parser used to take the first class in a file and then scan every
        property in the whole file into it, so a two-entity file produced one
        table carrying both entities' columns and lost the second entity
        entirely. Braces are matched here so a class body ends where it
        actually ends, nesting and namespaces included.
        """
        blocks: list[tuple[str, str, str]] = []
        for match in RE_CLASS.finditer(content):
            open_idx = content.find("{", match.end())
            if open_idx == -1:
                continue
            depth = 0
            close_idx = -1
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
            # Attributes sit on the lines immediately above the declaration.
            preceding = content[:match.start()]
            attrs = preceding[preceding.rfind("}") + 1:] if "}" in preceding else preceding
            blocks.append((match.group(1), attrs, content[open_idx + 1:close_idx]))
        return blocks

    @classmethod
    def _is_navigation(cls, prop_type: str, entity_class_names: set[str]) -> bool:
        bare = prop_type.rstrip("?")
        if any(bare.startswith(prefix) for prefix in COLLECTION_PREFIXES):
            return True
        # A property typed as another entity is a reference navigation; the
        # actual column is the FK scalar declared beside it.
        return bare in entity_class_names

    def _parse_entity_file(
        self,
        content: str,
        file_path: str,
        dbset_names: dict[str, str],
        entity_class_names: set[str] | None = None,
    ) -> list[TableDefinition]:
        entity_class_names = entity_class_names or set()
        tables: list[TableDefinition] = []

        for class_name, attrs_before, body in self._class_blocks(content):
            # A DbContext is not an entity; its DbSet properties would
            # otherwise be read as columns.
            if RE_DBSET.search(body):
                continue

            table_attr = RE_TABLE_ATTR.search(attrs_before)
            has_dbset = class_name in dbset_names
            has_key = RE_KEY_ATTR.search(body)

            properties = list(RE_PROPERTY.finditer(body))
            if not properties:
                continue
            if not table_attr and not has_dbset and not has_key:
                if not any(
                    m.group(3) == "Id" or m.group(3).endswith("Id")
                    for m in properties
                ):
                    continue

            # EF Core maps to the DbSet property name, or the entity name when
            # none is exposed. It does not pluralize — appending "s" invented a
            # table name that no database ever had.
            if table_attr:
                table_name = table_attr.group(1)
            elif has_dbset:
                table_name = dbset_names[class_name]
            else:
                table_name = class_name

            columns: list[ColumnDefinition] = []
            for i, prop in enumerate(properties):
                _, prop_type, prop_name = prop.groups()

                # Attributes belong to the property they precede. The old
                # five-line lookback leaked [Key] and [MaxLength] onto whatever
                # followed, so a [Key] Id made the next property a PK too.
                region_start = properties[i - 1].end() if i else 0
                attrs = body[region_start:prop.start()]

                if RE_NOT_MAPPED.search(attrs):
                    continue
                if self._is_navigation(prop_type, entity_class_names):
                    continue

                column_attr = RE_COLUMN_ATTR.search(attrs)
                col_name = column_attr.group(1) if column_attr else prop_name

                is_required = bool(RE_REQUIRED_ATTR.search(attrs))
                is_pk = bool(RE_KEY_ATTR.search(attrs)) or prop_name in (
                    "Id",
                    f"{class_name}Id",
                )
                fk_match = RE_FK_ATTR.search(attrs)
                is_fk = bool(fk_match) or (
                    prop_name.endswith("Id") and not is_pk
                )
                maxlen_match = RE_MAXLEN_ATTR.search(attrs)

                columns.append(
                    ColumnDefinition(
                        name=col_name,
                        data_type=prop_type.rstrip("?"),
                        nullable="?" in prop_type and not is_required,
                        is_primary_key=is_pk,
                        is_foreign_key=is_fk,
                        foreign_key_table=fk_match.group(1) if fk_match else None,
                        max_length=int(maxlen_match.group(1)) if maxlen_match else None,
                    )
                )

            if columns:
                tables.append(
                    TableDefinition(
                        name=table_name, columns=columns, source_file=file_path
                    )
                )

        return tables

    def _parse_migration_file(
        self, content: str, file_path: str
    ) -> MigrationInfo | None:
        # Get migration ID from attribute or class name
        attr_match = RE_MIGRATION_ATTR.search(content)
        if attr_match:
            migration_id = attr_match.group(1)
        else:
            class_match = RE_CLASS.search(content)
            if not class_match:
                return None
            migration_id = class_match.group(1)

        operations = []
        for match in RE_CREATE_TABLE.finditer(content):
            operations.append(f"CreateTable {match.group(1)}")
        for match in RE_ADD_COLUMN.finditer(content):
            operations.append(f"AddColumn {match.group(2)}.{match.group(1)}")
        for match in RE_CREATE_INDEX.finditer(content):
            operations.append(f"CreateIndex {match.group(1)} on {match.group(2)}")
        for match in RE_DROP_TABLE.finditer(content):
            operations.append(f"DropTable {match.group(1)}")

        if not operations:
            return None

        # Try to extract timestamp from migration ID (e.g., "20240301123456_InitialCreate")
        ts_match = re.match(r"(\d{14})", migration_id)
        timestamp = ts_match.group(1) if ts_match else None

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
