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


# ---------------------------------------------------------------------------
# get_extra_dns_servers
# ---------------------------------------------------------------------------


def _multi_server_get_resp(servers=None):
    mock = MagicMock()
    mock.status_code = 200
    mock.is_redirect = False
    mock.headers = {"Content-Type": "application/json"}
    mock.json.return_value = {"CLUSTER_ON": "yes", "servers": servers or {}}
    mock.raise_for_status = MagicMock()
    return mock


def test_get_extra_dns_servers_returns_servers_dict():
    servers = {
        "1.2.3.4": {"dns": "yes", "domain_check": "yes", "port": "2222", "ssl": "no"}
    }
    with patch("requests.get", return_value=_multi_server_get_resp(servers)):
        result = _client().get_extra_dns_servers()

    assert "1.2.3.4" in result
    assert result["1.2.3.4"]["dns"] == "yes"


def test_get_extra_dns_servers_returns_empty_on_http_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    with patch("requests.get", return_value=mock_resp):
        result = _client().get_extra_dns_servers()

    assert result == {}


def test_get_extra_dns_servers_returns_empty_on_connection_error():
    with patch(
        "requests.get", side_effect=requests.exceptions.ConnectionError("refused")
    ):
        result = _client().get_extra_dns_servers()

    assert result == {}


# ---------------------------------------------------------------------------
# add_extra_dns_server
# ---------------------------------------------------------------------------


def test_add_extra_dns_server_returns_true_on_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": "", "success": "Connection Added"}

    with patch("requests.post", return_value=mock_resp):
        result = _client().add_extra_dns_server("1.2.3.4", 2222, "ddnsonly", "s3cr3t")

    assert result is True


def test_add_extra_dns_server_returns_false_on_da_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": "Server already exists", "success": ""}

    with patch("requests.post", return_value=mock_resp):
        result = _client().add_extra_dns_server("1.2.3.4", 2222, "ddnsonly", "s3cr3t")

    assert result is False


def test_add_extra_dns_server_returns_false_on_connection_error():
    with patch(
        "requests.post", side_effect=requests.exceptions.ConnectionError("refused")
    ):
        result = _client().add_extra_dns_server("1.2.3.4", 2222, "ddnsonly", "s3cr3t")

    assert result is False


# ---------------------------------------------------------------------------
# ensure_extra_dns_server
# ---------------------------------------------------------------------------


def _add_success_resp():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"result": "", "success": "Connection Added"}
    return mock


def _save_success_resp():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"result": "", "success": "Connections Saved"}
    return mock


def test_ensure_extra_dns_server_adds_and_configures_new_server():
    """Server not yet registered — adds it, then saves dns+domain_check settings."""
    with (
        patch("requests.get", return_value=_multi_server_get_resp(servers={})),
        patch(
            "requests.post",
            side_effect=[_add_success_resp(), _save_success_resp()],
        ),
    ):
        result = _client().ensure_extra_dns_server(
            "1.2.3.4", 2222, "ddnsonly", "s3cr3t"
        )

    assert result is True


def test_ensure_extra_dns_server_skips_add_when_already_present():
    """Server already registered — no add call, only saves settings."""
    existing = {
        "1.2.3.4": {"dns": "no", "domain_check": "no", "port": "2222", "ssl": "no"}
    }
    with (
        patch("requests.get", return_value=_multi_server_get_resp(servers=existing)),
        patch("requests.post", return_value=_save_success_resp()) as mock_post,
    ):
        result = _client().ensure_extra_dns_server(
            "1.2.3.4", 2222, "ddnsonly", "s3cr3t"
        )

    assert result is True
    assert mock_post.call_count == 1  # save only, no add


def test_ensure_extra_dns_server_returns_false_when_add_fails():
    fail_resp = MagicMock()
    fail_resp.status_code = 200
    fail_resp.json.return_value = {"result": "error", "success": ""}

    with (
        patch("requests.get", return_value=_multi_server_get_resp(servers={})),
        patch("requests.post", return_value=fail_resp),
    ):
        result = _client().ensure_extra_dns_server(
            "1.2.3.4", 2222, "ddnsonly", "s3cr3t"
        )

    assert result is False


def test_ensure_extra_dns_server_returns_false_when_save_fails():
    """Add succeeds but the subsequent settings save fails."""
    fail_save = MagicMock()
    fail_save.status_code = 200
    fail_save.json.return_value = {"result": "error", "success": ""}

    with (
        patch("requests.get", return_value=_multi_server_get_resp(servers={})),
        patch(
            "requests.post",
            side_effect=[_add_success_resp(), fail_save],
        ),
    ):
        result = _client().ensure_extra_dns_server(
            "1.2.3.4", 2222, "ddnsonly", "s3cr3t"
        )

    assert result is False
