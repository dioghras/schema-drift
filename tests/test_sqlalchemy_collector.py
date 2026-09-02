"""SQLAlchemy collector — nullability inference from source.

Regression cover for the case where a 2.0-style ``Mapped[str]`` column (NOT NULL)
was read as nullable, producing a false ``nullable_mismatch`` against a correct
database and an ``ALTER COLUMN ... DROP NOT NULL`` to "fix" it.
"""

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP

MODELS = '''
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    bio: Mapped[str | None] = mapped_column(Text)
    nickname: Mapped[Optional[str]] = mapped_column(String(50))
    settings: Mapped[dict[str, Any] | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    # explicit kwarg must win over the annotation, in both directions
    forced_nullable: Mapped[str] = mapped_column(String(10), nullable=True)
    forced_not_null: Mapped[str | None] = mapped_column(String(10), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))


class Legacy(Base):
    """1.x style — no annotation, so the PK heuristic is all we have."""

    __tablename__ = "legacy"

    id = Column(Integer, primary_key=True)
    label = Column(String(50))
'''


def _collect(tmp_path):
    (tmp_path / "models.py").write_text(MODELS)
    snapshot = SCHEMA_COLLECTOR_MAP["sqlalchemy"]().collect_schema(str(tmp_path))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


class TestNullabilityInference:
    def test_bare_annotation_is_not_null(self, tmp_path):
        cols = _collect(tmp_path)["users"]
        assert cols["email"].nullable is False
        assert cols["is_active"].nullable is False
        assert cols["created_at"].nullable is False

    def test_union_none_is_nullable(self, tmp_path):
        assert _collect(tmp_path)["users"]["bio"].nullable is True

    def test_optional_is_nullable(self, tmp_path):
        assert _collect(tmp_path)["users"]["nickname"].nullable is True

    def test_nested_brackets_before_none(self, tmp_path):
        # dict[str, Any] | None — the inner brackets must not truncate the match
        assert _collect(tmp_path)["users"]["settings"].nullable is True

    def test_explicit_kwarg_overrides_annotation(self, tmp_path):
        cols = _collect(tmp_path)["users"]
        assert cols["forced_nullable"].nullable is True
        assert cols["forced_not_null"].nullable is False

    def test_primary_key_not_nullable(self, tmp_path):
        col = _collect(tmp_path)["users"]["id"]
        assert col.is_primary_key is True
        assert col.nullable is False

    def test_foreign_key_still_parsed(self, tmp_path):
        col = _collect(tmp_path)["users"]["owner_id"]
        assert col.is_foreign_key is True
        assert col.foreign_key_table == "accounts"
        assert col.nullable is True

    def test_unannotated_column_falls_back_to_pk_heuristic(self, tmp_path):
        cols = _collect(tmp_path)["legacy"]
        assert cols["id"].nullable is False
        assert cols["label"].nullable is True
