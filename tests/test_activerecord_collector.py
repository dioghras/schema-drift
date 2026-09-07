"""ActiveRecord collector — implicitly created columns.

Rails writes columns into the database that never appear as a `t.<type>` line
in schema.rb: the primary key that `create_table` adds on its own, and the
pair that `t.timestamps` expands to. Reading only the explicit lines leaves
them out of the code-side schema, so a correct database looks like it has
extra columns — and generating a migration for an empty database would
produce a table with no primary key at all.
"""

import pytest

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP

SCHEMA = """
ActiveRecord::Schema[7.1].define(version: 2024_03_01_120000) do
  create_table "users", force: :cascade do |t|
    t.string "email", limit: 255, null: false
    t.string "nickname"
    t.bigint "account_id"
    t.timestamps
  end

  create_table "legacy", id: false, force: :cascade do |t|
    t.string "code", null: false
  end

  create_table "events", primary_key: "uuid", force: :cascade do |t|
    t.string "kind"
  end
end
"""


@pytest.fixture
def tables(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    (db / "schema.rb").write_text(SCHEMA)
    snapshot = SCHEMA_COLLECTOR_MAP["activerecord"]().collect_schema(str(tmp_path))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


class TestImplicitPrimaryKey:
    def test_create_table_adds_an_id(self, tables):
        pk = tables["users"]["id"]
        assert pk.is_primary_key is True
        assert pk.nullable is False

    def test_id_false_suppresses_it(self, tables):
        assert "id" not in tables["legacy"]

    def test_custom_primary_key_name_is_honoured(self, tables):
        events = tables["events"]
        assert "id" not in events
        assert events["uuid"].is_primary_key is True


class TestTimestamps:
    def test_t_timestamps_expands_to_two_columns(self, tables):
        users = tables["users"]
        assert "created_at" in users
        assert "updated_at" in users

    def test_timestamps_are_not_null(self, tables):
        # Rails 5+ makes t.timestamps NOT NULL.
        assert tables["users"]["created_at"].nullable is False

    def test_tables_without_timestamps_do_not_gain_them(self, tables):
        assert "created_at" not in tables["legacy"]


class TestExplicitColumns:
    def test_null_false_is_respected(self, tables):
        assert tables["users"]["email"].nullable is False

    def test_plain_column_is_nullable(self, tables):
        assert tables["users"]["nickname"].nullable is True

    def test_limit_becomes_max_length(self, tables):
        assert tables["users"]["email"].max_length == 255

    def test_id_suffix_is_a_foreign_key(self, tables):
        assert tables["users"]["account_id"].is_foreign_key is True

    def test_every_table_is_found(self, tables):
        assert set(tables) == {"users", "legacy", "events"}
