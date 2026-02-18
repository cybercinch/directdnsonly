"""Tests for directdnsonly.app.reconciler — ReconciliationWorker."""
import pytest
import requests.exceptions
from queue import Queue
from unittest.mock import MagicMock, patch

from directdnsonly.app.reconciler import ReconciliationWorker
from directdnsonly.app.db.models import Domain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SERVER = {"hostname": "da1.example.com", "port": 2222, "username": "admin", "password": "secret", "ssl": True}

BASE_CONFIG = {
    "enabled": True,
    "dry_run": False,
    "interval_minutes": 60,
    "verify_ssl": True,
    "ipp": 100,
    "directadmin_servers": [SERVER],
}


@pytest.fixture
def delete_queue():
    return Queue()


@pytest.fixture
def worker(delete_queue):
    return ReconciliationWorker(delete_queue, BASE_CONFIG)


@pytest.fixture
def dry_run_worker(delete_queue):
    cfg = {**BASE_CONFIG, "dry_run": True}
    return ReconciliationWorker(delete_queue, cfg)


# ---------------------------------------------------------------------------
# _reconcile_all — orphan detection
# ---------------------------------------------------------------------------


def test_orphan_queued_when_domain_missing_from_da(worker, delete_queue, patch_connect):
    patch_connect.add(Domain(domain="orphan.com", hostname="da1.example.com", username="admin"))
    patch_connect.commit()

    with patch.object(worker, "_fetch_da_domains", return_value=set()):
        worker._reconcile_all()

    assert not delete_queue.empty()
    item = delete_queue.get_nowait()
    assert item["domain"] == "orphan.com"
    assert item["source"] == "reconciler"


def test_orphan_not_queued_in_dry_run(dry_run_worker, delete_queue, patch_connect):
    patch_connect.add(Domain(domain="orphan.com", hostname="da1.example.com", username="admin"))
    patch_connect.commit()

    with patch.object(dry_run_worker, "_fetch_da_domains", return_value=set()):
        dry_run_worker._reconcile_all()

    assert delete_queue.empty()


def test_orphan_not_queued_for_unknown_server(worker, delete_queue, patch_connect):
    """Domains whose recorded master is NOT in our configured servers are skipped."""
    patch_connect.add(Domain(domain="other.com", hostname="da99.unknown.com", username="admin"))
    patch_connect.commit()

    with patch.object(worker, "_fetch_da_domains", return_value=set()):
        worker._reconcile_all()

    assert delete_queue.empty()


def test_active_domain_not_queued(worker, delete_queue, patch_connect):
    patch_connect.add(Domain(domain="good.com", hostname="da1.example.com", username="admin"))
    patch_connect.commit()

    with patch.object(worker, "_fetch_da_domains", return_value={"good.com"}):
        worker._reconcile_all()

    assert delete_queue.empty()


# ---------------------------------------------------------------------------
# _reconcile_all — hostname backfill and migration
# ---------------------------------------------------------------------------


def test_backfill_null_hostname(worker, patch_connect):
    patch_connect.add(Domain(domain="backfill.com", hostname=None, username="admin"))
    patch_connect.commit()

    with patch.object(worker, "_fetch_da_domains", return_value={"backfill.com"}):
        worker._reconcile_all()

    record = patch_connect.query(Domain).filter_by(domain="backfill.com").first()
    assert record.hostname == "da1.example.com"


def test_migration_updates_hostname(worker, patch_connect):
    patch_connect.add(Domain(domain="moved.com", hostname="da-old.example.com", username="admin"))
    patch_connect.commit()

    with patch.object(worker, "_fetch_da_domains", return_value={"moved.com"}):
        worker._reconcile_all()

    record = patch_connect.query(Domain).filter_by(domain="moved.com").first()
    assert record.hostname == "da1.example.com"


def test_dry_run_still_backfills(dry_run_worker, patch_connect):
    """Backfill is a data-repair operation, applied even in dry-run mode."""
    patch_connect.add(Domain(domain="fill.com", hostname=None, username="admin"))
    patch_connect.commit()

    with patch.object(dry_run_worker, "_fetch_da_domains", return_value={"fill.com"}):
        dry_run_worker._reconcile_all()

    record = patch_connect.query(Domain).filter_by(domain="fill.com").first()
    assert record.hostname == "da1.example.com"


# ---------------------------------------------------------------------------
# _fetch_da_domains — HTTP handling
# ---------------------------------------------------------------------------


def _make_json_response(domains_dict, total_pages=1):
    """Return a mock requests.Response with JSON payload matching DA format."""
    data = {str(i): {"domain": d} for i, d in enumerate(domains_dict)}
    data["info"] = {"total_pages": total_pages}
    mock = MagicMock()
    mock.status_code = 200
    mock.is_redirect = False
    mock.headers = {"Content-Type": "application/json"}
    mock.json.return_value = data
    mock.raise_for_status = MagicMock()
    return mock


def test_fetch_returns_domains_from_json(worker):
    mock_resp = _make_json_response(["example.com", "test.com"])

    with patch("requests.get", return_value=mock_resp):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result == {"example.com", "test.com"}


def test_fetch_paginates(worker):
    page1 = _make_json_response(["a.com"], total_pages=2)
    page2 = _make_json_response(["b.com"], total_pages=2)

    with patch("requests.get", side_effect=[page1, page2]):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result == {"a.com", "b.com"}


def test_fetch_redirect_triggers_session_login(worker):
    redirect_resp = MagicMock()
    redirect_resp.status_code = 302
    redirect_resp.is_redirect = True

    with patch("requests.get", return_value=redirect_resp), \
         patch.object(worker, "_da_session_login", return_value=None):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result is None


def test_fetch_html_response_returns_none(worker):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.is_redirect = False
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    mock_resp.raise_for_status = MagicMock()

    with patch("requests.get", return_value=mock_resp):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result is None


def test_fetch_connection_error_returns_none(worker):
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("refused")):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result is None


def test_fetch_timeout_returns_none(worker):
    with patch("requests.get", side_effect=requests.exceptions.Timeout()):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result is None


def test_fetch_ssl_error_returns_none(worker):
    with patch("requests.get", side_effect=requests.exceptions.SSLError("cert verify failed")):
        result = worker._fetch_da_domains("da1.example.com", 2222, "admin", "secret", True)

    assert result is None


# ---------------------------------------------------------------------------
# _parse_da_domain_list — legacy format fallback
# ---------------------------------------------------------------------------


def test_parse_standard_querystring():
    body = "list[]=example.com&list[]=test.com"
    result = ReconciliationWorker._parse_da_domain_list(body)
    assert result == {"example.com", "test.com"}


def test_parse_newline_separated():
    body = "list[]=example.com\nlist[]=test.com"
    result = ReconciliationWorker._parse_da_domain_list(body)
    assert result == {"example.com", "test.com"}


def test_parse_empty_body_returns_empty_set():
    assert ReconciliationWorker._parse_da_domain_list("") == set()


def test_parse_normalises_to_lowercase():
    result = ReconciliationWorker._parse_da_domain_list("list[]=EXAMPLE.COM")
    assert "example.com" in result
    assert "EXAMPLE.COM" not in result


def test_parse_strips_whitespace():
    result = ReconciliationWorker._parse_da_domain_list("list[]= example.com ")
    assert "example.com" in result


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------


def test_disabled_worker_does_not_start(delete_queue):
    cfg = {**BASE_CONFIG, "enabled": False}
    w = ReconciliationWorker(delete_queue, cfg)
    w.start()
    assert not w.is_alive


def test_no_servers_does_not_start(delete_queue):
    cfg = {**BASE_CONFIG, "directadmin_servers": []}
    w = ReconciliationWorker(delete_queue, cfg)
    w.start()
    assert not w.is_alive
