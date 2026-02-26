import cherrypy
from urllib.parse import urlencode, parse_qs
from loguru import logger
from directdnsonly.config import config
from directdnsonly.app.utils import (
    check_zone_exists,
    check_parent_domain_owner,
    get_domain_record,
    get_parent_domain_record,
)
from directdnsonly.app.utils.zone_parser import validate_and_normalize_zone


class DNSAdminAPI:
    def __init__(self, save_queue, delete_queue, backend_registry):
        self.save_queue = save_queue
        self.delete_queue = delete_queue
        self.backend_registry = backend_registry

    @cherrypy.expose
    def index(self):
        return "DNS Admin API - Available endpoints: /CMD_API_DNS_ADMIN"

    @cherrypy.expose
    def CMD_API_LOGIN_TEST(self):
        """DirectAdmin login test — confirms credentials are valid"""
        return urlencode({"error": 0, "text": "Login OK"})

    @cherrypy.expose
    def CMD_API_DNS_ADMIN(self, **params):
        """Handle both DirectAdmin-style API calls and raw zone file uploads"""
        try:
            if cherrypy.request.method == "GET":
                return self._handle_exists(params)

            if cherrypy.request.method != "POST":
                cherrypy.response.status = 405
                return urlencode({"error": 1, "text": "Method not allowed"})

            # Parse parameters from both query string and body
            body_params = {}
            if cherrypy.request.body:
                content_type = cherrypy.request.headers.get("Content-Type", "")

                if "application/x-www-form-urlencoded" in content_type:
                    raw_body = cherrypy.request.body.read()
                    if raw_body:
                        body_params = parse_qs(raw_body.decode("utf-8"))
                        body_params = {
                            k: v[0] if len(v) == 1 else v
                            for k, v in body_params.items()
                        }
                elif "text/plain" in content_type:
                    body_params = {
                        "zone_file": cherrypy.request.body.read().decode("utf-8")
                    }

            # Combine parameters (body overrides query)
            all_params = {**params, **body_params}
            logger.debug(f"Request parameters: {all_params}")

            if "zone_file" not in all_params:
                logger.debug(
                    "No zone file provided.  Maybe in body as DirectAdmin does?"
                )
                # Grab from body
                all_params["zone_file"] = str(cherrypy.request.body.read(), "utf-8")
                logger.debug("Read zone file from body :)")

            # Required parameters
            action = all_params.get("action")
            domain = all_params.get("domain")

            if not action:
                # DirectAdmin sends an initial request without an action
                # parameter as a connectivity check — respond with success
                logger.debug("Received request with no action — connectivity check")
                return urlencode({"error": 0, "text": "OK"})
            if not domain:
                raise ValueError("Missing 'domain' parameter")

            # Handle different actions
            if action == "rawsave":
                return self._handle_rawsave(domain, all_params)
            elif action == "delete":
                return self._handle_delete(domain, all_params)
            else:
                raise ValueError(f"Unsupported action: {action}")

        except Exception as e:
            logger.error(f"API error: {str(e)}")
            cherrypy.response.status = 400
            return urlencode({"error": 1, "text": str(e)})

    def _handle_exists(self, params: dict):
        """Handle GET action=exists — domain and optional parent domain lookup"""
        action = params.get("action")
        if action != "exists":
            cherrypy.response.status = 400
            return urlencode({"error": 1, "text": f"Unsupported GET action: {action}"})

        domain = params.get("domain")
        if not domain:
            cherrypy.response.status = 400
            return urlencode({"error": 1, "text": "Missing 'domain' parameter"})

        check_parent = bool(params.get("check_for_parent_domain"))

        domain_exists = check_zone_exists(domain)
        parent_exists = check_parent_domain_owner(domain) if check_parent else False

        if not domain_exists and not parent_exists:
            return urlencode({"error": 0, "exists": 0})

        if domain_exists:
            record = get_domain_record(domain)
            return urlencode(
                {
                    "error": 0,
                    "exists": 1,
                    "details": f"Domain exists on {record.hostname}",
                }
            )

        # Parent domain match.
        # exists=2: basic check (DA 1.53.0) — parent in domainowners, no ownership data.
        # exists=3: cluster check (DA 1.59.0+) — parent in cluster_domainowners, returns
        #   hostname+username so the master can validate the requesting user owns the parent.
        parent_record = get_parent_domain_record(domain)
        cluster_check = int(
            config.get("app.check_subdomain_owner_in_cluster_domainowners") or 0
        )
        if cluster_check >= 1:
            return urlencode(
                {
                    "error": 0,
                    "exists": 3,
                    "hostname": parent_record.hostname or "",
                    "username": parent_record.username or "",
                }
            )
        return urlencode(
            {
                "error": 0,
                "exists": 2,
                "details": f"Parent Domain exists on {parent_record.hostname}",
            }
        )

    def _handle_rawsave(self, domain: str, params: dict):
        """Process zone file saves"""
        zone_data = params.get("zone_file")
        if not zone_data:
            raise ValueError("Missing zone file content")

        normalized_zone = validate_and_normalize_zone(zone_data, domain)
        logger.info(f"Validated zone for {domain}")

        self.save_queue.put(
            {
                "domain": domain,
                "zone_file": normalized_zone,
                "hostname": params.get("hostname", ""),
                "username": params.get("username", ""),
                "client_ip": cherrypy.request.remote.ip,
            }
        )

        logger.success(f"Queued zone update for {domain}")
        return urlencode({"error": 0})

    def _handle_delete(self, domain: str, params: dict):
        """Process zone deletions"""
        self.delete_queue.put(
            {
                "domain": domain,
                "hostname": params.get("hostname", ""),
                "username": params.get("username", ""),
                "client_ip": cherrypy.request.remote.ip,
            }
        )

        logger.success(f"Queued deletion for {domain}")
        return urlencode({"error": 0})

    @cherrypy.expose
    def queue_status(self):
        """Debug endpoint for queue monitoring"""
        return {
            "save_queue_size": self.save_queue.qsize(),
            "delete_queue_size": self.delete_queue.qsize(),
            "last_save_item": self._get_last_item(self.save_queue),
            "last_delete_item": self._get_last_item(self.delete_queue),
        }

    @staticmethod
    def _get_last_item(queue):
        """Helper to safely get last queue item"""
        try:
            if hasattr(queue, "last_item"):
                return queue.last_item
            return "Last item tracking not available"
        except Exception:
            return "Error retrieving last item"
