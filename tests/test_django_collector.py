"""Django collector — table naming, nullability and foreign keys.

The table *name* matters more here than anywhere else in the project. Drift is
matched by name, so a collector that reports ``plain`` for a table Django
actually created as ``myapp_plain`` marks every table in the project as
missing, and the generator answers with ``CREATE TABLE`` for tables that
already exist. That is a louder version of the ``nullable_mismatch`` bug this
package was extracted after.
"""

import pytest

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP


def collect(root):
    snapshot = SCHEMA_COLLECTOR_MAP["django"]().collect_schema(str(root))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


def write_app(root, app, filename, body):
    app_dir = root / app
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / filename).write_text(body)
    return app_dir


MODELS = '''
from django.db import models


class UserProfile(models.Model):
    """Meta.db_table must win over every naming convention."""

    email = models.CharField(max_length=255)
    bio = models.TextField(null=True)

    class Meta:
        db_table = "custom_profiles"


class Plain(models.Model):
    """No Meta — Django names this <app_label>_<model>."""

    name = models.CharField(max_length=10)
    count = models.IntegerField(default=0)
    when = models.DateTimeField(null=True)
    ratio = models.FloatField(blank=True)


class Invoice(models.Model):
    account = models.ForeignKey("billing.Account", on_delete=models.CASCADE)
    owner = models.OneToOneField(Owner, on_delete=models.CASCADE)
    parent = models.ForeignKey("self", null=True, on_delete=models.SET_NULL)
    code = models.CharField(max_length=8, primary_key=True)


class AbstractBase(models.Model):
    """Abstract models produce no table at all."""

    shared = models.CharField(max_length=5)

    class Meta:
        abstract = True
'''


@pytest.fixture
def cols(tmp_path):
    write_app(tmp_path, "myapp", "models.py", MODELS)
    return collect(tmp_path)


class TestTableNaming:
    def test_db_table_meta_wins(self, cols):
        assert "custom_profiles" in cols
        assert "userprofile" not in cols

    def test_default_name_is_app_prefixed(self, cols):
        # Django's documented default is <app_label>_<modelname>.
        assert "myapp_plain" in cols
        assert "plain" not in cols

    def test_app_label_comes_from_the_package_directory(self, tmp_path):
        write_app(tmp_path, "billing", "models.py", "class Fee(models.Model):\n    x = models.IntegerField()\n")
        assert "billing_fee" in collect(tmp_path)

    def test_models_package_uses_the_app_not_the_module(self, tmp_path):
        pkg = tmp_path / "shipping" / "models"
        pkg.mkdir(parents=True)
        (pkg / "crate.py").write_text("class Crate(models.Model):\n    x = models.IntegerField()\n")
        # shipping/models/crate.py -> app_label is "shipping", never "models".
        tables = collect(tmp_path)
        assert "shipping_crate" in tables
        assert "models_crate" not in tables

    def test_abstract_models_produce_no_table(self, cols):
        assert not any("abstractbase" in name for name in cols)


class TestNullability:
    def test_fields_are_not_null_by_default(self, cols):
        plain = cols["myapp_plain"]
        assert plain["name"].nullable is False
        assert plain["count"].nullable is False

    def test_null_true_is_nullable(self, cols):
        assert cols["myapp_plain"]["when"].nullable is True

    def test_blank_does_not_affect_nullability(self, cols):
        # blank= is a form-validation flag; only null= reaches the database.
        assert cols["myapp_plain"]["ratio"].nullable is False


class TestPrimaryKeys:
    def test_implicit_id_is_added(self, cols):
        pk = cols["myapp_plain"]["id"]
        assert pk.is_primary_key is True
        assert pk.nullable is False

    def test_explicit_pk_suppresses_the_implicit_id(self, cols):
        invoice = cols["myapp_invoice"]
        assert "id" not in invoice
        assert invoice["code"].is_primary_key is True


class TestForeignKeys:
    def test_column_gets_the_id_suffix(self, cols):
        assert "account_id" in cols["myapp_invoice"]

    def test_qualified_target_resolves_to_the_referenced_table(self, cols):
        # "billing.Account" is app_label.ModelName -> table billing_account.
        # Reading the app label as the table is the bug this pins.
        col = cols["myapp_invoice"]["account_id"]
        assert col.is_foreign_key is True
        assert col.foreign_key_table == "billing_account"

    def test_bare_target_resolves_within_the_same_app(self, cols):
        col = cols["myapp_invoice"]["owner_id"]
        assert col.is_foreign_key is True
        assert col.foreign_key_table == "myapp_owner"

    def test_self_reference_points_at_its_own_table(self, cols):
        col = cols["myapp_invoice"]["parent_id"]
        assert col.foreign_key_table == "myapp_invoice"
        assert col.nullable is True


class TestTypesAndLengths:
    def test_max_length_is_captured(self, cols):
        assert cols["custom_profiles"]["email"].max_length == 255

    def test_type_mapping(self, cols):
        plain = cols["myapp_plain"]
        assert plain["name"].data_type == "String"
        assert plain["count"].data_type == "Integer"
        assert plain["when"].data_type == "DateTime"
        assert cols["custom_profiles"]["bio"].data_type == "Text"
