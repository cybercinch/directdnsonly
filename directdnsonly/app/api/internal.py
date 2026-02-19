import cherrypy
import json
from loguru import logger
from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain


class InternalAPI:
    """Peer-to-peer zone_data exchange endpoint.

    Used by PeerSyncWorker to replicate zone_data between directdnsonly
    instances so each node can independently heal its local backends.

    All routes require the same basic auth as the main API.
    """

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
                record = (
                    session.query(Domain)
                    .filter_by(domain=domain)
                    .filter(Domain.zone_data.isnot(None))
                    .first()
                )
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
                records = (
                    session.query(Domain)
                    .filter(Domain.zone_data.isnot(None))
                    .all()
                )
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
