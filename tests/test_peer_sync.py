"""Tests for directdnsonly.app.peer_sync — PeerSyncWorker."""

import datetime
import json
import pytest
from sqlalchemy import select, func
from unittest.mock import patch, MagicMock

from directdnsonly.app.peer_sync import PeerSyncWorker
from directdnsonly.app.db.models import Domain


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_CONFIG = {
    "enabled": True,
    "interval_minutes": 15,
    "peers": [
        {
            "url": "http://ddo-2:2222",
            "username": "directdnsonly",
            "password": "changeme",
        }
    ],
}

NOW = datetime.datetime(2024, 6, 1, 12, 0, 0)
OLDER = datetime.datetime(2024, 6, 1, 11, 0, 0)

ZONE_DATA = "$ORIGIN example.com.\n@ 300 IN SOA ns1 hostmaster 1 3600 900 604800 300\n"


# ---------------------------------------------------------------------------
# Config / startup tests
# ---------------------------------------------------------------------------


def test_disabled_by_default():
    worker = PeerSyncWorker({})
    assert not worker.enabled


def test_interval_stored():
    worker = PeerSyncWorker({"enabled": True, "interval_minutes": 30})
    assert worker.interval_seconds == 1800


def test_default_interval():
    worker = PeerSyncWorker({"enabled": True})
    assert worker.interval_seconds == 15 * 60


def test_peers_stored():
    worker = PeerSyncWorker(BASE_CONFIG)
    assert len(worker.peers) == 1
    assert worker.peers[0]["url"] == "http://ddo-2:2222"


def test_start_skips_when_disabled(caplog):
    worker = PeerSyncWorker({"enabled": False})
    worker.start()
    assert worker._thread is None


def test_start_warns_when_no_peers(caplog):
    import logging

    worker = PeerSyncWorker({"enabled": True, "peers": []})
    with patch.object(worker, "_run"):
        worker.start()
    # Thread should not have started
    assert worker._thread is None


# ---------------------------------------------------------------------------
# _sync_from_peer tests
# ---------------------------------------------------------------------------


def _make_peer():
    return BASE_CONFIG["peers"][0]


def _peer_list(domain, ts=None):
    """Simulate the JSON response from GET /internal/zones."""
    return [
        {
            "domain": domain,
            "zone_updated_at": ts.isoformat() if ts else None,
            "hostname": "da1.example.com",
            "username": "admin",
        }
    ]


def _peer_zone(domain, ts=None, zone_data=ZONE_DATA):
    """Simulate the JSON response from GET /internal/zones?domain=X."""
    return {
        "domain": domain,
        "zone_data": zone_data,
        "zone_updated_at": ts.isoformat() if ts else None,
        "hostname": "da1.example.com",
        "username": "admin",
    }


def test_sync_creates_new_local_record(patch_connect, monkeypatch):
    """When local DB has no record, peer zone_data is fetched and stored."""
    worker = PeerSyncWorker(BASE_CONFIG)
    session = patch_connect

    def mock_get(url, auth=None, timeout=10, params=None):
        resp = MagicMock()
        resp.status_code = 200
        if params and params.get("domain"):
            resp.json.return_value = _peer_zone("example.com", NOW)
        else:
            resp.json.return_value = _peer_list("example.com", NOW)
        return resp

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    worker._sync_from_peer(_make_peer())

    record = session.execute(
        select(Domain).filter_by(domain="example.com")
    ).scalar_one_or_none()
    assert record is not None
    assert record.zone_data == ZONE_DATA
    assert record.zone_updated_at == NOW


def test_sync_updates_older_local_record(patch_connect, monkeypatch):
    """When local zone_data is older than peer's, it is overwritten."""
    session = patch_connect
    session.add(
        Domain(domain="example.com", zone_data="old data", zone_updated_at=OLDER)
    )
    session.commit()

    worker = PeerSyncWorker(BASE_CONFIG)

    def mock_get(url, auth=None, timeout=10, params=None):
        resp = MagicMock()
        resp.status_code = 200
        if params and params.get("domain"):
            resp.json.return_value = _peer_zone("example.com", NOW)
        else:
            resp.json.return_value = _peer_list("example.com", NOW)
        return resp

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    worker._sync_from_peer(_make_peer())

    record = session.execute(
        select(Domain).filter_by(domain="example.com")
    ).scalar_one_or_none()
    assert record.zone_data == ZONE_DATA
    assert record.zone_updated_at == NOW


def test_sync_skips_when_local_is_newer(patch_connect, monkeypatch):
    """When local zone_data is newer than peer's, it is not overwritten."""
    session = patch_connect
    session.add(
        Domain(domain="example.com", zone_data="newer local", zone_updated_at=NOW)
    )
    session.commit()

    worker = PeerSyncWorker(BASE_CONFIG)
    fetch_calls = []

    def mock_get(url, auth=None, timeout=10, params=None):
        resp = MagicMock()
        resp.status_code = 200
        if params and params.get("domain"):
            fetch_calls.append(url)
            resp.json.return_value = _peer_zone("example.com", OLDER)
        else:
            resp.json.return_value = _peer_list("example.com", OLDER)
        return resp

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    worker._sync_from_peer(_make_peer())

    # zone_data fetch should not have been called
    assert not fetch_calls
    record = session.execute(
        select(Domain).filter_by(domain="example.com")
    ).scalar_one_or_none()
    assert record.zone_data == "newer local"


def test_sync_skips_unreachable_peer(monkeypatch):
    """If the peer raises a connection error, _sync_all catches it gracefully."""
    worker = PeerSyncWorker(BASE_CONFIG)

    def mock_get(*args, **kwargs):
        raise ConnectionError("peer down")

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    # Should not raise
    worker._sync_all()


def test_sync_skips_peer_with_bad_status(patch_connect, monkeypatch):
    """Non-200 response from peer zone list is silently skipped."""
    worker = PeerSyncWorker(BASE_CONFIG)
    session = patch_connect

    def mock_get(url, auth=None, timeout=10, params=None):
        resp = MagicMock()
        resp.status_code = 503
        return resp

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    worker._sync_from_peer(_make_peer())

    # No records should have been created
    assert session.execute(select(func.count()).select_from(Domain)).scalar() == 0


def test_sync_skips_missing_zone_data_in_response(patch_connect, monkeypatch):
    """If the peer returns no zone_data for a domain, it is skipped."""
    session = patch_connect

    worker = PeerSyncWorker(BASE_CONFIG)

    def mock_get(url, auth=None, timeout=10, params=None):
        resp = MagicMock()
        resp.status_code = 200
        if params and params.get("domain"):
            resp.json.return_value = {"domain": "example.com", "zone_data": None}
        else:
            resp.json.return_value = _peer_list("example.com", NOW)
        return resp

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    worker._sync_from_peer(_make_peer())

    assert session.execute(select(func.count()).select_from(Domain)).scalar() == 0


def test_sync_empty_peer_list(patch_connect, monkeypatch):
    """Empty zone list from peer results in zero syncs without error."""
    worker = PeerSyncWorker(BASE_CONFIG)

    def mock_get(url, auth=None, timeout=10, params=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = []
        return resp

    monkeypatch.setattr("directdnsonly.app.peer_sync.requests.get", mock_get)

    worker._sync_from_peer(_make_peer())
