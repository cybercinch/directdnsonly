"""Tests for directdnsonly.app.utils.zone_parser."""
import pytest
from dns.exception import DNSException

from directdnsonly.app.utils.zone_parser import (
    count_zone_records,
    validate_and_normalize_zone,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ZONE = "example.com. 300 IN A 1.2.3.4"

FULL_ZONE = """\
$ORIGIN example.com.
$TTL 300
@ IN SOA ns1.example.com. admin.example.com. 2024010101 3600 900 604800 300
@ IN NS ns1.example.com.
@ IN A 1.2.3.4
www IN A 5.6.7.8
mail IN MX 10 mail.example.com.
"""


# ---------------------------------------------------------------------------
# validate_and_normalize_zone
# ---------------------------------------------------------------------------


def test_validate_adds_origin_when_missing():
    result = validate_and_normalize_zone(MINIMAL_ZONE, "example.com")
    assert "$ORIGIN example.com." in result


def test_validate_adds_ttl_when_missing():
    result = validate_and_normalize_zone(MINIMAL_ZONE, "example.com")
    assert "$TTL" in result


def test_validate_does_not_duplicate_origin():
    zone = "$ORIGIN example.com.\nexample.com. 300 IN A 1.2.3.4"
    result = validate_and_normalize_zone(zone, "example.com")
    assert result.count("$ORIGIN") == 1


def test_validate_does_not_duplicate_ttl():
    zone = "$TTL 300\nexample.com. 300 IN A 1.2.3.4"
    result = validate_and_normalize_zone(zone, "example.com")
    assert result.count("$TTL") == 1


def test_validate_appends_dot_to_domain():
    result = validate_and_normalize_zone(MINIMAL_ZONE, "example.com")
    assert "$ORIGIN example.com." in result


def test_validate_returns_string():
    result = validate_and_normalize_zone(MINIMAL_ZONE, "example.com")
    assert isinstance(result, str)


def test_validate_full_zone_passes():
    result = validate_and_normalize_zone(FULL_ZONE, "example.com")
    assert result is not None


def test_validate_raises_on_invalid_zone():
    bad_zone = "this is not a zone file at all !!!"
    with pytest.raises(ValueError, match="Invalid zone data"):
        validate_and_normalize_zone(bad_zone, "example.com")


# ---------------------------------------------------------------------------
# count_zone_records
# ---------------------------------------------------------------------------


def test_count_records_simple_zone():
    zone = "$ORIGIN example.com.\n$TTL 300\n@ IN A 1.2.3.4\n@ IN AAAA ::1\n"
    count = count_zone_records(zone, "example.com")
    assert count == 2


def test_count_records_soa_included():
    count = count_zone_records(FULL_ZONE, "example.com")
    # SOA + NS + A (apex) + A (www) + MX = 5
    assert count == 5


def test_count_records_returns_negative_on_bad_zone():
    count = count_zone_records("not a valid zone", "example.com")
    assert count == -1


def test_count_records_empty_zone():
    zone = "$ORIGIN example.com.\n$TTL 300\n"
    count = count_zone_records(zone, "example.com")
    assert count == 0
