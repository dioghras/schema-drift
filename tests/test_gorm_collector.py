"""GORM collector — untagged fields, pluralisation and primary keys.

The parser required a struct tag on every field, so a field with only a
`json:` tag — or no tag at all — was dropped even though GORM maps it as a
column. And table names were pluralised by appending "s", which produces
"categorys" and "persons" for tables GORM calls "categories" and "people".
"""

import pytest

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP

GO = """
package models

type Category struct {
\tID        uint   `gorm:"primaryKey"`
\tName      string `gorm:"column:title;not null"`
\tSlug      string `json:"slug"`
\tUntagged  string
\tOwnerID   uint   `gorm:"index"`
\tCreatedAt time.Time
}

type Person struct {
\tID   uint   `gorm:"primaryKey"`
\tName string `gorm:"not null"`
}

type Box struct {
\tID    uint `gorm:"primaryKey"`
\tLabel string
}

type Company struct {
\tID   uint `gorm:"primaryKey"`
\tName string
}

type Order struct {
\tID uint `gorm:"primaryKey"`
}

func (Order) TableName() string { return "custom_orders" }
"""


@pytest.fixture
def tables(tmp_path):
    (tmp_path / "models.go").write_text(GO)
    snapshot = SCHEMA_COLLECTOR_MAP["gorm"]().collect_schema(str(tmp_path))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


class TestTableNaming:
    def test_tablename_override_wins(self, tables):
        assert "custom_orders" in tables

    def test_y_becomes_ies(self, tables):
        assert "categories" in tables
        assert "categorys" not in tables

    def test_irregular_plural(self, tables):
        assert "people" in tables
        assert "persons" not in tables

    def test_sibilant_takes_es(self, tables):
        assert "boxes" in tables
        assert "boxs" not in tables

    def test_regular_plural_still_works(self, tables):
        assert "companies" in tables


class TestFieldDiscovery:
    def test_field_with_only_a_json_tag_is_a_column(self, tables):
        assert "slug" in tables["categories"]

    def test_field_with_no_tag_at_all_is_a_column(self, tables):
        assert "untagged" in tables["categories"]

    def test_gorm_column_tag_renames(self, tables):
        cat = tables["categories"]
        assert "title" in cat
        assert "name" not in cat

    def test_names_are_snake_cased(self, tables):
        assert "created_at" in tables["categories"]
        assert "owner_id" in tables["categories"]


class TestNullabilityAndKeys:
    def test_primary_key_is_not_nullable(self, tables):
        pk = tables["categories"]["id"]
        assert pk.is_primary_key is True
        assert pk.nullable is False

    def test_not_null_tag_is_respected(self, tables):
        assert tables["categories"]["title"].nullable is False

    def test_plain_field_is_nullable(self, tables):
        assert tables["categories"]["untagged"].nullable is True

    def test_id_suffix_is_a_foreign_key(self, tables):
        assert tables["categories"]["owner_id"].is_foreign_key is True
