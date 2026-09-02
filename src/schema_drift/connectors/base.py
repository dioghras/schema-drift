"""Base DB connector and dataclasses for live database analysis."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class LiveColumnInfo:
    name: str
    data_type: str
    nullable: bool
    max_length: int | None = None
    is_primary_key: bool = False


@dataclass
class LiveTableInfo:
    schema_name: str
    table_name: str
    columns: list[LiveColumnInfo] = field(default_factory=list)
    row_count: int | None = None
    size_bytes: int | None = None


@dataclass
class LiveIndexInfo:
    table_name: str
    index_name: str
    columns: list[str] = field(default_factory=list)
    is_unique: bool = False
    user_seeks: int = 0
    user_scans: int = 0
    user_lookups: int = 0
    user_updates: int = 0
    size_bytes: int | None = None
    fragmentation_percent: float | None = None


@dataclass
class SlowQuery:
    query_text: str
    execution_count: int = 0
    avg_elapsed_ms: float = 0.0
    max_elapsed_ms: float = 0.0
    avg_cpu_ms: float = 0.0
    avg_logical_reads: int = 0
    last_execution: str | None = None


@dataclass
class BackupInfo:
    database_name: str
    last_backup_date: str | None = None
    backup_type: str | None = None
    backup_size_bytes: int | None = None
    days_since_backup: int | None = None


@dataclass
class StoredProcedureInfo:
    schema_name: str
    name: str
    type: str = "PROCEDURE"  # PROCEDURE or FUNCTION
    created: str | None = None
    last_modified: str | None = None
    execution_count: int = 0
    avg_elapsed_ms: float = 0.0
    max_elapsed_ms: float = 0.0
    last_execution: str | None = None
    definition_length: int = 0
    has_dynamic_sql: bool = False


@dataclass
class LiveDBSnapshot:
    db_type: str
    database_name: str = ""
    tables: list[LiveTableInfo] = field(default_factory=list)
    indexes: list[LiveIndexInfo] = field(default_factory=list)
    slow_queries: list[SlowQuery] = field(default_factory=list)
    backups: list[BackupInfo] = field(default_factory=list)
    stored_procedures: list[StoredProcedureInfo] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BaseDBConnector(ABC):
    def __init__(self, connection_string: str):
        self.connection_string = connection_string

    @abstractmethod
    def db_type(self) -> str:
        """Return database type identifier."""

    @abstractmethod
    def test_connection(self) -> bool:
        """Test if the connection works."""

    @abstractmethod
    def get_tables(self) -> list[LiveTableInfo]:
        """Get table schema information."""

    @abstractmethod
    def get_indexes(self) -> list[LiveIndexInfo]:
        """Get index information with usage stats."""

    @abstractmethod
    def get_slow_queries(self, top_n: int = 20) -> list[SlowQuery]:
        """Get top N slowest queries."""

    @abstractmethod
    def get_backup_info(self) -> list[BackupInfo]:
        """Get backup status information."""

    @abstractmethod
    def get_stored_procedures(self) -> list[StoredProcedureInfo]:
        """Get stored procedures/functions with execution stats."""

    # --- DDL application (verified auto-apply of reconciliation migrations) -----
    #
    # These power the Phase-4 "apply migration" flow. Only ever called with the
    # *additive* statements from a generated migration (never the commented-out
    # destructive lines). The default implementation uses transactional DDL so a
    # dry-run can be rolled back without persisting anything; engines that do not
    # support transactional DDL (MySQL) opt out and the apply flow refuses.

    def supports_transactional_ddl(self) -> bool:
        """Whether DDL can run inside a transaction and be rolled back.

        True for PostgreSQL and SQL Server; MySQL auto-commits DDL and overrides
        this to ``False`` (so the verify dry-run can't be rolled back there).
        """
        return True

    def verify_ddl(self, statements: list[str]) -> tuple[bool, str]:
        """Dry-run the statements against the live schema, rolling back.

        Returns ``(ok, log)``. Persists nothing: the statements run inside a
        transaction that is always rolled back, so this proves the additive SQL is
        valid against the *real* current schema (catches "column already exists",
        "table missing", type errors) without touching production data.

        On engines without transactional DDL, refuses (apply is blocked upstream).
        """
        if not statements:
            return True, "No statements to verify."
        if not self.supports_transactional_ddl():
            return (
                False,
                "Transactional DDL verification is unsupported on this engine — "
                "apply blocked. Review and run the generated SQL manually.",
            )
        conn = self._connect()
        try:
            cursor = conn.cursor()
            for stmt in statements:
                cursor.execute(stmt)
            conn.rollback()
            return True, f"Dry run succeeded for {len(statements)} statement(s); rolled back."
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return False, f"Dry run failed: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def execute_ddl(self, statements: list[str]) -> None:
        """Apply the statements to the live database in a single transaction.

        All-or-nothing: any failure rolls back the whole batch and re-raises.
        """
        if not statements:
            return
        conn = self._connect()
        try:
            cursor = conn.cursor()
            for stmt in statements:
                cursor.execute(stmt)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def snapshot(self) -> LiveDBSnapshot:
        """Run all queries and return a complete snapshot.

        Each section is independent — failure in one doesn't prevent others.
        """
        result = LiveDBSnapshot(db_type=self.db_type())

        try:
            tables = self.get_tables()
            result.tables = tables
            if tables:
                result.database_name = tables[0].schema_name.split(".")[0] if "." in tables[0].schema_name else self.db_type()
        except Exception as e:
            logger.error(f"Failed to get tables: {e}")
            result.errors.append(f"Table query failed: {e}")

        try:
            result.indexes = self.get_indexes()
        except Exception as e:
            logger.error(f"Failed to get indexes: {e}")
            result.errors.append(f"Index query failed: {e}")

        try:
            result.slow_queries = self.get_slow_queries()
        except Exception as e:
            logger.error(f"Failed to get slow queries: {e}")
            result.errors.append(f"Slow query analysis failed: {e}")

        try:
            result.backups = self.get_backup_info()
        except Exception as e:
            logger.error(f"Failed to get backup info: {e}")
            result.errors.append(f"Backup query failed: {e}")

        try:
            result.stored_procedures = self.get_stored_procedures()
        except Exception as e:
            logger.error(f"Failed to get stored procedures: {e}")
            result.errors.append(f"Stored procedure query failed: {e}")

        return result
