import cherrypy
import json
from loguru import logger
from sqlalchemy import select
from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain


class InternalAPI:
    """Peer-to-peer zone_data exchange endpoints.

    Used by PeerSyncWorker to replicate zone_data between directdnsonly
    instances so each node can independently heal its local backends.

    All routes require peer_sync basic auth credentials, which are
    configured separately from the main DirectAdmin-facing credentials
    (peer_sync.auth_username / peer_sync.auth_password).
    """

    def __init__(self, peer_syncer=None):
        self._peer_syncer = peer_syncer

    @cherrypy.expose
    def zones(self, domain=None):
        """Return zone metadata or zone_data for a specific domain.

        GET /internal/zones
            Returns a JSON array of {domain, zone_updated_at, hostname, username}
            for all domains that have stored zone_data.

        GET /internal/zones?domain=example.com
            Returns {domain, zone_data, zone_updated_at, hostname, username}
            for the requested domain, or 404 if not found / no zone_data.
        """
        cherrypy.response.headers["Content-Type"] = "application/json"
        session = connect()
        try:
            if domain:
                record = session.execute(
                    select(Domain)
                    .filter_by(domain=domain)
                    .where(Domain.zone_data.isnot(None))
                ).scalar_one_or_none()
                if not record:
                    cherrypy.response.status = 404
                    return json.dumps({"error": "not found"}).encode()
                return json.dumps(
                    {
                        "domain": record.domain,
                        "zone_data": record.zone_data,
                        "zone_updated_at": (
                            record.zone_updated_at.isoformat()
                            if record.zone_updated_at
                            else None
                        ),
                        "hostname": record.hostname,
                        "username": record.username,
                    }
                ).encode()
            else:
                records = session.execute(
                    select(Domain).where(Domain.zone_data.isnot(None))
                ).scalars().all()
                return json.dumps(
                    [
                        {
                            "domain": r.domain,
                            "zone_updated_at": (
                                r.zone_updated_at.isoformat()
                                if r.zone_updated_at
                                else None
                            ),
                            "hostname": r.hostname,
                            "username": r.username,
                        }
                        for r in records
                    ]
                ).encode()
        except Exception as exc:
            logger.error(f"[internal] Error serving /internal/zones: {exc}")
            cherrypy.response.status = 500
            return json.dumps({"error": "internal server error"}).encode()
        finally:
            session.close()

    @cherrypy.expose
    def peers(self):
        """Return the list of peer URLs this node knows about.

        GET /internal/peers
            Returns a JSON array of URL strings.  Used by other nodes during
            sync to discover new cluster members (gossip-lite mesh expansion).
        """
        cherrypy.response.headers["Content-Type"] = "application/json"
        urls = self._peer_syncer.get_peer_urls() if self._peer_syncer else []
        return json.dumps(urls).encode()
