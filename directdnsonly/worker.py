import datetime
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from persistqueue import Queue
from persistqueue.exceptions import Empty
from sqlalchemy import select

from app.utils import check_zone_exists, put_zone_index, update_zone_hostname
from app.utils.zone_parser import count_zone_records
from directdnsonly.app.db.models import Domain
from directdnsonly.app.db import connect
from directdnsonly.app.reconciler import ReconciliationWorker
from directdnsonly.app.peer_sync import PeerSyncWorker

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 5
# Seconds to wait before each retry attempt (exponential-ish backoff)
BACKOFF_SECONDS = [30, 120, 300, 900, 1800]  # 30s, 2m, 5m, 15m, 30m
RETRY_DRAIN_INTERVAL = 30  # how often the retry drain thread wakes


class WorkerManager:
    def __init__(
        self,
        queue_path: str,
        backend_registry,
        reconciliation_config: dict = None,
        peer_sync_config: dict = None,
    ):
        self.queue_path = queue_path
        self.backend_registry = backend_registry
        self._running = False
        self._save_thread = None
        self._delete_thread = None
        self._retry_thread = None
        self._reconciler = None
        self._peer_syncer = None
        self._reconciliation_config = reconciliation_config or {}
        self._peer_sync_config = peer_sync_config or {}
        self._dead_letter_count = 0

        try:
            os.makedirs(queue_path, exist_ok=True)
            self.save_queue = Queue(f"{queue_path}/save")
            self.delete_queue = Queue(f"{queue_path}/delete")
            self.retry_queue = Queue(f"{queue_path}/retry")
            logger.success(f"Initialized queues at {queue_path}")
        except Exception as e:
            logger.critical(f"Failed to initialize queues: {e}")
            raise

    # ------------------------------------------------------------------
    # Save queue worker
    # ------------------------------------------------------------------

    def _process_save_queue(self):
        logger.info("Save queue worker started")
        session = connect()

        while self._running:
            # Block until at least one item is available
            try:
                item = self.save_queue.get(block=True, timeout=5)
            except Empty:
                continue

            # Open a batch and keep processing until the queue is empty
            batch_start = time.monotonic()
            batch_processed = 0
            batch_failed = 0
            logger.info("📥 Batch started")

            while True:
                try:
                    domain = item.get("domain", "unknown")
                    is_retry = item.get("source") in ("retry", "reconciler_heal")
                    target_backends = item.get("failed_backends")  # None = all backends

                    logger.debug(
                        f"Processing zone update for {domain}"
                        + (f" [retry #{item.get('retry_count', 0)}]" if is_retry else "")
                        + (f" [backends: {target_backends}]" if target_backends else "")
                    )

                    if not is_retry:
                        if not check_zone_exists(domain):
                            put_zone_index(domain, item.get("hostname"), item.get("username"))
                        else:
                            update_zone_hostname(domain, item.get("hostname"), item.get("username"))

                    if not all(k in item for k in ["domain", "zone_file"]):
                        logger.error(f"Invalid queue item: {item}")
                        self.save_queue.task_done()
                        batch_failed += 1
                    else:
                        backends = self.backend_registry.get_available_backends()
                        if target_backends:
                            backends = {
                                k: v for k, v in backends.items() if k in target_backends
                            }
                        if not backends:
                            logger.warning("No target backends available for this item!")
                            self.save_queue.task_done()
                            batch_failed += 1
                        else:
                            if len(backends) > 1:
                                failed = self._process_backends_parallel(backends, item, session)
                            else:
                                failed = set()
                                for backend_name, backend in backends.items():
                                    if not self._process_single_backend(
                                        backend_name, backend, item, session
                                    ):
                                        failed.add(backend_name)

                            if failed:
                                self._schedule_retry(item, failed)
                                batch_failed += 1
                            else:
                                self._store_zone_data(session, domain, item["zone_file"])
                                batch_processed += 1

                            self.save_queue.task_done()
                            logger.debug(f"Completed processing for {domain}")

                except Exception as e:
                    logger.error(f"Unexpected worker error processing {item.get('domain', '?')}: {e}")
                    batch_failed += 1
                    time.sleep(1)

                # Check immediately for the next item — keep batch open while
                # more work is queued; close it only when the queue is empty.
                try:
                    item = self.save_queue.get_nowait()
                except Empty:
                    break

            elapsed = time.monotonic() - batch_start
            total = batch_processed + batch_failed
            rate = batch_processed / elapsed if elapsed > 0 else 0
            logger.success(
                f"📦 Batch complete — {batch_processed}/{total} zone(s) "
                f"processed successfully in {elapsed:.1f}s "
                f"({rate:.1f} zones/sec)"
                + (f", {batch_failed} failed" if batch_failed else "")
            )

    def _process_single_backend(self, backend_name, backend, item, session) -> bool:
        """Write a zone to one backend. Returns True on success, False on failure."""
        try:
            if backend.write_zone(item["domain"], item["zone_file"]):
                logger.debug(f"Successfully updated {item['domain']} in {backend_name}")
                if backend.get_name() == "bind":
                    backend.update_named_conf(
                        [d.domain for d in session.execute(select(Domain)).scalars().all()]
                    )
                    backend.reload_zone()
                else:
                    backend.reload_zone(zone_name=item["domain"])
                self._verify_backend_record_count(
                    backend_name, backend, item["domain"], item["zone_file"]
                )
                return True
            else:
                logger.error(f"Failed to update {item['domain']} in {backend_name}")
                return False
        except Exception as e:
            logger.error(f"Error in {backend_name}: {str(e)}")
            return False

    def _process_backends_parallel(self, backends, item, session) -> set:
        """Write a zone to multiple backends concurrently.
        Returns a set of backend names that failed."""
        start_time = time.monotonic()
        failed = set()
        with ThreadPoolExecutor(
            max_workers=len(backends), thread_name_prefix="backend"
        ) as executor:
            futures = {
                executor.submit(
                    self._process_single_backend, backend_name, backend, item, session
                ): backend_name
                for backend_name, backend in backends.items()
            }
            for future in as_completed(futures):
                backend_name = futures[future]
                try:
                    success = future.result()
                    if not success:
                        failed.add(backend_name)
                except Exception as e:
                    logger.error(f"Unhandled error in backend {backend_name}: {e}")
                    failed.add(backend_name)
        elapsed = (time.monotonic() - start_time) * 1000
        logger.debug(
            f"Parallel processing of {item['domain']} across "
            f"{len(backends)} backends completed in {elapsed:.0f}ms"
        )
        return failed

    def _schedule_retry(self, item: dict, failed_backends: set):
        """Push a failed write onto the retry queue with exponential backoff.
        Discards to dead-letter after MAX_RETRIES attempts."""
        retry_count = item.get("retry_count", 0) + 1
        if retry_count > MAX_RETRIES:
            self._dead_letter_count += 1
            logger.error(
                f"[retry] Dead-letter: {item['domain']} failed on "
                f"{failed_backends} after {MAX_RETRIES} attempts — giving up"
            )
            return
        delay = BACKOFF_SECONDS[min(retry_count - 1, len(BACKOFF_SECONDS) - 1)]
        retry_item = {
            **item,
            "failed_backends": list(failed_backends),
            "retry_count": retry_count,
            "retry_after": time.time() + delay,
            "source": "retry",
        }
        self.retry_queue.put(retry_item)
        logger.warning(
            f"[retry] {item['domain']} → {list(failed_backends)} "
            f"scheduled for retry #{retry_count} in {delay}s"
        )

    def _store_zone_data(self, session, domain: str, zone_file: str):
        """Persist the latest zone file content to the domain DB record."""
        try:
            record = session.execute(
                select(Domain).filter_by(domain=domain)
            ).scalar_one_or_none()
            if record:
                record.zone_data = zone_file
                record.zone_updated_at = datetime.datetime.utcnow()
                session.commit()
        except Exception as exc:
            logger.warning(f"[worker] Could not store zone_data for {domain}: {exc}")

    # ------------------------------------------------------------------
    # Retry drain worker
    # ------------------------------------------------------------------

    def _process_retry_queue(self):
        """Periodically drain the retry queue and re-feed ready items to the
        save queue. Items not yet due are put back onto the retry queue."""
        logger.info("Retry drain worker started")
        while self._running:
            time.sleep(RETRY_DRAIN_INTERVAL)
            now = time.time()
            pending = []
            # Drain all current retry items into memory
            while True:
                try:
                    pending.append(self.retry_queue.get_nowait())
                    self.retry_queue.task_done()
                except Empty:
                    break

            if not pending:
                continue

            ready = [i for i in pending if i.get("retry_after", 0) <= now]
            not_ready = [i for i in pending if i.get("retry_after", 0) > now]

            for item in not_ready:
                self.retry_queue.put(item)

            for item in ready:
                logger.info(
                    f"[retry] Re-queuing {item['domain']} → "
                    f"{item.get('failed_backends')} "
                    f"(attempt #{item.get('retry_count', '?')})"
                )
                self.save_queue.put(item)

            if ready:
                logger.debug(
                    f"[retry] Drain: {len(ready)} item(s) ready, "
                    f"{len(not_ready)} still pending"
                )

    # ------------------------------------------------------------------
    # Delete queue worker
    # ------------------------------------------------------------------

    def _process_delete_queue(self):
        logger.info("Delete queue worker started")
        session = connect()

        while self._running:
            try:
                item = self.delete_queue.get(block=True, timeout=5)
                domain = item.get("domain")
                hostname = item.get("hostname", "")

                logger.debug(f"Processing delete for {domain}")

                record = session.execute(
                    select(Domain).filter_by(domain=domain)
                ).scalar_one_or_none()
                if not record:
                    logger.warning(f"Domain {domain} not found in DB — skipping delete")
                    self.delete_queue.task_done()
                    continue

                if record.hostname and record.hostname != hostname:
                    logger.warning(
                        f"[migration] Delete rejected for {domain}: zone is owned by "
                        f"{record.hostname} but delete arrived from {hostname} — "
                        f"did the old server remove the domain without checking 'Keep DNS'?"
                    )
                    self.delete_queue.task_done()
                    continue
                if not record.hostname:
                    logger.warning(
                        f"No origin hostname stored for {domain} — "
                        f"skipping ownership check, proceeding with delete"
                    )

                backends = self.backend_registry.get_available_backends()
                remaining_domains = [
                    d.domain for d in session.execute(select(Domain)).scalars().all()
                ]
                delete_success = True

                if not backends:
                    logger.warning(
                        f"No active backends — {domain} will be removed from DB only"
                    )
                elif len(backends) > 1:
                    results = []
                    with ThreadPoolExecutor(max_workers=len(backends)) as executor:
                        futures = {
                            executor.submit(
                                self._delete_single_backend,
                                backend_name,
                                backend,
                                domain,
                                remaining_domains,
                            ): backend_name
                            for backend_name, backend in backends.items()
                        }
                        for future in as_completed(futures):
                            backend_name = futures[future]
                            try:
                                results.append(future.result())
                            except Exception as e:
                                logger.error(
                                    f"Unhandled error deleting from {backend_name}: {e}"
                                )
                                results.append(False)
                    delete_success = all(results)
                else:
                    for backend_name, backend in backends.items():
                        if not self._delete_single_backend(
                            backend_name, backend, domain, remaining_domains
                        ):
                            delete_success = False

                if delete_success:
                    session.delete(record)
                    session.commit()
                    logger.success(f"Delete completed for {domain}")
                else:
                    logger.error(
                        f"Delete failed for {domain} on one or more backends — "
                        f"DB record retained"
                    )
                self.delete_queue.task_done()

            except Empty:
                continue
            except Exception as e:
                logger.error(f"Unexpected delete worker error: {e}")
                time.sleep(1)

    def _delete_single_backend(
        self, backend_name, backend, domain, remaining_domains
    ) -> bool:
        """Delete a zone from one backend. Returns True on success."""
        try:
            if backend.delete_zone(domain):
                logger.debug(f"Deleted {domain} from {backend_name}")
                if backend.get_name() == "bind":
                    backend.update_named_conf(remaining_domains)
                    backend.reload_zone()
                else:
                    backend.reload_zone(zone_name=domain)
                return True
            else:
                logger.error(f"Failed to delete {domain} from {backend_name}")
                return False
        except Exception as e:
            logger.error(f"Error deleting {domain} from {backend_name}: {e}")
            return False

    # ------------------------------------------------------------------
    # Record count verification
    # ------------------------------------------------------------------

    def _verify_backend_record_count(self, backend_name, backend, zone_name, zone_data):
        try:
            expected = count_zone_records(zone_data, zone_name)
            if expected < 0:
                logger.warning(
                    f"[{backend_name}] Could not parse source zone for "
                    f"{zone_name} — skipping record count verification"
                )
                return

            matches, actual = backend.verify_zone_record_count(zone_name, expected)
            if matches:
                return

            if actual > expected:
                logger.warning(
                    f"[{backend_name}] Backend has {actual - expected} extra "
                    f"record(s) for {zone_name} — reconciling"
                )
                success, removed = backend.reconcile_zone_records(zone_name, zone_data)
                if success and removed > 0:
                    matches, new_count = backend.verify_zone_record_count(
                        zone_name, expected
                    )
                    if matches:
                        logger.success(
                            f"[{backend_name}] Reconciliation successful for "
                            f"{zone_name}: removed {removed} extra record(s)"
                        )
                    else:
                        logger.error(
                            f"[{backend_name}] Reconciliation for {zone_name} "
                            f"removed {removed} record(s) but count still mismatched: "
                            f"expected {expected}, got {new_count}"
                        )
            else:
                logger.warning(
                    f"[{backend_name}] Backend has fewer records than source "
                    f"for {zone_name} (expected {expected}, got {actual}) — "
                    f"next zone push from DirectAdmin should correct this"
                )

        except NotImplementedError:
            logger.debug(
                f"[{backend_name}] Record count verification not supported — skipping"
            )
        except Exception as e:
            logger.error(
                f"[{backend_name}] Error during record count verification "
                f"for {zone_name}: {e}"
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True

        self._save_thread = threading.Thread(
            target=self._process_save_queue, daemon=True, name="save_queue_worker"
        )
        self._delete_thread = threading.Thread(
            target=self._process_delete_queue, daemon=True, name="delete_queue_worker"
        )
        self._retry_thread = threading.Thread(
            target=self._process_retry_queue, daemon=True, name="retry_drain_worker"
        )
        self._save_thread.start()
        self._delete_thread.start()
        self._retry_thread.start()
        logger.info(f"Started worker threads: save, delete, retry_drain")

        self._reconciler = ReconciliationWorker(
            delete_queue=self.delete_queue,
            save_queue=self.save_queue,
            backend_registry=self.backend_registry,
            reconciliation_config=self._reconciliation_config,
        )
        self._reconciler.start()

        self._peer_syncer = PeerSyncWorker(self._peer_sync_config)
        self._peer_syncer.start()

    def stop(self):
        self._running = False
        if self._reconciler:
            self._reconciler.stop()
        if self._peer_syncer:
            self._peer_syncer.stop()
        for thread in (self._save_thread, self._delete_thread, self._retry_thread):
            if thread:
                thread.join(timeout=5)
        logger.info("Workers stopped")

    def queue_status(self):
        reconciler = (
            self._reconciler.get_status()
            if self._reconciler
            else {"enabled": False, "alive": False, "last_run": {}}
        )
        peer_sync = (
            self._peer_syncer.get_peer_status()
            if self._peer_syncer
            else {"enabled": False, "alive": False, "peers": [], "total": 0, "healthy": 0, "degraded": 0}
        )
        return {
            "save_queue_size": self.save_queue.qsize(),
            "delete_queue_size": self.delete_queue.qsize(),
            "retry_queue_size": self.retry_queue.qsize(),
            "dead_letters": self._dead_letter_count,
            "save_worker_alive": bool(self._save_thread and self._save_thread.is_alive()),
            "delete_worker_alive": bool(self._delete_thread and self._delete_thread.is_alive()),
            "retry_worker_alive": bool(self._retry_thread and self._retry_thread.is_alive()),
            "reconciler": reconciler,
            "peer_sync": peer_sync,
        }
