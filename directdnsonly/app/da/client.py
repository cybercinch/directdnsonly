"""DirectAdmin HTTP client.

Encapsulates all outbound communication with a single DirectAdmin server:
authenticated requests, the Basic-Auth → session-cookie fallback for DA Evo,
paginated domain listing, and the legacy URL-encoded response parser.
"""

from __future__ import annotations

from urllib.parse import parse_qs
from typing import Optional

import requests
import requests.exceptions
from loguru import logger


class DirectAdminClient:
    """HTTP client for a single DirectAdmin server.

    Handles two authentication modes transparently:
    - Basic Auth (classic DA / API-only access)
    - Session cookie via CMD_LOGIN (DA Evolution — redirects Basic Auth)

    Usage::

        client = DirectAdminClient("da1.example.com", 2222, "admin", "secret")
        domains = client.list_domains()   # set[str] or None on failure
        response = client.get("CMD_API_SHOW_ALL_USERS")
    """

    def __init__(
        self,
        hostname: str,
        port: int,
        username: str,
        password: str,
        ssl: bool = True,
        verify_ssl: bool = True,
    ) -> None:
        self.hostname = hostname
        self.port = port
        self.username = username
        self.password = password
        self.scheme = "https" if ssl else "http"
        self.verify_ssl = verify_ssl
        self._cookies = None  # populated on first successful session login

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_domains(self, ipp: int = 1000) -> Optional[set]:
        """Return all domains on this DA server via CMD_DNS_ADMIN (JSON, paginated).

        Falls back to the legacy URL-encoded parser if JSON decode fails.
        Returns a set of lowercase domain strings, or ``None`` if the server
        is unreachable or returns an error.
        """
        page = 1
        all_domains: set = set()
        total_pages = 1

        try:
            while page <= total_pages:
                response = self.get(
                    "CMD_DNS_ADMIN",
                    params={"json": "yes", "page": page, "ipp": ipp},
                )
                if response is None:
                    return None

                if response.is_redirect or response.status_code in (
                    301,
                    302,
                    303,
                    307,
                    308,
                ):
                    if self._cookies:
                        logger.error(
                            f"[da:{self.hostname}] Still redirecting after session login — "
                            f"check that '{self.username}' has admin-level access. Skipping."
                        )
                        return None
                    logger.debug(
                        f"[da:{self.hostname}] Basic Auth redirected "
                        f"(HTTP {response.status_code}) — attempting session login (DA Evo)"
                    )
                    if not self._login():
                        return None
                    continue  # retry this page with cookies

                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")
                if "text/html" in content_type:
                    logger.error(
                        f"[da:{self.hostname}] Returned HTML instead of API response — "
                        f"check credentials and admin-level access. Skipping."
                    )
                    return None

                try:
                    data = response.json()
                    for k, v in data.items():
                        if k.isdigit() and isinstance(v, dict) and "domain" in v:
                            all_domains.add(v["domain"].strip().lower())
                    total_pages = int(data.get("info", {}).get("total_pages", 1))
                    page += 1
                except Exception as exc:
                    logger.error(
                        f"[da:{self.hostname}] JSON decode failed on page {page}: {exc}\n"
                        f"Raw response: {response.text[:500]}"
                    )
                    all_domains.update(self._parse_legacy_domain_list(response.text))
                    break  # no paging in legacy mode

            return all_domains

        except requests.exceptions.SSLError as exc:
            logger.error(
                f"[da:{self.hostname}] SSL error — {exc}. "
                f"Set verify_ssl: false in reconciliation config if using self-signed certs."
            )
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"[da:{self.hostname}] Cannot reach server — {exc}. Skipping.")
        except requests.exceptions.Timeout:
            logger.error(f"[da:{self.hostname}] Connection timed out. Skipping.")
        except requests.exceptions.HTTPError as exc:
            logger.error(f"[da:{self.hostname}] HTTP error — {exc}. Skipping.")
        except Exception as exc:
            logger.error(f"[da:{self.hostname}] Unexpected error: {exc}")

        return None

    def get(
        self, command: str, params: Optional[dict] = None
    ) -> Optional[requests.Response]:
        """Authenticated GET to any DA CMD_* endpoint.

        Uses session cookies when available (after a successful ``_login``),
        otherwise falls back to HTTP Basic Auth.  Does **not** follow redirects
        so callers can detect the Basic-Auth → cookie upgrade.
        """
        url = f"{self.scheme}://{self.hostname}:{self.port}/{command}"
        kwargs: dict = dict(
            params=params or {},
            timeout=30,
            verify=self.verify_ssl,
            allow_redirects=False,
        )
        if self._cookies:
            kwargs["cookies"] = self._cookies
        else:
            kwargs["auth"] = (self.username, self.password)

        try:
            return requests.get(url, **kwargs)
        except Exception as exc:
            logger.error(f"[da:{self.hostname}] GET {command} failed: {exc}")
            return None

    def post(
        self, command: str, data: Optional[dict] = None
    ) -> Optional[requests.Response]:
        """Authenticated POST to any DA CMD_* endpoint."""
        url = f"{self.scheme}://{self.hostname}:{self.port}/{command}"
        kwargs: dict = dict(
            data=data or {},
            timeout=30,
            verify=self.verify_ssl,
            allow_redirects=False,
        )
        if self._cookies:
            kwargs["cookies"] = self._cookies
        else:
            kwargs["auth"] = (self.username, self.password)

        try:
            return requests.post(url, **kwargs)
        except Exception as exc:
            logger.error(f"[da:{self.hostname}] POST {command} failed: {exc}")
            return None

    def get_extra_dns_servers(self) -> dict:
        """Return the Extra DNS server map from CMD_MULTI_SERVER (GET).

        Returns a dict keyed by server hostname/IP, each value being the
        per-server settings dict (dns, domain_check, port, user, ssl, …).
        Returns ``{}`` on any error.
        """
        resp = self.get("CMD_MULTI_SERVER", params={"json": "yes"})
        if resp is None or resp.status_code != 200:
            logger.error(f"[da:{self.hostname}] CMD_MULTI_SERVER GET failed")
            return {}
        try:
            return resp.json().get("servers", {})
        except Exception as exc:
            logger.error(f"[da:{self.hostname}] CMD_MULTI_SERVER parse error: {exc}")
            return {}

    def add_extra_dns_server(
        self, ip: str, port: int, user: str, passwd: str, ssl: bool = False
    ) -> bool:
        """Register a new Extra DNS server via CMD_MULTI_SERVER action=add.

        Returns ``True`` if DA reports success, ``False`` otherwise.
        """
        resp = self.post(
            "CMD_MULTI_SERVER",
            data={
                "action": "add",
                "json": "yes",
                "ip": ip,
                "port": str(port),
                "user": user,
                "passwd": passwd,
                "ssl": "yes" if ssl else "no",
            },
        )
        if resp is None or resp.status_code != 200:
            logger.error(f"[da:{self.hostname}] CMD_MULTI_SERVER add failed for {ip}")
            return False
        try:
            result = resp.json()
            if result.get("success"):
                logger.info(f"[da:{self.hostname}] Added Extra DNS server {ip}")
                return True
            logger.error(
                f"[da:{self.hostname}] CMD_MULTI_SERVER add error: {result.get('result', result)}"
            )
            return False
        except Exception as exc:
            logger.error(f"[da:{self.hostname}] CMD_MULTI_SERVER add parse error: {exc}")
            return False

    def ensure_extra_dns_server(
        self, ip: str, port: int, user: str, passwd: str, ssl: bool = False
    ) -> bool:
        """Add (if absent) and configure a directdnsonly Extra DNS server.

        Ensures the server is registered with ``dns=yes`` and
        ``domain_check=yes`` so DirectAdmin pushes zone updates to it.
        Returns ``True`` if fully configured, ``False`` on any failure.
        """
        servers = self.get_extra_dns_servers()
        if ip not in servers:
            if not self.add_extra_dns_server(ip, port, user, passwd, ssl):
                return False

        ssl_str = "yes" if ssl else "no"
        resp = self.post(
            "CMD_MULTI_SERVER",
            data={
                "action": "multiple",
                "save": "yes",
                "json": "yes",
                "passwd": "",
                "select0": ip,
                f"port-{ip}": str(port),
                f"user-{ip}": user,
                f"ssl-{ip}": ssl_str,
                f"dns-{ip}": "yes",
                f"domain_check-{ip}": "yes",
                f"user_check-{ip}": "no",
                f"email-{ip}": "no",
                f"show_all_users-{ip}": "no",
            },
        )
        if resp is None or resp.status_code != 200:
            logger.error(
                f"[da:{self.hostname}] CMD_MULTI_SERVER save failed for {ip}"
            )
            return False
        try:
            result = resp.json()
            if result.get("success"):
                logger.info(
                    f"[da:{self.hostname}] Extra DNS server {ip} configured "
                    f"(dns=yes domain_check=yes)"
                )
                return True
            logger.error(
                f"[da:{self.hostname}] CMD_MULTI_SERVER save error: {result.get('result', result)}"
            )
            return False
        except Exception as exc:
            logger.error(
                f"[da:{self.hostname}] CMD_MULTI_SERVER save parse error: {exc}"
            )
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _login(self) -> bool:
        """POST CMD_LOGIN to obtain a DA Evo session cookie.

        Populates ``self._cookies`` on success and returns ``True``.
        Returns ``False`` on any failure.
        """
        login_url = f"{self.scheme}://{self.hostname}:{self.port}/CMD_LOGIN"
        try:
            response = requests.post(
                login_url,
                data={
                    "username": self.username,
                    "password": self.password,
                    "referer": "/CMD_DNS_ADMIN?json=yes&page=1&ipp=500",
                },
                timeout=30,
                verify=self.verify_ssl,
                allow_redirects=False,
            )
            if not response.cookies:
                logger.error(
                    f"[da:{self.hostname}] CMD_LOGIN returned no session cookie — "
                    f"check username/password."
                )
                return False
            self._cookies = response.cookies
            logger.debug(f"[da:{self.hostname}] Session login successful (DA Evo)")
            return True
        except Exception as exc:
            logger.error(f"[da:{self.hostname}] Session login failed: {exc}")
            return False

    @staticmethod
    def _parse_legacy_domain_list(body: str) -> set:
        """Parse DA's legacy CMD_API_SHOW_ALL_DOMAINS URL-encoded response.

        DA returns ``list[]=example.com&list[]=example2.com``, optionally
        newline-separated instead of ampersand-separated.
        """
        normalised = body.replace("\n", "&").strip("&")
        params = parse_qs(normalised)
        domains = params.get("list[]", [])
        return {d.strip().lower() for d in domains if d.strip()}
