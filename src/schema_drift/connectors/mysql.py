"""MySQL connector — queries INFORMATION_SCHEMA and performance_schema."""

import logging

from schema_drift.connectors.base import (
    BackupInfo,
    BaseDBConnector,
    LiveColumnInfo,
    LiveIndexInfo,
    LiveTableInfo,
    SlowQuery,
    StoredProcedureInfo,
)

logger = logging.getLogger(__name__)


class MySQLConnector(BaseDBConnector):
    def db_type(self) -> str:
        return "mysql"

    def supports_transactional_ddl(self) -> bool:
        # MySQL implicitly commits on DDL, so a verify dry-run cannot be rolled
        # back. The apply flow refuses rather than risk a half-applied migration;
        # a true throwaway-database replay is a documented follow-up.
        return False

    def _connect(self):
        import pymysql

        # Parse connection string: host=X;port=Y;user=Z;password=W;database=D
        params = {}
        for part in self.connection_string.split(";"):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                params[key.strip().lower()] = value.strip()

        return pymysql.connect(
            host=params.get("host", "localhost"),
            port=int(params.get("port", 3306)),
            user=params.get("user", "root"),
            password=params.get("password", ""),
            database=params.get("database", ""),
            cursorclass=pymysql.cursors.DictCursor,
        )

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            logger.error(f"MySQL connection test failed: {e}")
            return False

    def get_tables(self) -> list[LiveTableInfo]:
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                t.TABLE_SCHEMA,
                t.TABLE_NAME,
                t.TABLE_ROWS,
                t.DATA_LENGTH + t.INDEX_LENGTH AS size_bytes
            FROM INFORMATION_SCHEMA.TABLES t
            WHERE t.TABLE_SCHEMA = DATABASE()
                AND t.TABLE_TYPE = 'BASE TABLE'
            ORDER BY t.TABLE_NAME
        """)
        tables_raw = cursor.fetchall()

        tables = []
        for row in tables_raw:
            schema_name = row["TABLE_SCHEMA"]
            table_name = row["TABLE_NAME"]

            cursor.execute("""
                SELECT
                    c.COLUMN_NAME,
                    c.DATA_TYPE,
                    c.IS_NULLABLE,
                    c.CHARACTER_MAXIMUM_LENGTH,
                    c.COLUMN_KEY
                FROM INFORMATION_SCHEMA.COLUMNS c
                WHERE c.TABLE_SCHEMA = DATABASE() AND c.TABLE_NAME = %s
                ORDER BY c.ORDINAL_POSITION
            """, (table_name,))

            columns = [
                LiveColumnInfo(
                    name=col["COLUMN_NAME"],
                    data_type=col["DATA_TYPE"],
                    nullable=col["IS_NULLABLE"] == "YES",
                    max_length=col["CHARACTER_MAXIMUM_LENGTH"],
                    is_primary_key=col["COLUMN_KEY"] == "PRI",
                )
                for col in cursor.fetchall()
            ]

            tables.append(
                LiveTableInfo(
                    schema_name=schema_name,
                    table_name=table_name,
                    columns=columns,
                    row_count=row.get("TABLE_ROWS"),
                    size_bytes=row.get("size_bytes"),
                )
            )

        conn.close()
        return tables

    def get_indexes(self) -> list[LiveIndexInfo]:
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                s.TABLE_NAME,
                s.INDEX_NAME,
                s.NON_UNIQUE,
                GROUP_CONCAT(s.COLUMN_NAME ORDER BY s.SEQ_IN_INDEX) AS columns
            FROM INFORMATION_SCHEMA.STATISTICS s
            WHERE s.TABLE_SCHEMA = DATABASE()
            GROUP BY s.TABLE_NAME, s.INDEX_NAME, s.NON_UNIQUE
            ORDER BY s.TABLE_NAME, s.INDEX_NAME
        """)

        indexes = [
            LiveIndexInfo(
                table_name=row["TABLE_NAME"],
                index_name=row["INDEX_NAME"],
                is_unique=not bool(row["NON_UNIQUE"]),
                columns=row["columns"].split(",") if row["columns"] else [],
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return indexes

    def get_slow_queries(self, top_n: int = 20) -> list[SlowQuery]:
        conn = self._connect()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT
                    LEFT(DIGEST_TEXT, 500) AS query_text,
                    COUNT_STAR AS execution_count,
                    AVG_TIMER_WAIT / 1000000000 AS avg_elapsed_ms,
                    MAX_TIMER_WAIT / 1000000000 AS max_elapsed_ms,
                    SUM_ROWS_EXAMINED / GREATEST(COUNT_STAR, 1) AS avg_logical_reads,
                    LAST_SEEN
                FROM performance_schema.events_statements_summary_by_digest
                WHERE DIGEST_TEXT IS NOT NULL
                    AND COUNT_STAR > 0
                ORDER BY AVG_TIMER_WAIT DESC
                LIMIT %s
            """, (top_n,))

            queries = [
                SlowQuery(
                    query_text=row["query_text"],
                    execution_count=row["execution_count"],
                    avg_elapsed_ms=float(row["avg_elapsed_ms"]) if row["avg_elapsed_ms"] else 0.0,
                    max_elapsed_ms=float(row["max_elapsed_ms"]) if row["max_elapsed_ms"] else 0.0,
                    avg_logical_reads=row["avg_logical_reads"] or 0,
                    last_execution=str(row["LAST_SEEN"]) if row.get("LAST_SEEN") else None,
                )
                for row in cursor.fetchall()
            ]
        except Exception:
            logger.info("performance_schema not available for slow query analysis")
            queries = []

        conn.close()
        return queries

    def get_backup_info(self) -> list[BackupInfo]:
        # MySQL has no built-in backup catalog
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT DATABASE()")
        db_name = cursor.fetchone()["DATABASE()"]
        conn.close()

        return [
            BackupInfo(
                database_name=db_name,
                last_backup_date=None,
                backup_type=None,
                days_since_backup=None,
            )
        ]

    def get_stored_procedures(self) -> list[StoredProcedureInfo]:
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                r.ROUTINE_SCHEMA,
                r.ROUTINE_NAME,
                r.ROUTINE_TYPE,
                r.CREATED,
                r.LAST_ALTERED,
                LENGTH(r.ROUTINE_DEFINITION) AS definition_length,
                CASE WHEN r.ROUTINE_DEFINITION LIKE '%%PREPARE%%'
                          OR r.ROUTINE_DEFINITION LIKE '%%EXECUTE%%'
                     THEN 1 ELSE 0 END AS has_dynamic_sql
            FROM INFORMATION_SCHEMA.ROUTINES r
            WHERE r.ROUTINE_SCHEMA = DATABASE()
            ORDER BY r.ROUTINE_NAME
        """)

        procedures = [
            StoredProcedureInfo(
                schema_name=row["ROUTINE_SCHEMA"],
                name=row["ROUTINE_NAME"],
                type=row["ROUTINE_TYPE"] or "PROCEDURE",
                created=str(row["CREATED"]) if row.get("CREATED") else None,
                last_modified=str(row["LAST_ALTERED"]) if row.get("LAST_ALTERED") else None,
                definition_length=row["definition_length"] or 0,
                has_dynamic_sql=bool(row["has_dynamic_sql"]),
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return procedures
