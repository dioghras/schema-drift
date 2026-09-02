"""SQLAlchemy/Alembic schema collector — parses Python model files and migrations."""

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

# Model patterns
RE_CLASS = re.compile(r"class\s+(\w+)\(.*(?:Base|Model).*\)")
RE_TABLENAME = re.compile(r'__tablename__\s*=\s*["\'](\w+)["\']')
RE_MAPPED_COL = re.compile(
    r"(\w+)\s*(?::\s*Mapped\[((?:[^\[\]]|\[[^\[\]]*\])*)\])?\s*=\s*"
    r"mapped_column\(((?:[^()]*|\([^()]*\))*)\)",
    re.DOTALL,
)
RE_COLUMN = re.compile(
    r"(\w+)\s*=\s*(?:db\.)?Column\(((?:[^()]*|\([^()]*\))*)\)", re.DOTALL
)
RE_FK = re.compile(r'ForeignKey\(["\'](\w+)\.(\w+)["\']')
RE_INDEX = re.compile(r"index\s*=\s*True")
RE_PK = re.compile(r"primary_key\s*=\s*True")
RE_NULLABLE = re.compile(r"nullable\s*=\s*(True|False)")
# SQLAlchemy 2.0 encodes nullability in the annotation: Mapped[str] is NOT NULL,
# Mapped[str | None] / Mapped[Optional[str]] are nullable.
RE_ANNOTATION_NONE = re.compile(r"\|\s*None\b|\bNone\s*\||\bOptional\[")
RE_TYPE = re.compile(r"(String|Integer|Boolean|Float|DateTime|Text|UUID|BigInteger|Numeric|JSON)")
RE_STRING_LEN = re.compile(r"String\((\d+)\)")

# Alembic migration patterns
RE_OP_CREATE_TABLE = re.compile(r'op\.create_table\(\s*["\'](\w+)["\']')
RE_OP_ADD_COLUMN = re.compile(r'op\.add_column\(\s*["\'](\w+)["\']')
RE_OP_CREATE_INDEX = re.compile(r'op\.create_index\(\s*["\'](\w+)["\'].*?["\'](\w+)["\']')
RE_OP_DROP_TABLE = re.compile(r'op\.drop_table\(\s*["\'](\w+)["\']')
RE_REVISION = re.compile(r'revision\s*=\s*["\'](\w+)["\']')
RE_DOWN_REVISION = re.compile(r'down_revision\s*=\s*["\'](\w+)["\']')


class SQLAlchemySchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "sqlalchemy"

    def entity_file_patterns(self) -> list[str]:
        return ["**/models.py", "**/models/*.py", "**/model.py"]

    def migration_file_patterns(self) -> list[str]:
        return ["**/alembic/versions/*.py", "**/migrations/versions/*.py"]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())
        root = Path(project_path)

        # Parse model files
        model_files = self._find_files(project_path, self.entity_file_patterns())
        for path in model_files:
            content = self._read_file(path)
            if not content:
                continue
            rel = str(path.relative_to(root))
            tables = self._parse_model_file(content, rel)
            snapshot.tables.extend(tables)
            if tables:
                snapshot.raw_files_parsed += 1

        # Parse migration files
        migration_files = self._find_files(
            project_path, self.migration_file_patterns()
        )
        for path in migration_files:
            content = self._read_file(path)
            if not content:
                continue
            rel = str(path.relative_to(root))
            migration = self._parse_migration_file(content, rel)
            if migration:
                snapshot.migrations.append(migration)
                snapshot.raw_files_parsed += 1

        return snapshot

    def _parse_model_file(
        self, content: str, file_path: str
    ) -> list[TableDefinition]:
        tables = []

        # Split by class definitions
        class_blocks = re.split(r"(?=^class\s+\w+)", content, flags=re.MULTILINE)

        for block in class_blocks:
            class_match = RE_CLASS.search(block)
            if not class_match:
                continue

            class_name = class_match.group(1)
            tablename_match = RE_TABLENAME.search(block)
            table_name = tablename_match.group(1) if tablename_match else class_name.lower() + "s"

            columns = []

            # Try mapped_column style first (SQLAlchemy 2.0)
            for col_match in RE_MAPPED_COL.finditer(block):
                col = self._parse_column(
                    col_match.group(1), col_match.group(3), col_match.group(2)
                )
                if col:
                    columns.append(col)

            # Try Column() style (SQLAlchemy 1.x) — no annotation to read
            for col_match in RE_COLUMN.finditer(block):
                col = self._parse_column(col_match.group(1), col_match.group(2))
                if col:
                    columns.append(col)

            if columns:
                tables.append(
                    TableDefinition(
                        name=table_name,
                        columns=columns,
                        source_file=file_path,
                    )
                )

        return tables

    def _parse_column(
        self, name: str, definition: str, annotation: str | None = None
    ) -> ColumnDefinition | None:
        if name.startswith("_"):
            return None

        type_match = RE_TYPE.search(definition)
        data_type = type_match.group(1) if type_match else "Unknown"

        strlen_match = RE_STRING_LEN.search(definition)
        max_length = int(strlen_match.group(1)) if strlen_match else None

        is_pk = bool(RE_PK.search(definition))
        # An explicit nullable= kwarg wins; otherwise the Mapped[...] annotation
        # decides (2.0 style); otherwise fall back to "everything but the PK is
        # nullable", which is all a bare Column() tells us.
        nullable_match = RE_NULLABLE.search(definition)
        if nullable_match:
            nullable = nullable_match.group(1) == "True"
        elif annotation:
            nullable = bool(RE_ANNOTATION_NONE.search(annotation))
        else:
            nullable = not is_pk

        fk_match = RE_FK.search(definition)
        is_fk = bool(fk_match)
        fk_table = fk_match.group(1) if fk_match else None

        has_index = bool(RE_INDEX.search(definition))

        return ColumnDefinition(
            name=name,
            data_type=data_type,
            nullable=nullable,
            is_primary_key=is_pk,
            is_foreign_key=is_fk,
            foreign_key_table=fk_table,
            max_length=max_length,
        )

    def _parse_migration_file(
        self, content: str, file_path: str
    ) -> MigrationInfo | None:
        revision_match = RE_REVISION.search(content)
        if not revision_match:
            return None

        migration_id = revision_match.group(1)

        operations = []
        for match in RE_OP_CREATE_TABLE.finditer(content):
            operations.append(f"CreateTable {match.group(1)}")
        for match in RE_OP_ADD_COLUMN.finditer(content):
            operations.append(f"AddColumn {match.group(1)}")
        for match in RE_OP_CREATE_INDEX.finditer(content):
            operations.append(f"CreateIndex {match.group(1)} on {match.group(2)}")
        for match in RE_OP_DROP_TABLE.finditer(content):
            operations.append(f"DropTable {match.group(1)}")

        return MigrationInfo(
            migration_id=migration_id,
            file_path=file_path,
            operations=operations,
        )

    def _read_file(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
