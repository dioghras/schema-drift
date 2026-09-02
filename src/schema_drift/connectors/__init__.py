from schema_drift.connectors.base import BaseDBConnector
from schema_drift.connectors.mysql import MySQLConnector
from schema_drift.connectors.postgresql import PostgreSQLConnector
from schema_drift.connectors.sqlserver import SQLServerConnector

CONNECTOR_MAP: dict[str, type[BaseDBConnector]] = {
    "sqlserver": SQLServerConnector,
    "postgresql": PostgreSQLConnector,
    "mysql": MySQLConnector,
}

__all__ = [
    "BaseDBConnector",
    "MySQLConnector",
    "PostgreSQLConnector",
    "SQLServerConnector",
    "CONNECTOR_MAP",
]
