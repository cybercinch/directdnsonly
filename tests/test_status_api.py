"""Tests for directdnsonly.app.api.status — StatusAPI."""

import json
from unittest.mock import MagicMock

import cherrypy
import pytest

from directdnsonly.app.api.status import StatusAPI
from directdnsonly.app.db.models import Domain


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RECONCILER_OK = {
    "enabled": True,
    "alive": True,
    "dry_run": False,
    "interval_minutes": 60,
    "last_run": {},
}
_PEER_SYNC_OFF = {
    "enabled": False,
    "alive": False,
    "peers": [],
    "total": 0,
    "healthy": 0,
    "degraded": 0,
}


def _qs(**overrides):
    base = {
        "save_queue_size": 0,
        "delete_queue_size": 0,
        "retry_queue_size": 0,
        "dead_letters": 0,
        "save_worker_alive": True,
        "delete_worker_alive": True,
        "retry_worker_alive": True,
        "reconciler": _RECONCILER_OK,
        "peer_sync": _PEER_SYNC_OFF,
    }
    base.update(overrides)
    return base


def _api(qs=None):
    wm = MagicMock()
    wm.queue_status.return_value = qs or _qs()
    return StatusAPI(wm)


# ---------------------------------------------------------------------------
# _compute_overall
# ---------------------------------------------------------------------------


def test_overall_ok_all_healthy():
    assert StatusAPI._compute_overall(_qs()) == "ok"


def test_overall_error_save_worker_dead():
    assert StatusAPI._compute_overall(_qs(save_worker_alive=False)) == "error"


def test_overall_error_delete_worker_dead():
    assert StatusAPI._compute_overall(_qs(delete_worker_alive=False)) == "error"


def test_overall_degraded_retries_pending():
    assert StatusAPI._compute_overall(_qs(retry_queue_size=3)) == "degraded"


def test_overall_degraded_dead_letters():
    assert StatusAPI._compute_overall(_qs(dead_letters=1)) == "degraded"


def test_overall_degraded_peer_unhealthy():
    ps = {**_PEER_SYNC_OFF, "degraded": 1}
    assert StatusAPI._compute_overall(_qs(peer_sync=ps)) == "degraded"


def test_overall_error_takes_priority_over_degraded():
    """error > degraded when both conditions are true."""
    assert (
        StatusAPI._compute_overall(
            _qs(save_worker_alive=False, retry_queue_size=5)
        )
        == "error"
    )


# ---------------------------------------------------------------------------
# _build — structure and zone count
# ---------------------------------------------------------------------------


def test_build_structure(patch_connect):
    api = _api()
    result = api._build()

    assert "status" in result
    assert "queues" in result
    assert "workers" in result
    assert "reconciler" in result
    assert "peer_sync" in result
    assert "zones" in result


def test_build_zone_count_zero(patch_connect):
    api = _api()
    result = api._build()
    assert result["zones"]["total"] == 0


def test_build_zone_count_with_domains(patch_connect):
    for d in ["a.com", "b.com", "c.com"]:
        patch_connect.add(Domain(domain=d, hostname="da1.example.com", username="admin"))
    patch_connect.commit()

    api = _api()
    result = api._build()
    assert result["zones"]["total"] == 3


def test_build_queues_forwarded(patch_connect):
    api = _api(_qs(save_queue_size=2, delete_queue_size=1, retry_queue_size=3, dead_letters=1))
    result = api._build()

    assert result["queues"]["save"] == 2
    assert result["queues"]["delete"] == 1
    assert result["queues"]["retry"] == 3
    assert result["queues"]["dead_letters"] == 1


def test_build_workers_forwarded(patch_connect):
    api = _api()
    result = api._build()

    assert result["workers"]["save"] is True
    assert result["workers"]["delete"] is True
    assert result["workers"]["retry_drain"] is True


# ---------------------------------------------------------------------------
# index — JSON encoding
# ---------------------------------------------------------------------------


def test_index_returns_valid_json(patch_connect):
    api = _api()
    with MagicMock() as mock_resp:
        cherrypy.response = mock_resp
        cherrypy.response.headers = {}
        body = api.index()

    data = json.loads(body)
    assert data["status"] == "ok"
    assert isinstance(data["zones"]["total"], int)
