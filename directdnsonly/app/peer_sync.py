#!/usr/bin/env python3
"""Peer sync worker — exchanges zone_data between directdnsonly instances.

Each node stores zone_data in its local SQLite DB after every successful
backend write.  When DirectAdmin pushes a zone to one node but another
is temporarily offline, the offline node misses that zone_data.

PeerSyncWorker corrects this by periodically comparing zone lists with
all known peers and fetching any zone_data that is newer or absent locally.
It only updates the local DB — it never writes directly to backends.  The
existing reconciler healing pass then detects missing zones and re-pushes
using the freshly synced zone_data.

Mesh behaviour:
- Each node exposes /internal/peers listing the URLs it knows about
- During each sync pass, every peer is asked for its peer list; any URLs
  not already known are added automatically (gossip-lite discovery)
- A three-node cluster therefore only needs a linear chain of initial
  connections — nodes propagate awareness of each other on the first pass

Health tracking:
- Consecutive failures per peer are counted; after FAILURE_THRESHOLD
  misses the peer is marked degraded and a warning is logged once
- On the next successful contact the peer is marked recovered

Safety properties:
- If a peer is unreachable, skip it and try next interval
- Only zone_data is synced — backend writes remain the sole responsibility
  of the local save queue worker
- Newer zone_updated_at timestamp wins; local data is never overwritten
  with older peer data
- Peer discovery is best-effort and never fails a sync pass
"""
import datetime
import os
import threading
from loguru import logger
import requests
from sqlalchemy import select

from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain

# Consecutive failures before a peer is logged as degraded
FAILURE_THRESHOLD = 3


class PeerSyncWorker:
    """Periodically fetches zone_data from peer directdnsonly instances and
    stores it locally so the healing pass can re-push missing zones without
    waiting for a DirectAdmin re-push."""

    def __init__(self, peer_sync_config: dict):
        self.enabled = peer_sync_config.get("enabled", False)
        self.interval_seconds = peer_sync_config.get("interval_minutes", 15) * 60
        self.peers = list(peer_sync_config.get("peers") or [])

        # Per-peer health state: url -> {consecutive_failures, healthy, last_seen}
        self._peer_health: dict = {}

        # ----------------------------------------------------------------
        # Env-var peer injection
        # ----------------------------------------------------------------
        # Original single-peer vars (backward compat):
        #   DADNS_PEER_SYNC_PEER_URL / _USERNAME / _PASSWORD
        # Numbered multi-peer vars (new):
        #   DADNS_PEER_SYNC_PEER_1_URL / _USERNAME / _PASSWORD
        #   DADNS_PEER_SYNC_PEER_2_URL / ...  (up to 9)
        known_urls = {p.get("url") for p in self.peers}

        env_candidates = []

        single_url = os.environ.get("DADNS_PEER_SYNC_PEER_URL", "").strip()
        if single_url:
            env_candidates.append({
                "url": single_url,
                "username": os.environ.get("DADNS_PEER_SYNC_PEER_USERNAME", "peersync"),
                "password": os.environ.get("DADNS_PEER_SYNC_PEER_PASSWORD", ""),
            })

        for i in range(1, 10):
            numbered_url = os.environ.get(f"DADNS_PEER_SYNC_PEER_{i}_URL", "").strip()
            if not numbered_url:
                break
            env_candidates.append({
                "url": numbered_url,
                "username": os.environ.get(
                    f"DADNS_PEER_SYNC_PEER_{i}_USERNAME", "peersync"
                ),
                "password": os.environ.get(f"DADNS_PEER_SYNC_PEER_{i}_PASSWORD", ""),
            })

        for candidate in env_candidates:
            if candidate["url"] not in known_urls:
                self.peers.append(candidate)
                known_urls.add(candidate["url"])
                logger.debug(
                    f"[peer_sync] Added peer from env vars: {candidate['url']}"
                )

        self._stop_event = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if not self.enabled:
            logger.info("Peer sync disabled — skipping")
            return
        if not self.peers:
            logger.warning("Peer sync enabled but no peers configured")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="peer_sync_worker"
        )
        self._thread.start()
        peer_urls = [p.get("url", "?") for p in self.peers]
        logger.info(
            f"Peer sync worker started — "
            f"interval: {self.interval_seconds // 60}m, "
            f"peers: {peer_urls}"
        )

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("Peer sync worker stopped")

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def get_peer_urls(self) -> list:
        """Return the current list of known peer URLs.
        Exposed via /internal/peers so other nodes can discover this node's mesh."""
        return [p["url"] for p in self.peers if p.get("url")]

    def get_peer_status(self) -> dict:
        """Return peer health summary for the /status endpoint."""
        peers = []
        for peer in self.peers:
            url = peer.get("url", "")
            h = self._peer_health.get(url, {})
            last_seen = h.get("last_seen")
            peers.append({
                "url": url,
                "healthy": h.get("healthy", True),
                "consecutive_failures": h.get("consecutive_failures", 0),
                "last_seen": last_seen.isoformat() if last_seen else None,
            })
        healthy = sum(1 for p in peers if p["healthy"])
        return {
            "enabled": self.enabled,
            "alive": self.is_alive,
            "interval_minutes": self.interval_seconds // 60,
            "peers": peers,
            "total": len(peers),
            "healthy": healthy,
            "degraded": len(peers) - healthy,
        }

    # ------------------------------------------------------------------
    # Health tracking
    # ------------------------------------------------------------------

    def _health(self, url: str) -> dict:
        return self._peer_health.setdefault(
            url, {"consecutive_failures": 0, "healthy": True, "last_seen": None}
        )

    def _record_success(self, url: str):
        h = self._health(url)
        recovered = not h["healthy"]
        h.update(
            consecutive_failures=0,
            healthy=True,
            last_seen=datetime.datetime.utcnow(),
        )
        if recovered:
            logger.info(f"[peer_sync] {url}: peer recovered")

    def _record_failure(self, url: str, exc):
        h = self._health(url)
        h["consecutive_failures"] += 1
        if h["healthy"] and h["consecutive_failures"] >= FAILURE_THRESHOLD:
            h["healthy"] = False
            logger.warning(
                f"[peer_sync] {url}: marked degraded after {FAILURE_THRESHOLD} "
                f"consecutive failures — {exc}"
            )
        else:
            logger.debug(
                f"[peer_sync] {url}: unreachable "
                f"(failure #{h['consecutive_failures']}) — {exc}"
            )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        logger.info("Peer sync worker starting — running initial sync now")
        self._sync_all()
        while not self._stop_event.wait(timeout=self.interval_seconds):
            self._sync_all()

    def _sync_all(self):
        logger.debug(f"[peer_sync] Starting sync pass across {len(self.peers)} peer(s)")
        # Iterate over a snapshot — _discover_peers_from may grow self.peers
        for peer in list(self.peers):
            url = peer.get("url")
            if not url:
                logger.warning("[peer_sync] Peer config missing url — skipping")
                continue
            try:
                self._sync_from_peer(peer)
                self._discover_peers_from(peer)
                self._record_success(url)
            except Exception as exc:
                self._record_failure(url, exc)

    def _discover_peers_from(self, peer: dict):
        """Fetch peer's known peer list and add any new nodes for mesh expansion.

        This is best-effort — failures are silently swallowed so they never
        interrupt the main sync pass."""
        url = peer.get("url", "").rstrip("/")
        username = peer.get("username")
        password = peer.get("password")
        auth = (username, password) if username else None
        try:
            resp = requests.get(f"{url}/internal/peers", auth=auth, timeout=5)
            if resp.status_code != 200:
                return
            remote_urls = resp.json()  # list of URL strings
            known_urls = {p.get("url") for p in self.peers}
            for remote_url in remote_urls:
                if remote_url and remote_url not in known_urls:
                    # Inherit credentials from the introducing peer — in practice
                    # all cluster nodes share the same peer_sync auth credentials.
                    self.peers.append({
                        "url": remote_url,
                        "username": username,
                        "password": password,
                    })
                    known_urls.add(remote_url)
                    logger.info(
                        f"[peer_sync] Discovered new peer {remote_url} via {url}"
                    )
        except Exception:
            pass  # discovery is best-effort

    def _sync_from_peer(self, peer: dict):
        url = peer.get("url", "").rstrip("/")
        username = peer.get("username")
        password = peer.get("password")
        auth = (username, password) if username else None

        # Fetch the peer's zone list
        resp = requests.get(f"{url}/internal/zones", auth=auth, timeout=10)
        if resp.status_code != 200:
            logger.warning(
                f"[peer_sync] {url}: /internal/zones returned {resp.status_code}"
            )
            return

        peer_zones = resp.json()  # [{domain, zone_updated_at, hostname, username}]
        if not peer_zones:
            logger.debug(f"[peer_sync] {url}: no zone_data on peer yet")
            return

        session = connect()
        try:
            synced = 0
            for entry in peer_zones:
                domain = entry.get("domain")
                if not domain:
                    continue

                peer_ts_str = entry.get("zone_updated_at")
                peer_ts = (
                    datetime.datetime.fromisoformat(peer_ts_str)
                    if peer_ts_str
                    else None
                )

                local = session.execute(
                    select(Domain).filter_by(domain=domain)
                ).scalar_one_or_none()

                needs_sync = (
                    local is None
                    or local.zone_data is None
                    or (peer_ts and not local.zone_updated_at)
                    or (
                        peer_ts
                        and local.zone_updated_at
                        and peer_ts > local.zone_updated_at
                    )
                )

                if not needs_sync:
                    continue

                # Fetch full zone_data from peer
                zresp = requests.get(
                    f"{url}/internal/zones",
                    params={"domain": domain},
                    auth=auth,
                    timeout=10,
                )
                if zresp.status_code != 200:
                    logger.warning(
                        f"[peer_sync] {url}: could not fetch zone_data "
                        f"for {domain} (HTTP {zresp.status_code})"
                    )
                    continue

                zdata = zresp.json()
                zone_data = zdata.get("zone_data")
                if not zone_data:
                    continue

                if local is None:
                    local = Domain(
                        domain=domain,
                        hostname=entry.get("hostname"),
                        username=entry.get("username"),
                        zone_data=zone_data,
                        zone_updated_at=peer_ts,
                    )
                    session.add(local)
                    logger.debug(
                        f"[peer_sync] {url}: created local record for {domain}"
                    )
                else:
                    local.zone_data = zone_data
                    local.zone_updated_at = peer_ts
                    logger.debug(f"[peer_sync] {url}: updated zone_data for {domain}")
                synced += 1

            if synced:
                session.commit()
                logger.info(f"[peer_sync] Synced {synced} zone(s) from {url}")
            else:
                logger.debug(f"[peer_sync] {url}: already up to date")
        finally:
            session.close()
