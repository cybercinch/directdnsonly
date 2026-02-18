"""Tests for directdnsonly.app.api.admin — DNSAdminAPI handler methods."""
import pytest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import cherrypy

from directdnsonly.app.api.admin import DNSAdminAPI


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def save_queue():
    return MagicMock()


@pytest.fixture
def delete_queue():
    return MagicMock()


@pytest.fixture
def api(save_queue, delete_queue):
    return DNSAdminAPI(save_queue, delete_queue, backend_registry=MagicMock())


# ---------------------------------------------------------------------------
# CMD_API_LOGIN_TEST
# ---------------------------------------------------------------------------


def test_login_test_returns_success(api):
    result = api.CMD_API_LOGIN_TEST()
    parsed = parse_qs(result)
    assert parsed["error"] == ["0"]
    assert parsed["text"] == ["Login OK"]


# ---------------------------------------------------------------------------
# _handle_exists — GET action=exists
# ---------------------------------------------------------------------------


def test_handle_exists_missing_domain_returns_error(api):
    with patch.object(cherrypy, "response", MagicMock()):
        result = api._handle_exists({"action": "exists"})
    parsed = parse_qs(result)
    assert parsed["error"] == ["1"]


def test_handle_exists_unsupported_action_returns_error(api):
    with patch.object(cherrypy, "response", MagicMock()):
        result = api._handle_exists({"action": "rawsave"})
    parsed = parse_qs(result)
    assert parsed["error"] == ["1"]


def test_handle_exists_domain_not_found(api):
    with patch("directdnsonly.app.api.admin.check_zone_exists", return_value=False), \
         patch("directdnsonly.app.api.admin.check_parent_domain_owner", return_value=False):
        result = api._handle_exists({"action": "exists", "domain": "example.com"})

    parsed = parse_qs(result)
    assert parsed["error"] == ["0"]
    assert parsed["exists"] == ["0"]


def test_handle_exists_domain_found(api):
    record = MagicMock()
    record.hostname = "da1.example.com"

    with patch("directdnsonly.app.api.admin.check_zone_exists", return_value=True), \
         patch("directdnsonly.app.api.admin.get_domain_record", return_value=record):
        result = api._handle_exists({"action": "exists", "domain": "example.com"})

    parsed = parse_qs(result)
    assert parsed["error"] == ["0"]
    assert parsed["exists"] == ["1"]
    assert "da1.example.com" in parsed["details"][0]


def test_handle_exists_parent_found(api):
    parent = MagicMock()
    parent.hostname = "da2.example.com"

    with patch("directdnsonly.app.api.admin.check_zone_exists", return_value=False), \
         patch("directdnsonly.app.api.admin.check_parent_domain_owner", return_value=True), \
         patch("directdnsonly.app.api.admin.get_parent_domain_record", return_value=parent):
        result = api._handle_exists({
            "action": "exists",
            "domain": "sub.example.com",
            "check_for_parent_domain": "1",
        })

    parsed = parse_qs(result)
    assert parsed["error"] == ["0"]
    assert parsed["exists"] == ["2"]
    assert "da2.example.com" in parsed["details"][0]


def test_handle_exists_no_parent_check_when_flag_absent(api):
    """check_parent_domain_owner should not be called if flag not set."""
    record = MagicMock()
    record.hostname = "da1.example.com"

    with patch("directdnsonly.app.api.admin.check_zone_exists", return_value=True), \
         patch("directdnsonly.app.api.admin.check_parent_domain_owner") as mock_parent, \
         patch("directdnsonly.app.api.admin.get_domain_record", return_value=record):
        api._handle_exists({"action": "exists", "domain": "example.com"})

    mock_parent.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_rawsave
# ---------------------------------------------------------------------------

SAMPLE_ZONE = "$ORIGIN example.com.\n$TTL 300\nexample.com. 300 IN A 1.2.3.4\n"


def test_rawsave_enqueues_item(api, save_queue):
    with patch("directdnsonly.app.api.admin.validate_and_normalize_zone",
               return_value=SAMPLE_ZONE), \
         patch.object(cherrypy, "request", MagicMock(remote=MagicMock(ip="127.0.0.1"))):
        result = api._handle_rawsave("example.com", {
            "zone_file": SAMPLE_ZONE,
            "hostname": "da1.example.com",
            "username": "admin",
        })

    save_queue.put.assert_called_once()
    item = save_queue.put.call_args[0][0]
    assert item["domain"] == "example.com"
    assert item["hostname"] == "da1.example.com"
    assert item["username"] == "admin"
    assert item["client_ip"] == "127.0.0.1"

    parsed = parse_qs(result)
    assert parsed["error"] == ["0"]


def test_rawsave_missing_zone_file_raises(api):
    with patch.object(cherrypy, "request", MagicMock(remote=MagicMock(ip="127.0.0.1"))):
        with pytest.raises(ValueError, match="Missing zone file"):
            api._handle_rawsave("example.com", {})


def test_rawsave_invalid_zone_raises(api):
    with patch("directdnsonly.app.api.admin.validate_and_normalize_zone",
               side_effect=ValueError("Invalid zone data: bad record")), \
         patch.object(cherrypy, "request", MagicMock(remote=MagicMock(ip="127.0.0.1"))):
        with pytest.raises(ValueError, match="Invalid zone data"):
            api._handle_rawsave("example.com", {"zone_file": "garbage"})


# ---------------------------------------------------------------------------
# _handle_delete
# ---------------------------------------------------------------------------


def test_delete_enqueues_item(api, delete_queue):
    with patch.object(cherrypy, "request", MagicMock(remote=MagicMock(ip="10.0.0.1"))):
        result = api._handle_delete("example.com", {
            "hostname": "da1.example.com",
            "username": "admin",
        })

    delete_queue.put.assert_called_once()
    item = delete_queue.put.call_args[0][0]
    assert item["domain"] == "example.com"
    assert item["hostname"] == "da1.example.com"
    assert item["client_ip"] == "10.0.0.1"

    parsed = parse_qs(result)
    assert parsed["error"] == ["0"]


def test_delete_missing_params_uses_empty_strings(api, delete_queue):
    with patch.object(cherrypy, "request", MagicMock(remote=MagicMock(ip="127.0.0.1"))):
        api._handle_delete("example.com", {})

    item = delete_queue.put.call_args[0][0]
    assert item["hostname"] == ""
    assert item["username"] == ""
