"""Eloquent (Laravel) collector — the no-argument column helpers.

Laravel's most common column declarations take no column name: $table->id(),
->timestamps(), ->softDeletes(), ->rememberToken(). The parser required a
quoted name inside the parentheses, so all of them were skipped — including
the primary key of essentially every Laravel table.
"""

import pytest

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP

PHP = """<?php
use Illuminate\\Database\\Migrations\\Migration;

class CreateUsersTable extends Migration
{
    public function up()
    {
        Schema::create('users', function (Blueprint $table) {
            $table->id();
            $table->string('email', 255);
            $table->string('nickname')->nullable();
            $table->foreignId('account_id');
            $table->rememberToken();
            $table->softDeletes();
            $table->timestamps();
        });
    }
}
"""

PLAIN = """<?php
class CreateNotesTable extends Migration
{
    public function up()
    {
        Schema::create('notes', function (Blueprint $table) {
            $table->uuid('id');
            $table->text('body');
        });
    }
}
"""


@pytest.fixture
def tables(tmp_path):
    migrations = tmp_path / "database" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "2024_03_01_120000_create_users_table.php").write_text(PHP)
    (migrations / "2024_03_02_120000_create_notes_table.php").write_text(PLAIN)
    snapshot = SCHEMA_COLLECTOR_MAP["eloquent"]().collect_schema(str(tmp_path))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


class TestNoArgumentHelpers:
    def test_id_creates_the_primary_key(self, tables):
        pk = tables["users"]["id"]
        assert pk.is_primary_key is True
        assert pk.nullable is False

    def test_timestamps_creates_both_columns(self, tables):
        users = tables["users"]
        assert "created_at" in users
        assert "updated_at" in users

    def test_laravel_timestamps_are_nullable(self, tables):
        assert tables["users"]["created_at"].nullable is True

    def test_soft_deletes_creates_deleted_at(self, tables):
        col = tables["users"]["deleted_at"]
        assert col.nullable is True

    def test_remember_token_is_snake_cased(self, tables):
        users = tables["users"]
        assert "remember_token" in users
        assert users["remember_token"].nullable is True

    def test_tables_without_the_helpers_do_not_gain_columns(self, tables):
        notes = tables["notes"]
        assert "created_at" not in notes
        assert "deleted_at" not in notes


class TestNamedColumns:
    def test_explicit_columns_are_kept(self, tables):
        assert "email" in tables["users"]
        assert "body" in tables["notes"]

    def test_length_argument_becomes_max_length(self, tables):
        assert tables["users"]["email"].max_length == 255

    def test_nullable_chain_is_detected(self, tables):
        assert tables["users"]["nickname"].nullable is True

    def test_plain_column_is_not_null(self, tables):
        assert tables["users"]["email"].nullable is False

    def test_foreign_id_is_a_foreign_key(self, tables):
        assert tables["users"]["account_id"].is_foreign_key is True

    def test_named_uuid_is_still_a_primary_key(self, tables):
        assert tables["notes"]["id"].is_primary_key is True

    def test_no_duplicate_columns(self, tables):
        for name, cols in tables.items():
            assert len(cols) == len(set(cols)), name
