"""Base schema collector and dataclasses for code-only DB analysis."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ColumnDefinition:
    name: str
    data_type: str
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_key_table: str | None = None
    max_length: int | None = None


@dataclass
class IndexDefinition:
    name: str | None
    table_name: str
    columns: list[str]
    is_unique: bool = False


@dataclass
class TableDefinition:
    name: str
    columns: list[ColumnDefinition] = field(default_factory=list)
    indexes: list[IndexDefinition] = field(default_factory=list)
    source_file: str = ""


@dataclass
class MigrationInfo:
    migration_id: str
    file_path: str
    timestamp: str | None = None
    operations: list[str] = field(default_factory=list)


@dataclass
class SchemaSnapshot:
    tables: list[TableDefinition] = field(default_factory=list)
    migrations: list[MigrationInfo] = field(default_factory=list)
    orm_type: str = ""
    raw_files_parsed: int = 0


class BaseSchemaCollector(ABC):
    @abstractmethod
    def orm_type(self) -> str:
        """Return ORM type identifier."""

    @abstractmethod
    def collect_schema(self, project_path: str) -> SchemaSnapshot:
        """Parse entity models and migrations from project source code."""

    @abstractmethod
    def entity_file_patterns(self) -> list[str]:
        """Glob patterns for entity/model files."""

    @abstractmethod
    def migration_file_patterns(self) -> list[str]:
        """Glob patterns for migration files."""

    def _find_files(self, project_path: str, patterns: list[str]) -> list[Path]:
        """Find files matching any of the given glob patterns."""
        root = Path(project_path)
        exclude_dirs = {
            ".git", "node_modules", "bin", "obj", "__pycache__",
            ".vs", ".vscode", "dist", "build", "venv", ".venv",
        }
        files = []
        for pattern in patterns:
            for path in root.rglob(pattern):
                if not path.is_file():
                    continue
                if any(part in exclude_dirs for part in path.relative_to(root).parts):
                    continue
                files.append(path)
        return sorted(set(files))
