"""Tests for directdnsonly.app.utils — zone index helper functions."""

import pytest

from directdnsonly.app.db.models import Domain
from directdnsonly.app.utils import (
    check_zone_exists,
    check_parent_domain_owner,
    get_domain_record,
    get_parent_domain_record,
    put_zone_index,
)


# ---------------------------------------------------------------------------
# check_zone_exists
# ---------------------------------------------------------------------------


def test_check_zone_exists_not_found(patch_connect):
    assert check_zone_exists("example.com") is False


def test_check_zone_exists_found(patch_connect):
    patch_connect.add(
        Domain(domain="example.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    assert check_zone_exists("example.com") is True


def test_check_zone_exists_does_not_match_partial(patch_connect):
    patch_connect.add(
        Domain(domain="example.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    assert check_zone_exists("sub.example.com") is False


# ---------------------------------------------------------------------------
# put_zone_index
# ---------------------------------------------------------------------------


def test_put_zone_index_adds_record(patch_connect):
    put_zone_index("new.com", "da1.example.com", "admin")

    record = patch_connect.query(Domain).filter_by(domain="new.com").first()
    assert record is not None
    assert record.hostname == "da1.example.com"
    assert record.username == "admin"


def test_put_zone_index_stores_domain_name(patch_connect):
    put_zone_index("another.nz", "da2.example.com", "user1")

    assert check_zone_exists("another.nz") is True


# ---------------------------------------------------------------------------
# get_domain_record
# ---------------------------------------------------------------------------


def test_get_domain_record_returns_none_when_missing(patch_connect):
    assert get_domain_record("missing.com") is None


def test_get_domain_record_returns_record(patch_connect):
    patch_connect.add(
        Domain(domain="found.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    record = get_domain_record("found.com")
    assert record is not None
    assert record.domain == "found.com"
    assert record.hostname == "da1.example.com"


# ---------------------------------------------------------------------------
# check_parent_domain_owner
# ---------------------------------------------------------------------------


def test_check_parent_domain_owner_not_found(patch_connect):
    assert check_parent_domain_owner("sub.example.com") is False


def test_check_parent_domain_owner_found(patch_connect):
    patch_connect.add(
        Domain(domain="example.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    assert check_parent_domain_owner("sub.example.com") is True


def test_check_parent_domain_owner_single_label_returns_false(patch_connect):
    # A single-label name like "com" has no parent
    assert check_parent_domain_owner("com") is False


def test_check_parent_domain_owner_ignores_grandparent(patch_connect):
    # Only the immediate parent is checked, not grandparents
    patch_connect.add(
        Domain(domain="example.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    # deep.sub.example.com's immediate parent is sub.example.com (not in DB)
    assert check_parent_domain_owner("deep.sub.example.com") is False


# ---------------------------------------------------------------------------
# get_parent_domain_record
# ---------------------------------------------------------------------------


def test_get_parent_domain_record_returns_none_when_missing(patch_connect):
    assert get_parent_domain_record("sub.example.com") is None


def test_get_parent_domain_record_returns_parent(patch_connect):
    patch_connect.add(
        Domain(domain="example.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    parent = get_parent_domain_record("sub.example.com")
    assert parent is not None
    assert parent.domain == "example.com"


def test_get_parent_domain_record_single_label_returns_none(patch_connect):
    assert get_parent_domain_record("com") is None
