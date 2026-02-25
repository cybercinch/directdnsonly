#!/usr/bin/env python3
import datetime
import threading
from loguru import logger
from sqlalchemy import select

from directdnsonly.app.da import DirectAdminClient
from directdnsonly.app.db import connect
from directdnsonly.app.db.models import Domain


class ReconciliationWorker:
    """Periodically polls configured DirectAdmin servers and queues deletes
    for any zones in our DB that no longer exist in DirectAdmin.

    Also runs an Option C backend healing pass: for each zone with stored
    zone_data, checks every backend for presence and re-queues any that are
    missing (e.g. after a prolonged backend outage).

    Safety rules:
    - If a DA server is unreachable, skip it entirely — never delete on uncertainty
    - Only touches domains registered via DaDNS (present in our `domains` table)
    - Domains in CoreDNS but NOT in our DB are not our zones; left untouched
    - Pushes to the existing delete_queue so the full delete path is exercised
    """

    def __init__(
        self,
        delete_queue,
        reconciliation_config: dict,
        save_queue=None,
        backend_registry=None,
    ):
        self.delete_queue = delete_queue
        self.save_queue = save_queue
        self.backend_registry = backend_registry
        self.enabled = reconciliation_config.get("enabled", False)
        self.interval_seconds = reconciliation_config.get("interval_minutes", 60) * 60
        self.servers = reconciliation_config.get("directadmin_servers") or []
        self.verify_ssl = reconciliation_config.get("verify_ssl", True)
        self.ipp = int(reconciliation_config.get("ipp", 1000))
        self.dry_run = bool(reconciliation_config.get("dry_run", False))
        self._initial_delay = reconciliation_config.get("initial_delay_minutes", 0) * 60
        self._stop_event = threading.Event()
        self._thread = None
        self._last_run: dict = {}

    def get_status(self) -> dict:
        """Return reconciler configuration and last-run statistics."""
        return {
            "enabled": self.enabled,
            "alive": self.is_alive,
            "dry_run": self.dry_run,
            "interval_minutes": self.interval_seconds // 60,
            "last_run": dict(self._last_run),
        }

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
        delay_str = (
            f", initial_delay: {self._initial_delay // 60}m"
            if self._initial_delay
            else ""
        )
        logger.info(
            f"Reconciliation poller started [{mode}] — "
            f"interval: {self.interval_seconds // 60}m"
            f"{delay_str}, "
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
        if self._initial_delay > 0:
            logger.info(
                f"[reconciler] Initial delay {self._initial_delay // 60}m — "
                f"first reconciliation pass deferred"
            )
            if self._stop_event.wait(timeout=self._initial_delay):
                return  # stopped cleanly during the initial delay
        logger.info("Reconciliation worker starting — running initial check now")
        self._reconcile_all()
        while not self._stop_event.wait(timeout=self.interval_seconds):
            self._reconcile_all()

    def _reconcile_all(self):
        started_at = datetime.datetime.utcnow()
        self._last_run = {"status": "running", "started_at": started_at.isoformat()}
        logger.info(
            f"[reconciler] Starting reconciliation pass across "
            f"{len(self.servers)} server(s)"
        )
        total_queued = 0
        da_servers_polled = 0
        da_servers_unreachable = 0
        migrated = 0
        backfilled = 0
        zones_in_db = 0

        # Build a map of all domains seen on all DA servers: domain -> hostname
        all_da_domains: dict = {}
        for server in self.servers:
            hostname = server.get("hostname")
            if not hostname:
                logger.warning("[reconciler] Server config missing hostname — skipping")
                continue
            try:
                client = DirectAdminClient(
                    hostname=hostname,
                    port=server.get("port", 2222),
                    username=server.get("username"),
                    password=server.get("password"),
                    ssl=server.get("ssl", True),
                    verify_ssl=self.verify_ssl,
                )
                da_servers_polled += 1
                da_domains = client.list_domains(ipp=self.ipp)
                if da_domains is not None:
                    for d in da_domains:
                        all_da_domains[d] = hostname
                else:
                    da_servers_unreachable += 1
                logger.debug(
                    f"[reconciler] {hostname}: "
                    f"{len(da_domains) if da_domains else 0} active domain(s) in DA"
                )
            except Exception as exc:
                logger.error(f"[reconciler] Unexpected error polling {hostname}: {exc}")
                da_servers_unreachable += 1

        # Compare local DB against what DA reported; update masters and queue deletes
        session = connect()
        try:
            all_local_domains = session.execute(select(Domain)).scalars().all()
            zones_in_db = len(all_local_domains)
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

        # Option C: heal backends that are missing zones
        zones_healed = 0
        if self.save_queue is not None and self.backend_registry is not None:
            zones_healed = self._heal_backends()

        completed_at = datetime.datetime.utcnow()
        self._last_run = {
            "status": "ok",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": round(
                (completed_at - started_at).total_seconds(), 1
            ),
            "da_servers_polled": da_servers_polled,
            "da_servers_unreachable": da_servers_unreachable,
            "zones_in_da": len(all_da_domains),
            "zones_in_db": zones_in_db,
            "orphans_found": total_queued,
            "orphans_queued": total_queued if not self.dry_run else 0,
            "hostnames_backfilled": backfilled,
            "hostnames_migrated": migrated,
            "zones_healed": zones_healed,
            "dry_run": self.dry_run,
        }

    def _heal_backends(self) -> int:
        """Check every backend for zone presence and re-queue any zone that is
        missing from one or more backends, using the stored zone_data as the
        authoritative source.  This corrects backends that missed pushes due to
        downtime without waiting for DirectAdmin to re-send the zone.
        """
        backends = self.backend_registry.get_available_backends()
        if not backends:
            return 0

        session = connect()
        healed = 0
        try:
            domains = session.execute(
                    select(Domain).where(Domain.zone_data.isnot(None))
                ).scalars().all()
            if not domains:
                logger.debug(
                    "[reconciler] Healing pass: no zone_data stored yet — skipping"
                )
                return 0
            for record in domains:
                missing = []
                for backend_name, backend in backends.items():
                    try:
                        if not backend.zone_exists(record.domain):
                            missing.append(backend_name)
                    except Exception as exc:
                        logger.warning(
                            f"[reconciler] heal: zone_exists check failed for "
                            f"{record.domain} on {backend_name}: {exc}"
                        )

                if missing:
                    mode = "[DRY-RUN] Would heal" if self.dry_run else "Healing"
                    logger.warning(
                        f"[reconciler] {mode} — {record.domain} missing from "
                        f"{missing}; re-queuing with stored zone_data"
                    )
                    if not self.dry_run:
                        self.save_queue.put(
                            {
                                "domain": record.domain,
                                "hostname": record.hostname or "",
                                "username": record.username or "",
                                "zone_file": record.zone_data,
                                "failed_backends": missing,
                                "retry_count": 0,
                                "source": "reconciler_heal",
                            }
                        )
                        healed += 1

            if healed:
                logger.info(
                    f"[reconciler] Healing pass complete — "
                    f"{healed} zone(s) re-queued for backend recovery"
                )
            else:
                logger.debug(
                    "[reconciler] Healing pass complete — all backends consistent"
                )
        finally:
            session.close()
        return healed
