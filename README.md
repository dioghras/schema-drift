# schema-drift

Your ORM models say one thing. Your production database says another. `schema-drift` finds the difference and writes the SQL that closes it. It parses entity definitions straight out of a source tree — **SQLAlchemy, Django, EF Core, Hibernate, GORM, ActiveRecord, or Eloquent** — reads the real schema out of a live **PostgreSQL, MySQL, or SQL Server** database, diffs the two, and generates dialect-aware DDL to reconcile them.

The interesting part is what it refuses to do. Generating migration SQL is the easy half; the hard half is being trustworthy enough to point at a production database. So the generator is **additive-only** — it will `CREATE TABLE`, `ADD COLUMN`, and widen a `NOT NULL` to nullable, but a `DROP` or a `NOT NULL` tightening is emitted as a **commented-out line with a warning** and never as something you can accidentally execute. And before any statement runs, it is proved against the live schema by a dry run inside a transaction that is **always rolled back**. Nothing is applied that hasn't already been shown to work on the actual current schema.

```python
from schema_drift import CONNECTOR_MAP, SCHEMA_COLLECTOR_MAP, detect_drift, generate_migration

code_schema = SCHEMA_COLLECTOR_MAP["sqlalchemy"]().collect_schema("./myapp")
connector = CONNECTOR_MAP["postgresql"]("postgresql://user:pass@host/db")

issues = detect_drift(code_schema, connector.snapshot())
migration = generate_migration(issues, code_schema, "postgresql")

print(migration.sql)          # full script: additive statements + commented destructive
print(migration.warnings)     # why each destructive change was withheld

ok, log = connector.verify_ddl(migration.statements)   # dry run, always rolled back
if ok:
    connector.execute_ddl(migration.statements)        # single transaction, all-or-nothing
```

## Install

```bash
pip install schema-drift[postgresql]   # or [mysql], [sqlserver], [all]
```

Drivers are imported lazily inside each connector, so you install only the one matching the database you actually use. The library itself has **no required dependencies**.

## What it detects

| Drift type | Generated | Why |
|---|---|---|
| `missing_table` | `CREATE TABLE` | Additive — nothing to lose |
| `missing_column` | `ADD COLUMN` | Additive |
| `nullable_mismatch` (widening) | `DROP NOT NULL` | Strictly loosens a constraint |
| `nullable_mismatch` (tightening) | *commented* | Fails if `NULL`s exist — backfill first |
| `extra_table` | *commented* `DROP TABLE` | Destructive, and often intentional |
| `extra_column` | *commented* `DROP COLUMN` | Destructive |

Framework bookkeeping tables (`alembic_version`, `__EFMigrationsHistory`, `django_migrations`, the Django `auth_*` set) are skipped rather than reported as drift you need to act on.

Generation is fully deterministic — no API key, no network, no model call. The same drift always produces the same SQL.

## The safety design

Three decisions shape everything, and each one cost something:

**Additive-only, rather than reversible migrations.** A generated `DROP COLUMN` is unrecoverable in a way a wrong `ADD COLUMN` never is, and the asymmetry doesn't reverse just because a rollback script exists — you still lose the data. The cost is that drift is only ever *half*-closed automatically. Removing a column a human deleted from the models is left as homework, emitted as commented SQL. That's deliberate: it's the half where being wrong is expensive.

**Verification by rolled-back transaction, rather than against a throwaway database.** Wrapping the DDL in a transaction and rolling it back proves the statements against the *real, current* production schema — it catches the "column already exists," "referenced table missing," "type isn't coercible" class of failure that a clone can only catch if the clone is perfectly in sync, which is precisely the assumption in doubt. The cost is real and it's paid in one place, below.

**Refusing beats guessing.** When verification can't run, the answer is to stop, not to proceed carefully. Every safety property here is worth exactly as much as its worst-case behavior.

### Known limitation: MySQL cannot be verified

MySQL auto-commits DDL. There is no transaction to roll back, so the dry run can't be undone, so **`verify_ddl` refuses on MySQL and returns an error rather than a pass**:

```python
def supports_transactional_ddl(self) -> bool:
    """True for PostgreSQL and SQL Server; MySQL auto-commits DDL and
    overrides this to False (so the verify dry-run can't be rolled back there)."""
```

This is the price of the rollback approach. A throwaway-database strategy would have covered MySQL, at the cost of only ever verifying against a *copy* of the schema rather than the live one.

Detection, drift analysis, and SQL generation all work fine on MySQL — you get the script, reviewed and run by a human. It is only the automated verify-then-apply path that is closed. **Reporting an honest refusal is treated as the correct outcome, not a gap to paper over.** A verification that silently passes because it couldn't actually check anything is worse than no verification at all, because someone will trust it.

## Architecture

Four stages, each usable on its own:

```
collect ──────────→ snapshot ──────────→ diff ──────────→ generate + verify
schema_collectors   connectors           drift.py         migration.py
(7 ORMs, static     (3 engines, live     detect_drift()   generate_migration()
 source parsing)     introspection)                       verify_ddl()
```

Adding an ORM means implementing `BaseSchemaCollector` (four methods); adding a database engine means implementing `BaseDBConnector`. Neither requires touching the diff or the generator — they speak only in `SchemaSnapshot` and `LiveDBSnapshot`.

Collectors parse source statically; they never import or execute your application code.

## Tests

```bash
uv sync --extra dev
uv run pytest
```

## Origin

Extracted from [Sentinel](https://github.com/dioghras/maintenance), a maintenance-automation platform where this runs as the database scanner. Sentinel wraps it in the parts that are specific to a multi-tenant service — an audit row per attempt, an admin-only trigger, per-project opt-in — while everything here is the engine underneath.

## License

MIT
