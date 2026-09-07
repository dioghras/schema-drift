"""Hibernate/JPA collector — column naming, attribute scoping, nullability.

Naming assumption, stated once: Spring Boot's default physical naming strategy
(CamelCaseToUnderscoresNamingStrategy) is applied, so an entity `UserAccount`
maps to `user_account` and a field `createdAt` to `created_at`. @Table and
@Column override it absolutely. Plain Hibernate without Spring Boot leaves
names as-is; @Table/@Column make that explicit, which real projects do.
"""

import pytest

from schema_drift.schema_collectors import SCHEMA_COLLECTOR_MAP

JAVA = """
package com.shop;
import jakarta.persistence.*;

@Entity
@Table(name = "app_users")
public class User {
    @Id
    @GeneratedValue
    private Long id;

    @Column(name = "email_address", nullable = false, length = 255)
    private String email;

    private String nickname;

    @Column(nullable = false)
    private String status;

    @Transient
    private String cachedToken;

    @ManyToOne
    @JoinColumn(name = "account_id")
    private Account account;

    @OneToMany
    private List<Order> orders;

    private String createdBy;
}

@Entity
public class AuditRecord {
    @Id
    private Long id;
    private String action;
}
"""


@pytest.fixture
def tables(tmp_path):
    (tmp_path / "User.java").write_text(JAVA)
    snapshot = SCHEMA_COLLECTOR_MAP["hibernate"]().collect_schema(str(tmp_path))
    return {t.name: {c.name: c for c in t.columns} for t in snapshot.tables}


class TestTableNaming:
    def test_table_annotation_wins(self, tables):
        assert "app_users" in tables

    def test_default_is_snake_case_not_pluralized(self, tables):
        # The old default appended "s" to the lowercased class name, inventing
        # "auditrecords" for a table Hibernate calls audit_record.
        assert "audit_record" in tables
        assert "auditrecords" not in tables

    def test_every_entity_in_a_file_is_collected(self, tables):
        assert len(tables) == 2


class TestColumnNaming:
    def test_column_annotation_renames(self, tables):
        user = tables["app_users"]
        assert "email_address" in user
        assert "email" not in user

    def test_unannotated_field_is_snake_cased(self, tables):
        assert "created_by" in tables["app_users"]

    def test_transient_fields_are_excluded(self, tables):
        assert "cached_token" not in tables["app_users"]
        assert "cachedToken" not in tables["app_users"]

    def test_collections_are_not_columns(self, tables):
        assert "orders" not in tables["app_users"]

    def test_join_column_names_the_foreign_key(self, tables):
        user = tables["app_users"]
        assert "account_id" in user
        assert "account" not in user
        assert user["account_id"].is_foreign_key is True


class TestNullability:
    def test_primary_key_is_never_nullable(self, tables):
        pk = tables["app_users"]["id"]
        assert pk.is_primary_key is True
        assert pk.nullable is False

    def test_nullable_false_is_respected(self, tables):
        assert tables["app_users"]["email_address"].nullable is False

    def test_plain_field_is_nullable(self, tables):
        # JPA's @Column(nullable) defaults to true, so an unannotated field is
        # nullable. It must not inherit the previous field's annotation.
        assert tables["app_users"]["nickname"].nullable is True


class TestAnnotationsDoNotBleed:
    def test_id_does_not_make_the_next_field_a_key(self, tables):
        assert tables["app_users"]["email_address"].is_primary_key is False

    def test_length_does_not_leak_to_the_next_field(self, tables):
        user = tables["app_users"]
        assert user["email_address"].max_length == 255
        assert user["nickname"].max_length is None

    def test_nullable_does_not_leak_backwards_or_forwards(self, tables):
        user = tables["app_users"]
        assert user["status"].nullable is False
        assert user["created_by"].nullable is True
