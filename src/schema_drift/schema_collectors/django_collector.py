"""Django ORM schema collector — parses models.py and migration files."""

import re
from pathlib import Path

from schema_drift.schema_collectors.base import (
    BaseSchemaCollector,
    ColumnDefinition,
    MigrationInfo,
    SchemaSnapshot,
    TableDefinition,
)

# Django model patterns
RE_CLASS = re.compile(r"class\s+(\w+)\(.*models\.Model.*\)")
RE_FIELD = re.compile(r"(\w+)\s*=\s*models\.(\w+(?:Field|Key))\(((?:[^()]*|\([^()]*\))*)\)", re.DOTALL)
RE_MAXLEN = re.compile(r"max_length\s*=\s*(\d+)")
RE_NULL = re.compile(r"null\s*=\s*(True|False)")
RE_PK = re.compile(r"primary_key\s*=\s*True")
RE_FK_TO = re.compile(r"models\.ForeignKey\(\s*['\"]?(\w+)['\"]?")
RE_DB_INDEX = re.compile(r"db_index\s*=\s*True")

# Django migration patterns
RE_CREATE_MODEL = re.compile(r"migrations\.CreateModel\(\s*name=['\"](\w+)['\"]")
RE_ADD_FIELD = re.compile(
    r"migrations\.AddField\(\s*model_name=['\"](\w+)['\"].*?name=['\"](\w+)['\"]",
    re.DOTALL,
)
RE_ADD_INDEX = re.compile(
    r"migrations\.AddIndex\(\s*model_name=['\"](\w+)['\"]", re.DOTALL
)
RE_DELETE_MODEL = re.compile(r"migrations\.DeleteModel\(\s*name=['\"](\w+)['\"]")

# Field type mapping
DJANGO_TYPE_MAP = {
    "CharField": "String",
    "TextField": "Text",
    "IntegerField": "Integer",
    "BigIntegerField": "BigInteger",
    "FloatField": "Float",
    "DecimalField": "Numeric",
    "BooleanField": "Boolean",
    "DateTimeField": "DateTime",
    "DateField": "Date",
    "UUIDField": "UUID",
    "EmailField": "String",
    "URLField": "String",
    "SlugField": "String",
    "FileField": "String",
    "ImageField": "String",
    "JSONField": "JSON",
    "AutoField": "Integer",
    "BigAutoField": "BigInteger",
    "ForeignKey": "Integer",
    "OneToOneField": "Integer",
}


class DjangoSchemaCollector(BaseSchemaCollector):
    def orm_type(self) -> str:
        return "django"

    def entity_file_patterns(self) -> list[str]:
        return ["**/models.py", "**/models/*.py"]

    def migration_file_patterns(self) -> list[str]:
        return ["**/migrations/0*.py"]

    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        snapshot = SchemaSnapshot(orm_type=self.orm_type())
        root = Path(project_path)

        # Parse model files
        model_files = self._find_files(project_path, self.entity_file_patterns())
        for path in model_files:
            # Skip migration files
            if "migrations" in str(path.relative_to(root)):
                continue
            content = self._read_file(path)
            if not content:
                continue
            rel = str(path.relative_to(root))
            tables = self._parse_model_file(content, rel)
            snapshot.tables.extend(tables)
            if tables:
                snapshot.raw_files_parsed += 1

        # Parse migrations
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
        class_blocks = re.split(r"(?=^class\s+\w+)", content, flags=re.MULTILINE)

        for block in class_blocks:
            class_match = RE_CLASS.search(block)
            if not class_match:
                continue

            class_name = class_match.group(1)
            # Django default table name: appname_modelname (lowercase)
            table_name = class_name.lower()

            columns = []
            # Django auto-adds an 'id' PK unless overridden
            has_explicit_pk = bool(RE_PK.search(block))
            if not has_explicit_pk:
                columns.append(
                    ColumnDefinition(
                        name="id",
                        data_type="BigInteger",
                        nullable=False,
                        is_primary_key=True,
                    )
                )

            for field_match in RE_FIELD.finditer(block):
                field_name = field_match.group(1)
                field_type = field_match.group(2)
                field_args = field_match.group(3)

                data_type = DJANGO_TYPE_MAP.get(field_type, "Unknown")

                maxlen_match = RE_MAXLEN.search(field_args)
                max_length = int(maxlen_match.group(1)) if maxlen_match else None

                null_match = RE_NULL.search(field_args)
                nullable = null_match.group(1) == "True" if null_match else False

                is_pk = bool(RE_PK.search(field_args))

                fk_match = RE_FK_TO.search(field_match.group(0))
                is_fk = field_type in ("ForeignKey", "OneToOneField")
                fk_table = fk_match.group(1).lower() if fk_match else None

                # For FK fields, Django appends _id
                col_name = f"{field_name}_id" if is_fk else field_name

                columns.append(
                    ColumnDefinition(
                        name=col_name,
                        data_type=data_type,
                        nullable=nullable,
                        is_primary_key=is_pk,
                        is_foreign_key=is_fk,
                        foreign_key_table=fk_table,
                        max_length=max_length,
                    )
                )

            if columns:
                tables.append(
                    TableDefinition(
                        name=table_name,
                        columns=columns,
                        source_file=file_path,
                    )
                )

        return tables

    def _parse_migration_file(
        self, content: str, file_path: str
    ) -> MigrationInfo | None:
        # Use filename as migration ID
        migration_id = Path(file_path).stem

        operations = []
        for match in RE_CREATE_MODEL.finditer(content):
            operations.append(f"CreateModel {match.group(1)}")
        for match in RE_ADD_FIELD.finditer(content):
            operations.append(f"AddField {match.group(1)}.{match.group(2)}")
        for match in RE_ADD_INDEX.finditer(content):
            operations.append(f"AddIndex {match.group(1)}")
        for match in RE_DELETE_MODEL.finditer(content):
            operations.append(f"DeleteModel {match.group(1)}")

        if not operations:
            return None

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
