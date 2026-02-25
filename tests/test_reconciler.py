"""Tests for directdnsonly.app.reconciler — ReconciliationWorker."""

import pytest
from queue import Queue
from unittest.mock import patch, MagicMock

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
    return patch(
        DA_CLIENT_PATH, **{"return_value.list_domains.return_value": return_value}
    )


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


def test_initial_delay_stored(delete_queue):
    cfg = {**BASE_CONFIG, "initial_delay_minutes": 30}
    w = ReconciliationWorker(delete_queue, cfg)
    assert w._initial_delay == 30 * 60


def test_zero_initial_delay_by_default(delete_queue):
    w = ReconciliationWorker(delete_queue, BASE_CONFIG)
    assert w._initial_delay == 0


# ---------------------------------------------------------------------------
# _heal_backends — Option C backend healing
# ---------------------------------------------------------------------------


def _make_backend_registry(zone_exists_return: bool):
    """Build a mock backend_registry with one backend whose zone_exists returns
    the given value."""
    backend = MagicMock()
    backend.zone_exists.return_value = zone_exists_return
    registry = MagicMock()
    registry.get_available_backends.return_value = {"coredns": backend}
    return registry, backend


def test_heal_queues_zone_missing_from_backend(delete_queue, patch_connect):
    save_queue = Queue()
    registry, backend = _make_backend_registry(zone_exists_return=False)

    patch_connect.add(
        Domain(
            domain="missing.com",
            hostname="da1.example.com",
            username="admin",
            zone_data="; zone file",
        )
    )
    patch_connect.commit()

    w = ReconciliationWorker(
        delete_queue, BASE_CONFIG, save_queue=save_queue, backend_registry=registry
    )
    w._heal_backends()

    assert not save_queue.empty()
    item = save_queue.get_nowait()
    assert item["domain"] == "missing.com"
    assert item["failed_backends"] == ["coredns"]
    assert item["source"] == "reconciler_heal"
    assert item["zone_file"] == "; zone file"


def test_heal_skips_domains_without_zone_data(delete_queue, patch_connect):
    save_queue = Queue()
    registry, _ = _make_backend_registry(zone_exists_return=False)

    patch_connect.add(
        Domain(
            domain="nodata.com",
            hostname="da1.example.com",
            username="admin",
            zone_data=None,
        )
    )
    patch_connect.commit()

    w = ReconciliationWorker(
        delete_queue, BASE_CONFIG, save_queue=save_queue, backend_registry=registry
    )
    w._heal_backends()

    assert save_queue.empty()


def test_heal_skips_when_all_backends_have_zone(delete_queue, patch_connect):
    save_queue = Queue()
    registry, _ = _make_backend_registry(zone_exists_return=True)

    patch_connect.add(
        Domain(
            domain="present.com",
            hostname="da1.example.com",
            username="admin",
            zone_data="; zone file",
        )
    )
    patch_connect.commit()

    w = ReconciliationWorker(
        delete_queue, BASE_CONFIG, save_queue=save_queue, backend_registry=registry
    )
    w._heal_backends()

    assert save_queue.empty()


def test_heal_dry_run_does_not_queue(delete_queue, patch_connect):
    save_queue = Queue()
    registry, _ = _make_backend_registry(zone_exists_return=False)

    patch_connect.add(
        Domain(
            domain="dry.com",
            hostname="da1.example.com",
            username="admin",
            zone_data="; zone file",
        )
    )
    patch_connect.commit()

    cfg = {**BASE_CONFIG, "dry_run": True}
    w = ReconciliationWorker(
        delete_queue, cfg, save_queue=save_queue, backend_registry=registry
    )
    w._heal_backends()

    assert save_queue.empty()


def test_heal_skipped_when_no_registry(delete_queue, patch_connect):
    """_heal_backends should not run when backend_registry is None."""
    save_queue = Queue()

    patch_connect.add(
        Domain(
            domain="noregistry.com",
            hostname="da1.example.com",
            username="admin",
            zone_data="; zone file",
        )
    )
    patch_connect.commit()

    w = ReconciliationWorker(delete_queue, BASE_CONFIG, save_queue=save_queue)
    # Should not raise; healing is silently skipped
    with _patch_da({"noregistry.com"}):
        w._reconcile_all()

    assert save_queue.empty()


# ---------------------------------------------------------------------------
# get_status — last-run state
# ---------------------------------------------------------------------------


def test_get_status_before_any_run(worker):
    status = worker.get_status()
    assert status["enabled"] is True
    assert status["alive"] is False
    assert status["last_run"] == {}


def test_get_status_after_run(worker, patch_connect):
    with _patch_da(set()):
        worker._reconcile_all()

    s = worker.get_status()
    assert s["enabled"] is True
    lr = s["last_run"]
    assert lr["status"] == "ok"
    assert "started_at" in lr
    assert "completed_at" in lr
    assert "duration_seconds" in lr
    assert lr["da_servers_polled"] == 1
    assert lr["da_servers_unreachable"] == 0
    assert lr["dry_run"] is False


def test_get_status_counts_unreachable_server(worker, patch_connect):
    with _patch_da(None):
        worker._reconcile_all()

    lr = worker.get_status()["last_run"]
    assert lr["da_servers_polled"] == 1
    assert lr["da_servers_unreachable"] == 1


def test_get_status_counts_orphans(worker, delete_queue, patch_connect):
    patch_connect.add(
        Domain(domain="orphan.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da(set()):
        worker._reconcile_all()

    lr = worker.get_status()["last_run"]
    assert lr["orphans_found"] == 1
    assert lr["orphans_queued"] == 1


def test_get_status_dry_run_orphans_not_queued_in_stats(dry_run_worker, patch_connect):
    patch_connect.add(
        Domain(domain="orphan.com", hostname="da1.example.com", username="admin")
    )
    patch_connect.commit()

    with _patch_da(set()):
        dry_run_worker._reconcile_all()

    lr = dry_run_worker.get_status()["last_run"]
    assert lr["dry_run"] is True
    assert lr["orphans_found"] == 1
    assert lr["orphans_queued"] == 0


def test_get_status_zones_in_db_counted(worker, patch_connect):
    for d in ["a.com", "b.com", "c.com"]:
        patch_connect.add(Domain(domain=d, hostname="da1.example.com", username="admin"))
    patch_connect.commit()

    with _patch_da({"a.com", "b.com", "c.com"}):
        worker._reconcile_all()

    lr = worker.get_status()["last_run"]
    assert lr["zones_in_db"] == 3
    assert lr["zones_in_da"] == 3
    assert lr["orphans_found"] == 0
