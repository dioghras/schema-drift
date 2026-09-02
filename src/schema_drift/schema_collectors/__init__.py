from schema_drift.schema_collectors.activerecord import (
    ActiveRecordSchemaCollector,
)
from schema_drift.schema_collectors.base import BaseSchemaCollector
from schema_drift.schema_collectors.django_collector import (
    DjangoSchemaCollector,
)
from schema_drift.schema_collectors.efcore import (
    EFCoreSchemaCollector,
)
from schema_drift.schema_collectors.eloquent import (
    EloquentSchemaCollector,
)
from schema_drift.schema_collectors.gorm import (
    GORMSchemaCollector,
)
from schema_drift.schema_collectors.hibernate import (
    HibernateSchemaCollector,
)
from schema_drift.schema_collectors.sqlalchemy_collector import (
    SQLAlchemySchemaCollector,
)

SCHEMA_COLLECTOR_MAP: dict[str, type[BaseSchemaCollector]] = {
    "efcore": EFCoreSchemaCollector,
    "sqlalchemy": SQLAlchemySchemaCollector,
    "django": DjangoSchemaCollector,
    "hibernate": HibernateSchemaCollector,
    "gorm": GORMSchemaCollector,
    "activerecord": ActiveRecordSchemaCollector,
    "eloquent": EloquentSchemaCollector,
}

TECH_STACK_TO_SCHEMA_COLLECTOR: dict[str, str] = {
    "dotnet": "efcore",
    "csharp": "efcore",
    "python": "sqlalchemy",
    "fastapi": "sqlalchemy",
    "flask": "sqlalchemy",
    "django": "django",
    "java": "hibernate",
    "kotlin": "hibernate",
    "spring": "hibernate",
    "go": "gorm",
    "golang": "gorm",
    "ruby": "activerecord",
    "rails": "activerecord",
    "php": "eloquent",
    "laravel": "eloquent",
    "symfony": "eloquent",
}
