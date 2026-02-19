#!/usr/bin/env python3
"""Peer sync worker — exchanges zone_data between directdnsonly instances.

Each node stores zone_data in its local SQLite DB after every successful
backend write.  When DirectAdmin pushes a zone to one node but the other
is temporarily offline, the offline node misses that zone_data.

PeerSyncWorker corrects this by periodically comparing zone lists with
configured peers and fetching any zone_data that is newer or absent locally.
It only updates the local DB — it never writes directly to backends.  The
existing reconciler healing pass then detects missing zones and re-pushes
using the freshly synced zone_data.

Safety properties:
- If a peer is unreachable, skip it silently and retry next interval
- Only zone_data is synced — backend writes remain the sole responsibility
  of the local save queue worker
- Newer zone_updated_at timestamp wins; local data is never overwritten
  with older peer data
"""
import datetime
import threading
from loguru import logger
import requests

from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain


class PeerSyncWorker:
    """Periodically fetches zone_data from peer directdnsonly instances and
    stores it locally so the healing pass can re-push missing zones without
    waiting for a DirectAdmin re-push."""

    def __init__(self, peer_sync_config: dict):
        self.enabled = peer_sync_config.get("enabled", False)
        self.interval_seconds = peer_sync_config.get("interval_minutes", 15) * 60
        self.peers = peer_sync_config.get("peers") or []
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        if not self.enabled:
            logger.info("Peer sync disabled — skipping")
            return
        if not self.peers:
            logger.warning(
                "Peer sync enabled but no peers configured"
            )
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

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        logger.info("Peer sync worker starting — running initial sync now")
        self._sync_all()
        while not self._stop_event.wait(timeout=self.interval_seconds):
            self._sync_all()

    def _sync_all(self):
        logger.debug(
            f"[peer_sync] Starting sync pass across {len(self.peers)} peer(s)"
        )
        for peer in self.peers:
            url = peer.get("url")
            if not url:
                logger.warning("[peer_sync] Peer config missing url — skipping")
                continue
            try:
                self._sync_from_peer(peer)
            except Exception as exc:
                logger.warning(
                    f"[peer_sync] Skipping unreachable peer {url}: {exc}"
                )

    def _sync_from_peer(self, peer: dict):
        url = peer.get("url", "").rstrip("/")
        username = peer.get("username")
        password = peer.get("password")
        auth = (username, password) if username else None

        # Fetch the peer's zone list
        resp = requests.get(
            f"{url}/internal/zones", auth=auth, timeout=10
        )
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

                local = session.query(Domain).filter_by(domain=domain).first()

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
                    logger.debug(
                        f"[peer_sync] {url}: updated zone_data for {domain}"
                    )
                synced += 1

            if synced:
                session.commit()
                logger.info(
                    f"[peer_sync] Synced {synced} zone(s) from {url}"
                )
            else:
                logger.debug(f"[peer_sync] {url}: already up to date")
        finally:
            session.close()
