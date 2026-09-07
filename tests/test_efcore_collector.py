"""EF Core collector — class boundaries, attributes and column naming.

The C# parser had no notion of where a class ended. It took the first class in
a file, then scanned every property in the whole file into it, so a two-entity
file produced one table with both entities' columns and the second entity
disappeared. Attributes were attributed by a five-line lookback, which leaked
[Key] and [MaxLength] onto whichever property happened to follow.
"""

import pytest

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP

ENTITIES = """
using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Shop.Models
{
    [Table("categories")]
    public class Category
    {
        [Key]
        public int Id { get; set; }

        [Column("display_name")]
        [MaxLength(120)]
        public string Name { get; set; }

        public string? Notes { get; set; }

        [NotMapped]
        public string Computed { get; set; }

        public ICollection<Product> Products { get; set; }
    }

    public class Product
    {
        public int Id { get; set; }
        public string Title { get; set; }
        public decimal? Discount { get; set; }
        [Required]
        public string? Sku { get; set; }
        public int CategoryId { get; set; }
        public Category Category { get; set; }
    }

    public class Warehouse
    {
        public int Id { get; set; }
        public string Code { get; set; }
    }
}
"""

CONTEXT = """
public class ShopContext : DbContext
{
    public DbSet<Product> Products { get; set; }
}
"""


@pytest.fixture
def tables(tmp_path):
    (tmp_path / "Entities.cs").write_text(ENTITIES)
    (tmp_path / "ShopContext.cs").write_text(CONTEXT)
    snapshot = SCHEMA_COLLECTOR_MAP["efcore"]().collect_schema(str(tmp_path))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


class TestClassBoundaries:
    def test_every_entity_in_a_file_becomes_a_table(self, tables):
        assert set(tables) == {"categories", "Products", "Warehouse"}

    def test_columns_do_not_leak_between_classes(self, tables):
        assert "Title" not in tables["categories"]
        assert "Name" not in tables["Products"]

    def test_no_duplicate_columns(self, tmp_path):
        (tmp_path / "Entities.cs").write_text(ENTITIES)
        (tmp_path / "ShopContext.cs").write_text(CONTEXT)
        snapshot = SCHEMA_COLLECTOR_MAP["efcore"]().collect_schema(str(tmp_path))
        for table in snapshot.tables:
            names = [c.name for c in table.columns]
            assert len(names) == len(set(names)), f"{table.name}: {names}"


class TestTableNaming:
    def test_table_attribute_wins(self, tables):
        assert "categories" in tables
        assert "Category" not in tables

    def test_dbset_property_name_is_used(self, tables):
        # EF Core maps an entity to the DbSet property that exposes it.
        assert "Products" in tables

    def test_entity_without_dbset_keeps_its_class_name(self, tables):
        # EF Core does not pluralize; appending "s" invents a table.
        assert "Warehouse" in tables
        assert "Warehouses" not in tables


class TestAttributesDoNotBleed:
    def test_key_applies_only_to_its_own_property(self, tables):
        # "Name" is reachable as display_name here — [Column] renamed it.
        cat = tables["categories"]
        assert cat["Id"].is_primary_key is True
        assert cat["display_name"].is_primary_key is False

    def test_maxlength_applies_only_to_its_own_property(self, tables):
        cat = tables["categories"]
        assert cat["display_name"].max_length == 120
        assert cat["Notes"].max_length is None


class TestColumnNaming:
    def test_column_attribute_renames_the_column(self, tables):
        cat = tables["categories"]
        assert "display_name" in cat
        assert "Name" not in cat

    def test_not_mapped_properties_are_excluded(self, tables):
        assert "Computed" not in tables["categories"]


class TestNavigationProperties:
    def test_collections_are_not_columns(self, tables):
        assert "Products" not in tables["categories"]

    def test_reference_navigation_is_not_a_column(self, tables):
        # "public Category Category" is a navigation property; the column is
        # the CategoryId beside it.
        assert "Category" not in tables["Products"]

    def test_the_foreign_key_scalar_is_kept(self, tables):
        col = tables["Products"]["CategoryId"]
        assert col.is_foreign_key is True
        assert col.nullable is False


class TestNullability:
    def test_value_type_is_not_null(self, tables):
        assert tables["Products"]["Id"].nullable is False

    def test_nullable_value_type(self, tables):
        assert tables["Products"]["Discount"].nullable is True

    def test_non_nullable_reference_type_is_not_null(self, tables):
        # Nullable reference types are on by default in modern .NET.
        assert tables["Products"]["Title"].nullable is False

    def test_nullable_reference_type(self, tables):
        assert tables["categories"]["Notes"].nullable is True

    def test_required_overrides_the_question_mark(self, tables):
        assert tables["Products"]["Sku"].nullable is False

    def test_data_type_drops_the_nullable_marker(self, tables):
        assert tables["Products"]["Discount"].data_type == "decimal"
