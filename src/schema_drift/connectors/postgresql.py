"""PostgreSQL connector — queries system catalogs for schema, indexes, performance."""

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


class PostgreSQLConnector(BaseDBConnector):
    def db_type(self) -> str:
        return "postgresql"

    def _connect(self):
        import psycopg2
        import psycopg2.extras

        return psycopg2.connect(self.connection_string)

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            logger.error(f"PostgreSQL connection test failed: {e}")
            return False

    def get_tables(self) -> list[LiveTableInfo]:
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                t.table_schema,
                t.table_name,
                s.n_live_tup AS row_count,
                pg_total_relation_size(quote_ident(t.table_schema) || '.' || quote_ident(t.table_name)) AS size_bytes
            FROM information_schema.tables t
            LEFT JOIN pg_stat_user_tables s
                ON t.table_name = s.relname AND t.table_schema = s.schemaname
            WHERE t.table_type = 'BASE TABLE'
                AND t.table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY t.table_name
        """)
        tables_raw = cursor.fetchall()

        tables = []
        for schema_name, table_name, row_count, size_bytes in tables_raw:
            cursor.execute("""
                SELECT
                    c.column_name,
                    c.data_type,
                    c.is_nullable,
                    c.character_maximum_length,
                    CASE WHEN pk.column_name IS NOT NULL THEN TRUE ELSE FALSE END AS is_pk
                FROM information_schema.columns c
                LEFT JOIN (
                    SELECT ku.column_name, ku.table_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage ku
                        ON tc.constraint_name = ku.constraint_name
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                        AND tc.table_schema = %s
                ) pk ON c.table_name = pk.table_name AND c.column_name = pk.column_name
                WHERE c.table_schema = %s AND c.table_name = %s
                ORDER BY c.ordinal_position
            """, (schema_name, schema_name, table_name))

            columns = [
                LiveColumnInfo(
                    name=col[0],
                    data_type=col[1],
                    nullable=col[2] == "YES",
                    max_length=col[3],
                    is_primary_key=bool(col[4]),
                )
                for col in cursor.fetchall()
            ]

            tables.append(
                LiveTableInfo(
                    schema_name=schema_name,
                    table_name=table_name,
                    columns=columns,
                    row_count=row_count,
                    size_bytes=size_bytes,
                )
            )

        conn.close()
        return tables

    def get_indexes(self) -> list[LiveIndexInfo]:
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                sui.relname AS table_name,
                sui.indexrelname AS index_name,
                ix.indisunique AS is_unique,
                sui.idx_scan AS user_seeks,
                sui.idx_tup_read AS user_scans,
                sui.idx_tup_fetch AS user_lookups,
                pg_relation_size(sui.indexrelid) AS size_bytes,
                array_to_string(
                    array_agg(a.attname ORDER BY k.n),
                    ','
                ) AS columns
            FROM pg_stat_user_indexes sui
            JOIN pg_index ix ON sui.indexrelid = ix.indexrelid
            CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, n)
            JOIN pg_attribute a ON a.attrelid = sui.relid AND a.attnum = k.attnum
            GROUP BY sui.relname, sui.indexrelname, ix.indisunique,
                     sui.idx_scan, sui.idx_tup_read, sui.idx_tup_fetch, sui.indexrelid
            ORDER BY sui.relname, sui.indexrelname
        """)

        indexes = [
            LiveIndexInfo(
                table_name=row[0],
                index_name=row[1],
                is_unique=bool(row[2]),
                user_seeks=row[3] or 0,
                user_scans=row[4] or 0,
                user_lookups=row[5] or 0,
                size_bytes=row[6],
                columns=row[7].split(",") if row[7] else [],
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return indexes

    def get_slow_queries(self, top_n: int = 20) -> list[SlowQuery]:
        conn = self._connect()
        cursor = conn.cursor()

        # Check if pg_stat_statements is available
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
            )
        """)
        has_extension = cursor.fetchone()[0]

        if not has_extension:
            conn.close()
            logger.info("pg_stat_statements extension not installed")
            return []

        cursor.execute("""
            SELECT
                LEFT(query, 500) AS query_text,
                calls AS execution_count,
                mean_exec_time AS avg_elapsed_ms,
                max_exec_time AS max_elapsed_ms,
                mean_exec_time AS avg_cpu_ms,
                shared_blks_read / GREATEST(calls, 1) AS avg_logical_reads
            FROM pg_stat_statements
            WHERE calls > 0
                AND query NOT LIKE '%%pg_stat%%'
            ORDER BY mean_exec_time DESC
            LIMIT %s
        """, (top_n,))

        queries = [
            SlowQuery(
                query_text=row[0],
                execution_count=row[1],
                avg_elapsed_ms=float(row[2]) if row[2] else 0.0,
                max_elapsed_ms=float(row[3]) if row[3] else 0.0,
                avg_cpu_ms=float(row[4]) if row[4] else 0.0,
                avg_logical_reads=row[5] or 0,
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return queries

    def get_backup_info(self) -> list[BackupInfo]:
        # PostgreSQL doesn't have a built-in backup catalog like SQL Server.
        # We can only check WAL archiving status.
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("SELECT current_database()")
        db_name = cursor.fetchone()[0]

        cursor.execute("""
            SELECT
                last_archived_wal,
                last_archived_time,
                last_failed_wal
            FROM pg_stat_archiver
        """)
        row = cursor.fetchone()
        conn.close()

        if row and row[1]:
            from datetime import datetime, timezone

            last_time = row[1]
            if hasattr(last_time, "tzinfo") and last_time.tzinfo:
                now = datetime.now(timezone.utc)
            else:
                now = datetime.now(timezone.utc).replace(tzinfo=None)
            days = (now - last_time).days

            return [
                BackupInfo(
                    database_name=db_name,
                    last_backup_date=str(last_time),
                    backup_type="WAL Archive",
                    days_since_backup=days,
                )
            ]

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
                n.nspname AS schema_name,
                p.proname AS proc_name,
                CASE p.prokind
                    WHEN 'p' THEN 'PROCEDURE'
                    WHEN 'f' THEN 'FUNCTION'
                    ELSE 'FUNCTION'
                END AS type,
                LENGTH(pg_get_functiondef(p.oid)) AS definition_length,
                CASE WHEN pg_get_functiondef(p.oid) LIKE '%%EXECUTE%%'
                          OR pg_get_functiondef(p.oid) LIKE '%%EXEC%%'
                     THEN TRUE ELSE FALSE END AS has_dynamic_sql
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                AND p.prokind IN ('f', 'p')
            ORDER BY p.proname
        """)

        procedures = [
            StoredProcedureInfo(
                schema_name=row[0],
                name=row[1],
                type=row[2],
                definition_length=row[3] or 0,
                has_dynamic_sql=bool(row[4]),
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return procedures
