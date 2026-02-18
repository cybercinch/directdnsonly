#!/usr/bin/env python3
import threading
from urllib.parse import parse_qs
from loguru import logger

import requests
import requests.exceptions

from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain


class ReconciliationWorker:
    """Periodically polls configured DirectAdmin servers and queues deletes
    for any zones in our DB that no longer exist in DirectAdmin.

    Safety rules:
    - If a DA server is unreachable, skip it entirely — never delete on uncertainty
    - Only touches domains registered via DaDNS (present in our `domains` table)
    - Domains in CoreDNS but NOT in our DB are not our zones; left untouched
    - Pushes to the existing delete_queue so the full delete path is exercised
    """

    def __init__(self, delete_queue, reconciliation_config: dict):
        self.delete_queue = delete_queue
        self.enabled = reconciliation_config.get("enabled", False)
        self.interval_seconds = reconciliation_config.get("interval_minutes", 60) * 60
        self.servers = reconciliation_config.get("directadmin_servers") or []
        self.verify_ssl = reconciliation_config.get("verify_ssl", True)
        self.ipp = int(reconciliation_config.get("ipp", 1000))
        self.dry_run = bool(reconciliation_config.get("dry_run", False))
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if not self.enabled:
            logger.info("Reconciliation poller disabled — skipping")
            return
        if not self.servers:
            logger.warning(
                "Reconciliation enabled but no directadmin_servers configured"
            )
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="reconciliation_worker"
        )
        self._thread.start()
        server_names = [s.get("hostname", "?") for s in self.servers]
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        logger.info(
            f"Reconciliation poller started [{mode}] — "
            f"interval: {self.interval_seconds // 60}m, "
            f"servers: {server_names}"
        )
        if self.dry_run:
            logger.warning(
                "[reconciler] DRY-RUN mode active — orphans will be logged but NOT queued for deletion"
            )

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Reconciliation poller stopped")

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        logger.info("Reconciliation worker starting — running initial check now")
        self._reconcile_all()
        # Wait for interval or stop signal; returns True when stopped
        while not self._stop_event.wait(timeout=self.interval_seconds):
            self._reconcile_all()

    def _reconcile_all(self):
        logger.info(
            f"[reconciler] Starting reconciliation pass across "
            f"{len(self.servers)} server(s)"
        )
        total_queued = 0
        # Build a map of all domains seen on all DA servers
        all_da_domains = {}  # domain -> hostname
        for server in self.servers:
            hostname = server.get("hostname")
            if not hostname:
                logger.warning("[reconciler] Server config missing hostname — skipping")
                continue
            try:
                da_domains = self._fetch_da_domains(
                    hostname,
                    server.get("port", 2222),
                    server.get("username"),
                    server.get("password"),
                    server.get("ssl", True),
                    ipp=self.ipp,
                )
                if da_domains is not None:
                    for d in da_domains:
                        all_da_domains[d] = hostname
                logger.debug(
                    f"[reconciler] {hostname}: {len(da_domains) if da_domains else 0} active domain(s) in DA"
                )
            except Exception as e:
                logger.error(f"[reconciler] Unexpected error polling {hostname}: {e}")

        # Now check local DB for all domains, update master if needed, and queue deletes only from recorded master
        session = connect()
        try:
            all_local_domains = session.query(Domain).all()
            migrated = 0
            backfilled = 0
            known_servers = {s.get("hostname") for s in self.servers}
            for record in all_local_domains:
                domain = record.domain
                recorded_master = record.hostname
                actual_master = all_da_domains.get(domain)
                if actual_master:
                    if not recorded_master:
                        logger.info(
                            f"[reconciler] Domain '{domain}' hostname backfilled: '{actual_master}'"
                        )
                        record.hostname = actual_master
                        backfilled += 1
                    elif actual_master != recorded_master:
                        logger.warning(
                            f"[reconciler] Domain '{domain}' migrated: "
                            f"'{recorded_master}' -> '{actual_master}'. Updating local DB."
                        )
                        record.hostname = actual_master
                        migrated += 1
                else:
                    # Only act if the recorded master is one we're polling
                    if recorded_master in known_servers:
                        if self.dry_run:
                            logger.warning(
                                f"[reconciler] [DRY-RUN] Would delete orphan: {record.domain} "
                                f"(master: {recorded_master})"
                            )
                        else:
                            self.delete_queue.put(
                                {
                                    "domain": record.domain,
                                    "hostname": record.hostname,
                                    "username": record.username or "",
                                    "source": "reconciler",
                                }
                            )
                            logger.debug(
                                f"[reconciler] Queued delete for orphan: {record.domain} "
                                f"(master: {recorded_master})"
                            )
                        total_queued += 1
            if migrated or backfilled:
                session.commit()
                if backfilled:
                    logger.info(
                        f"[reconciler] {backfilled} domain(s) had missing hostname backfilled."
                    )
                if migrated:
                    logger.info(
                        f"[reconciler] {migrated} domain(s) migrated to new master."
                    )
        finally:
            session.close()
        if self.dry_run:
            logger.info(
                f"[reconciler] Reconciliation pass complete [DRY-RUN] — "
                f"{total_queued} orphan(s) identified (none deleted)"
            )
        else:
            logger.info(
                f"[reconciler] Reconciliation pass complete — "
                f"{total_queued} domain(s) queued for deletion"
            )

    def _fetch_da_domains(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool,
        ipp: int = 1000,
    ):
        """Fetch all domains from a DA server via CMD_DNS_ADMIN (JSON, paging supported).

        Returns a set of domain strings on success, or None on any failure.
        """
        scheme = "https" if use_ssl else "http"
        page = 1
        all_domains = set()
        total_pages = 1
        cookies = None

        try:
            while page <= total_pages:
                url = f"{scheme}://{hostname}:{port}/CMD_DNS_ADMIN?json=yes&page={page}&ipp={ipp}"
                req_kwargs = dict(
                    timeout=30,
                    verify=self.verify_ssl,
                    allow_redirects=False,
                )
                if cookies:
                    req_kwargs["cookies"] = cookies
                else:
                    req_kwargs["auth"] = (username, password)

                response = requests.get(url, **req_kwargs)

                if response.is_redirect or response.status_code in (
                    301,
                    302,
                    303,
                    307,
                    308,
                ):
                    if not cookies:
                        logger.debug(
                            f"[reconciler] {hostname}:{port} redirected Basic Auth "
                            f"(HTTP {response.status_code}) — attempting session login (DA Evo)"
                        )
                        cookies = self._da_session_login(
                            scheme, hostname, port, username, password
                        )
                        if cookies is None:
                            return None
                        continue  # retry this page with cookies
                    else:
                        logger.error(
                            f"[reconciler] {hostname}:{port} still redirecting after session login — "
                            f"check that '{username}' has admin-level access. Skipping."
                        )
                        return None

                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    logger.error(
                        f"[reconciler] {hostname}:{port} returned HTML instead of API response — "
                        f"check credentials and admin-level access. Skipping."
                    )
                    return None

                # Try JSON first
                try:
                    data = response.json()
                    # Domains are in keys '0', '1', ...
                    for k, v in data.items():
                        if k.isdigit() and isinstance(v, dict) and "domain" in v:
                            all_domains.add(v["domain"].strip().lower())
                    # Paging info
                    info = data.get("info", {})
                    total_pages = int(info.get("total_pages", 1))
                    page += 1
                    continue
                except Exception as e:
                    logger.error(
                        f"[reconciler] JSON decode failed for {hostname}:{port} page {page}: {e}\nRaw response: {response.text[:500]}"
                    )
                    # Fallback to legacy parser
                    domains = self._parse_da_domain_list(response.text)
                    all_domains.update(domains)
                    break  # No paging in legacy mode

            return all_domains

        except requests.exceptions.SSLError as e:
            logger.error(
                f"[reconciler] SSL error connecting to {hostname}:{port} — {e}. "
                f"Set verify_ssl: false in reconciliation config if using self-signed certs."
            )
            return None
        except requests.exceptions.ConnectionError as e:
            logger.error(
                f"[reconciler] Cannot reach {hostname}:{port} — {e}. "
                f"Skipping this server."
            )
            return None
        except requests.exceptions.Timeout:
            logger.error(
                f"[reconciler] Timeout connecting to {hostname}:{port}. "
                f"Skipping this server."
            )
            return None
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"[reconciler] HTTP {response.status_code} from {hostname}:{port} — {e}. "
                f"Skipping this server."
            )
            return None
        except Exception as e:
            logger.error(f"[reconciler] Unexpected error fetching from {hostname}: {e}")
            return None

    def _da_session_login(
        self, scheme: str, hostname: str, port: int, username: str, password: str
    ):
        """POST to CMD_LOGIN to obtain a DA Evo session cookie.

        Returns a RequestsCookieJar on success, or None on failure.
        """
        login_url = f"{scheme}://{hostname}:{port}/CMD_LOGIN"
        try:
            response = requests.post(
                login_url,
                data={
                    "username": username,
                    "password": password,
                    "referer": "/CMD_DNS_ADMIN?json=yes&page=1&ipp=500",
                },
                timeout=30,
                verify=self.verify_ssl,
                allow_redirects=False,
            )
            if not response.cookies:
                logger.error(
                    f"[reconciler] {hostname}:{port} CMD_LOGIN returned no session cookie — "
                    f"check username/password."
                )
                return None
            logger.debug(
                f"[reconciler] {hostname}:{port} session login successful (DA Evo)"
            )
            return response.cookies
        except Exception as e:
            logger.error(f"[reconciler] {hostname}:{port} session login failed: {e}")
            return None

    @staticmethod
    def _parse_da_domain_list(body: str) -> set:
        """Parse DA's CMD_API_SHOW_ALL_DOMAINS response.

        DA returns URL-encoded key=value pairs, either on one line or newline-
        separated. The domain list uses the key 'list[]'.

        Example response:
            list[]=example.com&list[]=example2.com
        """
        # Normalise newline-separated responses to a single query string
        normalised = body.replace("\n", "&").strip("&")
        params = parse_qs(normalised)
        domains = params.get("list[]", [])
        return {d.strip().lower() for d in domains if d.strip()}


if __name__ == "__main__":
    import argparse
    import sys
    from queue import Queue

    parser = argparse.ArgumentParser(
        description="Test DirectAdmin domain fetcher (JSON/paging)"
    )
    parser.add_argument("--hostname", required=True, help="DirectAdmin server hostname")
    parser.add_argument(
        "--port", type=int, default=2222, help="DirectAdmin port (default: 2222)"
    )
    parser.add_argument("--username", required=True, help="DirectAdmin admin username")
    parser.add_argument("--password", required=True, help="DirectAdmin admin password")
    parser.add_argument("--ssl", action="store_true", help="Use HTTPS (default: True)")
    parser.add_argument(
        "--no-ssl", dest="ssl", action="store_false", help="Use HTTP (not recommended)"
    )
    parser.set_defaults(ssl=True)
    parser.add_argument(
        "--verify-ssl", action="store_true", help="Verify SSL certs (default: True)"
    )
    parser.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_false",
        help="Don't verify SSL certs",
    )
    parser.set_defaults(verify_ssl=True)
    parser.add_argument(
        "--ipp", type=int, default=1000, help="Items per page (default: 1000)"
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print raw JSON response for first page",
    )

    args = parser.parse_args()

    # Minimal config for testing
    config = {
        "enabled": True,
        "directadmin_servers": [
            {
                "hostname": args.hostname,
                "port": args.port,
                "username": args.username,
                "password": args.password,
                "ssl": args.ssl,
            }
        ],
        "verify_ssl": args.verify_ssl,
    }
    q = Queue()
    worker = ReconciliationWorker(q, config)
    server = config["directadmin_servers"][0]
    print(
        f"Fetching domains from {server['hostname']}:{server['port']} (ipp={args.ipp})..."
    )
    # Directly call the fetch method for testing
    domains = worker._fetch_da_domains(
        server["hostname"],
        server.get("port", 2222),
        server.get("username"),
        server.get("password"),
        server.get("ssl", True),
        ipp=args.ipp,
    )
    if domains is None:
        print("Failed to fetch domains.", file=sys.stderr)
        sys.exit(1)
    print(f"Fetched {len(domains)} domains:")
    for d in sorted(domains):
        print(d)

    if args.print_json:
        # Print the first page's raw JSON for inspection
        scheme = "https" if server.get("ssl", True) else "http"
        url = f"{scheme}://{server['hostname']}:{server.get('port', 2222)}/CMD_DNS_ADMIN?json=yes&page=1&ipp={args.ipp}"
        resp = requests.get(
            url,
            auth=(server.get("username"), server.get("password")),
            timeout=30,
            verify=args.verify_ssl,
            allow_redirects=False,
        )
        try:
            print("\nRaw JSON for first page:")
            print(resp.json())
        except Exception:
            print("(Could not parse JSON)")
