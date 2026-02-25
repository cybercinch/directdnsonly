"""Tests for directdnsonly.app.da.client — DirectAdminClient."""

import requests.exceptions
from unittest.mock import MagicMock, patch

from directdnsonly.app.da import DirectAdminClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_json_response(domains_list, total_pages=1):
    data = {str(i): {"domain": d} for i, d in enumerate(domains_list)}
    data["info"] = {"total_pages": total_pages}
    mock = MagicMock()
    mock.status_code = 200
    mock.is_redirect = False
    mock.headers = {"Content-Type": "application/json"}
    mock.json.return_value = data
    mock.raise_for_status = MagicMock()
    return mock


def _client():
    return DirectAdminClient(
        "da1.example.com", 2222, "admin", "secret", ssl=True, verify_ssl=True
    )


# ---------------------------------------------------------------------------
# list_domains — JSON happy path
# ---------------------------------------------------------------------------


def test_list_domains_returns_set_from_json():
    mock_resp = _make_json_response(["example.com", "test.com"])

    with patch("requests.get", return_value=mock_resp):
        result = _client().list_domains()

    assert result == {"example.com", "test.com"}


def test_list_domains_paginates():
    page1 = _make_json_response(["a.com"], total_pages=2)
    page2 = _make_json_response(["b.com"], total_pages=2)

    with patch("requests.get", side_effect=[page1, page2]):
        result = _client().list_domains()

    assert result == {"a.com", "b.com"}


# ---------------------------------------------------------------------------
# list_domains — DA Evo session login fallback
# ---------------------------------------------------------------------------


def test_redirect_triggers_session_login():
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.is_redirect = True

    client = _client()
    with (
        patch("requests.get", return_value=redirect_resp),
        patch.object(client, "_login", return_value=False),
    ):
        result = client.list_domains()

    assert result is None


def test_persistent_redirect_after_login_returns_none():
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.is_redirect = True

    client = _client()
    # Simulate cookies already set (login succeeded previously)
    client._cookies = {"session": "abc"}

    with patch("requests.get", return_value=redirect_resp):
        result = client.list_domains()

    assert result is None


# ---------------------------------------------------------------------------
# list_domains — error cases
# ---------------------------------------------------------------------------


def test_html_response_returns_none():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_redirect = False
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp):
        result = _client().list_domains()

    assert result is None


def test_connection_error_returns_none():
    with patch(
        "requests.get", side_effect=requests.exceptions.ConnectionError("refused")
    ):
        result = _client().list_domains()

    assert result is None


def test_timeout_returns_none():
    with patch("requests.get", side_effect=requests.exceptions.Timeout()):
        result = _client().list_domains()

    assert result is None


def test_ssl_error_returns_none():
    with patch(
        "requests.get", side_effect=requests.exceptions.SSLError("cert verify failed")
    ):
        result = _client().list_domains()

    assert result is None


# ---------------------------------------------------------------------------
# _parse_legacy_domain_list
# ---------------------------------------------------------------------------


def test_parse_standard_querystring():
    result = DirectAdminClient._parse_legacy_domain_list(
        "list[]=example.com&list[]=test.com"
    )
    assert result == {"example.com", "test.com"}


def test_parse_newline_separated():
    result = DirectAdminClient._parse_legacy_domain_list(
        "list[]=example.com\nlist[]=test.com"
    )
    assert result == {"example.com", "test.com"}


def test_parse_empty_body_returns_empty_set():
    assert DirectAdminClient._parse_legacy_domain_list("") == set()


def test_parse_normalises_to_lowercase():
    result = DirectAdminClient._parse_legacy_domain_list("list[]=EXAMPLE.COM")
    assert "example.com" in result
    assert "EXAMPLE.COM" not in result


def test_parse_strips_whitespace():
    result = DirectAdminClient._parse_legacy_domain_list("list[]= example.com ")
    assert "example.com" in result


# ---------------------------------------------------------------------------
# _login
# ---------------------------------------------------------------------------


def test_login_stores_cookies_on_success():
    mock_resp = MagicMock()
    mock_resp.cookies = {"session": "tok123"}

    client = _client()
    with patch("requests.post", return_value=mock_resp):
        result = client._login()

    assert result is True
    assert client._cookies == {"session": "tok123"}


def test_login_returns_false_when_no_cookies():
    mock_resp = MagicMock()
    mock_resp.cookies = {}

    client = _client()
    with patch("requests.post", return_value=mock_resp):
        result = client._login()

    assert result is False
    assert client._cookies is None


def test_login_returns_false_on_exception():
    client = _client()
    with patch("requests.post", side_effect=requests.exceptions.ConnectionError()):
        result = client._login()

    assert result is False
