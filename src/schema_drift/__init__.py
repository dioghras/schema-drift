"""schema-drift — compare ORM models against a live database and generate verified additive migrations.

The pipeline is four stages, each usable on its own:

    collect  →  snapshot  →  diff  →  generate + verify

1. ``schema_collectors`` parse ORM model definitions out of a source tree
   (SQLAlchemy, Django, EF Core, Hibernate, GORM, ActiveRecord, Eloquent)
   into a :class:`~schema_drift.schema_collectors.base.SchemaSnapshot`.
2. ``connectors`` read the real schema out of a live database
   (PostgreSQL, MySQL, SQL Server) into a
   :class:`~schema_drift.connectors.base.LiveDBSnapshot`.
3. :func:`~schema_drift.drift.detect_drift` diffs the two.
4. :func:`~schema_drift.migration.generate_migration` turns the drift into
   additive-only DDL, which
   :meth:`~schema_drift.connectors.base.BaseDBConnector.verify_ddl` proves
   against the live schema inside a transaction that is always rolled back.

Destructive changes are never executed — they are emitted as commented-out SQL
for a human to review.
"""

from schema_drift.connectors import CONNECTOR_MAP, BaseDBConnector
from schema_drift.connectors.base import LiveDBSnapshot
from schema_drift.drift import DriftIssue, detect_drift
from schema_drift.migration import GeneratedMigration, generate_migration
from schema_drift.schema_collectors import (
    SCHEMA_COLLECTOR_MAP,
    TECH_STACK_TO_SCHEMA_COLLECTOR,
    BaseSchemaCollector,
)
from schema_drift.schema_collectors.base import (
    ColumnDefinition,
    IndexDefinition,
    SchemaSnapshot,
    TableDefinition,
)

__version__ = "0.1.0"

__all__ = [
    "detect_drift",
    "DriftIssue",
    "generate_migration",
    "GeneratedMigration",
    "BaseDBConnector",
    "CONNECTOR_MAP",
    "LiveDBSnapshot",
    "BaseSchemaCollector",
    "SCHEMA_COLLECTOR_MAP",
    "TECH_STACK_TO_SCHEMA_COLLECTOR",
    "SchemaSnapshot",
    "TableDefinition",
    "ColumnDefinition",
    "IndexDefinition",
    "__version__",
]
