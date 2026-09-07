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
# A relation's target may be quoted and app-qualified ("billing.Account"),
# quoted and bare ("Account"), the literal "self", or an unquoted class name.
RE_FK_TO = re.compile(
    r"models\.(?:ForeignKey|OneToOneField)\(\s*(?:['\"]([\w.]+)['\"]|(\w+))"
)
RE_DB_INDEX = re.compile(r"db_index\s*=\s*True")

# Meta options that change what — or whether — a table is called.
RE_DB_TABLE = re.compile(r"db_table\s*=\s*['\"]([^'\"]+)['\"]")
RE_ABSTRACT = re.compile(r"abstract\s*=\s*True")

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

    @staticmethod
    def _app_label(rel_path: Path) -> str | None:
        """The Django app a models file belongs to, from its location.

        Django names a table ``<app_label>_<modelname>``, and the app label
        defaults to the app package's directory name. Both layouts occur:

            myapp/models.py          -> myapp
            myapp/models/order.py    -> myapp   (never "models")

        Returns None for a models.py sitting at the project root, where there
        is no app package to name.
        """
        parts = rel_path.parts
        if len(parts) >= 3 and parts[-2] == "models":
            return parts[-3]
        if len(parts) >= 2 and parts[-1] == "models.py":
            return parts[-2]
        return None

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
            rel_path = path.relative_to(root)
            rel = str(rel_path)
            tables = self._parse_model_file(content, rel, self._app_label(rel_path))
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
        self, content: str, file_path: str, app_label: str | None = None
    ) -> list[TableDefinition]:
        tables = []
        class_blocks = re.split(r"(?=^class\s+\w+)", content, flags=re.MULTILINE)

        for block in class_blocks:
            class_match = RE_CLASS.search(block)
            if not class_match:
                continue

            class_name = class_match.group(1)

            # An abstract model contributes fields to its children and has no
            # table of its own. Emitting one guarantees a false missing_table.
            if RE_ABSTRACT.search(block):
                continue

            table_name = self._table_name(class_name, block, app_label)

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

                is_fk = field_type in ("ForeignKey", "OneToOneField")
                fk_table = (
                    self._fk_target(field_match.group(0), table_name, app_label)
                    if is_fk
                    else None
                )

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

    @staticmethod
    def _table_name(class_name: str, block: str, app_label: str | None) -> str:
        """Resolve a model class to the table Django would create for it.

        ``Meta.db_table`` is absolute when present. Otherwise the name is
        ``<app_label>_<modelname>``; without an app label — a models.py at the
        project root — the bare model name is the best available guess.

        This matters more than it looks: drift is matched on table name, so
        returning the bare model name for an app-prefixed table reports every
        table in the project as missing and invites a CREATE TABLE for each
        one that already exists.
        """
        db_table = RE_DB_TABLE.search(block)
        if db_table:
            return db_table.group(1)
        if app_label:
            return f"{app_label}_{class_name}".lower()
        return class_name.lower()

    @staticmethod
    def _fk_target(
        field_source: str, own_table: str, app_label: str | None
    ) -> str | None:
        """Resolve a relation's target to the table name it references.

        Handles the four spellings Django accepts: "app.Model", "Model",
        "self", and a bare class reference.

        Limitation, deliberate: if the *referenced* model overrides its own
        Meta.db_table, that override cannot be seen from this field alone, so
        the convention name is returned. Nothing in drift.py or migration.py
        reads foreign_key_table today, so this is reporting metadata rather
        than something that can generate a wrong statement.
        """
        match = RE_FK_TO.search(field_source)
        if not match:
            return None
        target = match.group(1) or match.group(2)
        if not target:
            return None
        if target == "self":
            return own_table
        if "." in target:
            app, _, model = target.rpartition(".")
            return f"{app}_{model}".lower()
        if app_label:
            return f"{app_label}_{target}".lower()
        return target.lower()

    def _read_file(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
