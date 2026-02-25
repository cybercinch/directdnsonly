"""Operational status endpoint — aggregates queue, worker, reconciler, and peer health."""

import json

import cherrypy
from sqlalchemy import func, select

from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain


class StatusAPI:
    """Exposes GET /status as a JSON health/status document.

    Aggregates data from WorkerManager.queue_status() and a live DB zone count
    into a single response that a UI or monitoring system can poll.

    Overall ``status`` field:
    - ``ok``       — all workers alive, no dead-letters, all peers healthy
    - ``degraded`` — retries pending, dead-letters present, or a peer is unhealthy
    - ``error``    — a core worker thread is not alive
    """

    def __init__(self, worker_manager):
        self._wm = worker_manager

    @cherrypy.expose
    def index(self):
        cherrypy.response.headers["Content-Type"] = "application/json"
        return json.dumps(self._build(), default=str).encode()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build(self) -> dict:
        qs = self._wm.queue_status()

        zone_count = self._zone_count()

        overall = self._compute_overall(qs)

        return {
            "status": overall,
            "queues": {
                "save": qs.get("save_queue_size", 0),
                "delete": qs.get("delete_queue_size", 0),
                "retry": qs.get("retry_queue_size", 0),
                "dead_letters": qs.get("dead_letters", 0),
            },
            "workers": {
                "save": qs.get("save_worker_alive"),
                "delete": qs.get("delete_worker_alive"),
                "retry_drain": qs.get("retry_worker_alive"),
            },
            "reconciler": qs.get("reconciler", {}),
            "peer_sync": qs.get("peer_sync", {}),
            "zones": {"total": zone_count},
        }

    @staticmethod
    def _zone_count() -> int:
        session = connect()
        try:
            return session.execute(select(func.count(Domain.id))).scalar() or 0
        except Exception:
            return 0
        finally:
            session.close()

    @staticmethod
    def _compute_overall(qs: dict) -> str:
        if not qs.get("save_worker_alive") or not qs.get("delete_worker_alive"):
            return "error"
        peer_sync = qs.get("peer_sync", {})
        if (
            qs.get("retry_queue_size", 0) > 0
            or qs.get("dead_letters", 0) > 0
            or peer_sync.get("degraded", 0) > 0
        ):
            return "degraded"
        return "ok"
