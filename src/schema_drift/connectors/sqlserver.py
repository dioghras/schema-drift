"""SQL Server connector — queries system views for schema, indexes, performance."""

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


class SQLServerConnector(BaseDBConnector):
    def db_type(self) -> str:
        return "sqlserver"

    def _connect(self):
        import pymssql

        # Parse ADO-style connection string: Server=X;Database=Y;User=Z;Password=W
        params = {}
        for part in self.connection_string.split(";"):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                params[key.strip().lower()] = value.strip()

        return pymssql.connect(
            server=params.get("server", params.get("data source", "localhost")),
            port=params.get("port", "1433"),
            user=params.get("user", params.get("user id", params.get("uid", "sa"))),
            password=params.get("password", params.get("pwd", "")),
            database=params.get("database", params.get("initial catalog", "")),
        )

    def test_connection(self) -> bool:
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            conn.close()
            return True
        except Exception as e:
            logger.error(f"SQL Server connection test failed: {e}")
            return False

    def get_tables(self) -> list[LiveTableInfo]:
        conn = self._connect()
        cursor = conn.cursor(as_dict=True)

        cursor.execute("""
            SELECT
                t.TABLE_SCHEMA,
                t.TABLE_NAME,
                (SELECT SUM(p.rows) FROM sys.partitions p
                 JOIN sys.tables st ON p.object_id = st.object_id
                 WHERE st.name = t.TABLE_NAME AND p.index_id IN (0, 1)) AS row_count
            FROM INFORMATION_SCHEMA.TABLES t
            WHERE t.TABLE_TYPE = 'BASE TABLE'
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
                    CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_pk
                FROM INFORMATION_SCHEMA.COLUMNS c
                LEFT JOIN (
                    SELECT ku.TABLE_NAME, ku.COLUMN_NAME
                    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                        ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                    WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
                ) pk ON c.TABLE_NAME = pk.TABLE_NAME AND c.COLUMN_NAME = pk.COLUMN_NAME
                WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s
                ORDER BY c.ORDINAL_POSITION
            """, (schema_name, table_name))

            columns = [
                LiveColumnInfo(
                    name=col["COLUMN_NAME"],
                    data_type=col["DATA_TYPE"],
                    nullable=col["IS_NULLABLE"] == "YES",
                    max_length=col["CHARACTER_MAXIMUM_LENGTH"],
                    is_primary_key=bool(col["is_pk"]),
                )
                for col in cursor.fetchall()
            ]

            tables.append(
                LiveTableInfo(
                    schema_name=schema_name,
                    table_name=table_name,
                    columns=columns,
                    row_count=row.get("row_count"),
                )
            )

        conn.close()
        return tables

    def get_indexes(self) -> list[LiveIndexInfo]:
        conn = self._connect()
        cursor = conn.cursor(as_dict=True)

        cursor.execute("""
            SELECT
                OBJECT_NAME(i.object_id) AS table_name,
                i.name AS index_name,
                i.is_unique,
                COL_NAME(ic.object_id, ic.column_id) AS column_name,
                ISNULL(us.user_seeks, 0) AS user_seeks,
                ISNULL(us.user_scans, 0) AS user_scans,
                ISNULL(us.user_lookups, 0) AS user_lookups,
                ISNULL(us.user_updates, 0) AS user_updates
            FROM sys.indexes i
            JOIN sys.index_columns ic ON i.object_id = ic.object_id AND i.index_id = ic.index_id
            LEFT JOIN sys.dm_db_index_usage_stats us
                ON i.object_id = us.object_id AND i.index_id = us.index_id
            WHERE i.name IS NOT NULL
                AND OBJECTPROPERTY(i.object_id, 'IsUserTable') = 1
            ORDER BY table_name, index_name, ic.key_ordinal
        """)

        rows = cursor.fetchall()
        conn.close()

        # Group by index
        index_map: dict[str, LiveIndexInfo] = {}
        for row in rows:
            key = f"{row['table_name']}.{row['index_name']}"
            if key not in index_map:
                index_map[key] = LiveIndexInfo(
                    table_name=row["table_name"],
                    index_name=row["index_name"],
                    columns=[],
                    is_unique=bool(row["is_unique"]),
                    user_seeks=row["user_seeks"],
                    user_scans=row["user_scans"],
                    user_lookups=row["user_lookups"],
                    user_updates=row["user_updates"],
                )
            index_map[key].columns.append(row["column_name"])

        return list(index_map.values())

    def get_slow_queries(self, top_n: int = 20) -> list[SlowQuery]:
        conn = self._connect()
        cursor = conn.cursor(as_dict=True)

        cursor.execute("""
            SELECT TOP %s
                SUBSTRING(qt.text, 1, 500) AS query_text,
                qs.execution_count,
                qs.total_elapsed_time / qs.execution_count / 1000.0 AS avg_elapsed_ms,
                qs.max_elapsed_time / 1000.0 AS max_elapsed_ms,
                qs.total_worker_time / qs.execution_count / 1000.0 AS avg_cpu_ms,
                qs.total_logical_reads / qs.execution_count AS avg_logical_reads,
                qs.last_execution_time
            FROM sys.dm_exec_query_stats qs
            CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) qt
            WHERE qs.execution_count > 0
            ORDER BY avg_elapsed_ms DESC
        """, (top_n,))

        queries = [
            SlowQuery(
                query_text=row["query_text"],
                execution_count=row["execution_count"],
                avg_elapsed_ms=float(row["avg_elapsed_ms"]),
                max_elapsed_ms=float(row["max_elapsed_ms"]),
                avg_cpu_ms=float(row["avg_cpu_ms"]),
                avg_logical_reads=row["avg_logical_reads"],
                last_execution=str(row["last_execution_time"]) if row.get("last_execution_time") else None,
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return queries

    def get_backup_info(self) -> list[BackupInfo]:
        conn = self._connect()
        cursor = conn.cursor(as_dict=True)

        cursor.execute("""
            SELECT
                bs.database_name,
                MAX(bs.backup_finish_date) AS last_backup_date,
                bs.type AS backup_type,
                MAX(bs.backup_size) AS backup_size,
                DATEDIFF(DAY, MAX(bs.backup_finish_date), GETDATE()) AS days_since
            FROM msdb.dbo.backupset bs
            WHERE bs.database_name = DB_NAME()
            GROUP BY bs.database_name, bs.type
            ORDER BY last_backup_date DESC
        """)

        backups = [
            BackupInfo(
                database_name=row["database_name"],
                last_backup_date=str(row["last_backup_date"]) if row.get("last_backup_date") else None,
                backup_type={"D": "Full", "I": "Differential", "L": "Log"}.get(row.get("backup_type"), row.get("backup_type")),
                backup_size_bytes=row.get("backup_size"),
                days_since_backup=row.get("days_since"),
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return backups

    def get_stored_procedures(self) -> list[StoredProcedureInfo]:
        conn = self._connect()
        cursor = conn.cursor(as_dict=True)

        cursor.execute("""
            SELECT
                s.name AS schema_name,
                p.name AS proc_name,
                p.type_desc,
                p.create_date,
                p.modify_date,
                ISNULL(ps.execution_count, 0) AS execution_count,
                CASE WHEN ps.execution_count > 0
                     THEN ps.total_elapsed_time / ps.execution_count / 1000.0
                     ELSE 0 END AS avg_elapsed_ms,
                ISNULL(ps.max_elapsed_time / 1000.0, 0) AS max_elapsed_ms,
                ps.last_execution_time,
                LEN(ISNULL(m.definition, '')) AS definition_length,
                CASE WHEN m.definition LIKE '%%EXEC(%%' OR m.definition LIKE '%%EXECUTE(%%'
                          OR m.definition LIKE '%%sp_executesql%%'
                     THEN 1 ELSE 0 END AS has_dynamic_sql
            FROM sys.procedures p
            JOIN sys.schemas s ON p.schema_id = s.schema_id
            LEFT JOIN sys.dm_exec_procedure_stats ps
                ON p.object_id = ps.object_id
            LEFT JOIN sys.sql_modules m
                ON p.object_id = m.object_id
            ORDER BY execution_count DESC, p.name
        """)

        procedures = [
            StoredProcedureInfo(
                schema_name=row["schema_name"],
                name=row["proc_name"],
                type=row["type_desc"] or "PROCEDURE",
                created=str(row["create_date"]) if row.get("create_date") else None,
                last_modified=str(row["modify_date"]) if row.get("modify_date") else None,
                execution_count=row["execution_count"] or 0,
                avg_elapsed_ms=float(row["avg_elapsed_ms"]) if row["avg_elapsed_ms"] else 0.0,
                max_elapsed_ms=float(row["max_elapsed_ms"]) if row["max_elapsed_ms"] else 0.0,
                last_execution=str(row["last_execution_time"]) if row.get("last_execution_time") else None,
                definition_length=row["definition_length"] or 0,
                has_dynamic_sql=bool(row["has_dynamic_sql"]),
            )
            for row in cursor.fetchall()
        ]

        conn.close()
        return procedures
