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
        for path in entity_files:
            content = self._read_file(path)
            if not content:
                continue
            for match in RE_DBSET.finditer(content):
                entity_class, prop_name = match.group(1), match.group(2)
                dbset_names[entity_class] = prop_name

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

            table = self._parse_entity_file(content, rel, dbset_names)
            if table:
                snapshot.tables.append(table)
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

    def _parse_entity_file(
        self, content: str, file_path: str, dbset_names: dict[str, str]
    ) -> TableDefinition | None:
        class_match = RE_CLASS.search(content)
        if not class_match:
            return None

        class_name = class_match.group(1)

        # Only parse classes that look like entities (have properties with get/set)
        properties = RE_PROPERTY.findall(content)
        if not properties:
            return None

        # Skip if clearly not an entity (no DbSet reference, no [Table] attr,
        # and no typical entity properties)
        table_attr = RE_TABLE_ATTR.search(content)
        has_dbset = class_name in dbset_names
        has_key = RE_KEY_ATTR.search(content)

        if not table_attr and not has_dbset and not has_key:
            # Check for common entity patterns: Id property
            if not any(p[2] == "Id" or p[2].endswith("Id") for p in properties):
                return None

        # Determine table name
        if table_attr:
            table_name = table_attr.group(1)
        elif has_dbset:
            table_name = dbset_names[class_name]
        else:
            table_name = class_name + "s"  # Default EF pluralization

        columns = []
        lines = content.split("\n")
        for i, line in enumerate(lines):
            prop_match = RE_PROPERTY.search(line)
            if not prop_match:
                continue

            _, prop_type, prop_name = prop_match.groups()

            # Skip navigation properties (collections, virtual references)
            if prop_type.startswith("ICollection") or prop_type.startswith("List"):
                continue
            if prop_type.startswith("virtual"):
                continue

            # Look at preceding lines for attributes
            preceding = "\n".join(lines[max(0, i - 5) : i])

            is_pk = bool(RE_KEY_ATTR.search(preceding)) or prop_name == "Id"
            is_required = bool(RE_REQUIRED_ATTR.search(preceding))
            nullable = "?" in prop_type and not is_required
            fk_match = RE_FK_ATTR.search(preceding)
            is_fk = bool(fk_match) or (
                prop_name.endswith("Id") and prop_name != "Id"
            )
            fk_table = fk_match.group(1) if fk_match else None
            maxlen_match = RE_MAXLEN_ATTR.search(preceding)
            max_length = int(maxlen_match.group(1)) if maxlen_match else None

            columns.append(
                ColumnDefinition(
                    name=prop_name,
                    data_type=prop_type.rstrip("?"),
                    nullable=nullable,
                    is_primary_key=is_pk,
                    is_foreign_key=is_fk,
                    foreign_key_table=fk_table,
                    max_length=max_length,
                )
            )

        if not columns:
            return None

        return TableDefinition(
            name=table_name, columns=columns, source_file=file_path
        )

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
