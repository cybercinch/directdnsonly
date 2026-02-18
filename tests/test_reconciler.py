"""Tests for directdnsonly.app.reconciler — ReconciliationWorker."""

import pytest
from queue import Queue
from unittest.mock import patch

from directdnsonly.app.reconciler import ReconciliationWorker
from directdnsonly.app.db.models import Domain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SERVER = {
    "hostname": "da1.example.com",
    "port": 2222,
    "username": "admin",
    "password": "secret",
    "ssl": True,
}

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
# Helpers
# ---------------------------------------------------------------------------

DA_CLIENT_PATH = "directdnsonly.app.reconciler.DirectAdminClient"


def _patch_da(return_value):
    """Patch DirectAdminClient so list_domains returns a fixed value."""
    return patch(DA_CLIENT_PATH, **{"return_value.list_domains.return_value": return_value})


# ---------------------------------------------------------------------------
# _reconcile_all — orphan detection
# ---------------------------------------------------------------------------


def test_orphan_queued_when_domain_missing_from_da(worker, delete_queue, patch_connect):
    patch_connect.add(
        Domain(domain="orphan.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da(set()):
        worker._reconcile_all()

    assert not delete_queue.empty()
    item = delete_queue.get_nowait()
    assert item["domain"] == "orphan.com"
    assert item["source"] == "reconciler"


def test_orphan_not_queued_in_dry_run(dry_run_worker, delete_queue, patch_connect):
    patch_connect.add(
        Domain(domain="orphan.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da(set()):
        dry_run_worker._reconcile_all()

    assert delete_queue.empty()


def test_orphan_not_queued_for_unknown_server(worker, delete_queue, patch_connect):
    """Domains whose recorded master is NOT in our configured servers are skipped."""
    patch_connect.add(
        Domain(domain="other.com", hostname="da99.unknown.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da(set()):
        worker._reconcile_all()

    assert delete_queue.empty()


def test_active_domain_not_queued(worker, delete_queue, patch_connect):
    patch_connect.add(
        Domain(domain="good.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da({"good.com"}):
        worker._reconcile_all()

    assert delete_queue.empty()


# ---------------------------------------------------------------------------
# _reconcile_all — hostname backfill and migration
# ---------------------------------------------------------------------------


def test_backfill_null_hostname(worker, patch_connect):
    patch_connect.add(Domain(domain="backfill.com", hostname=None, username="admin"))
    patch_connect.commit()

    with _patch_da({"backfill.com"}):
        worker._reconcile_all()

    record = patch_connect.query(Domain).filter_by(domain="backfill.com").first()
    assert record.hostname == "da1.example.com"


def test_migration_updates_hostname(worker, patch_connect):
    patch_connect.add(
        Domain(domain="moved.com", hostname="da-old.example.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da({"moved.com"}):
        worker._reconcile_all()

    record = patch_connect.query(Domain).filter_by(domain="moved.com").first()
    assert record.hostname == "da1.example.com"


def test_dry_run_still_backfills(dry_run_worker, patch_connect):
    """Backfill is a data-repair operation, applied even in dry-run mode."""
    patch_connect.add(Domain(domain="fill.com", hostname=None, username="admin"))
    patch_connect.commit()

    with _patch_da({"fill.com"}):
        dry_run_worker._reconcile_all()

    record = patch_connect.query(Domain).filter_by(domain="fill.com").first()
    assert record.hostname == "da1.example.com"


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
