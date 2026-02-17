import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from loguru import logger
from persistqueue import Queue
from persistqueue.exceptions import Empty

from app.utils import check_zone_exists, put_zone_index
from app.utils.zone_parser import count_zone_records
from directdnsonly.app.db.models import Domain
from directdnsonly.app.db import connect


class WorkerManager:
    def __init__(self, queue_path: str, backend_registry):
        self.queue_path = queue_path
        self.backend_registry = backend_registry
        self._running = False
        self._thread = None

        # Initialize queues with error handling
        try:
            os.makedirs(queue_path, exist_ok=True)
            self.save_queue = Queue(f"{queue_path}/save")
            self.delete_queue = Queue(f"{queue_path}/delete")
            logger.success(f"Initialized queues at {queue_path}")
        except Exception as e:
            logger.critical(f"Failed to initialize queues: {e}")
            raise

    def _process_save_queue(self):
        """Main worker loop for processing save requests"""
        logger.info("Save queue worker started")
        # Get DB Connection
        session = connect()

        # Batch tracking
        batch_start = None
        batch_processed = 0
        batch_failed = 0

        while self._running:
            try:
                item = self.save_queue.get(block=True, timeout=5)

                # Start a new batch timer on the first item
                if batch_start is None:
                    batch_start = time.monotonic()
                    batch_processed = 0
                    batch_failed = 0
                    pending = self.save_queue.qsize()
                    logger.info(
                        f"📥 Batch started — {pending + 1} zone(s) queued "
                        f"for processing"
                    )

                logger.debug(
                    f"Processing zone update for {item.get('domain', 'unknown')}"
                )

                if not check_zone_exists(item.get("domain")):
                    put_zone_index(
                        item.get("domain"), item.get("hostname"), item.get("username")
                    )
                # Validate item structure
                if not all(k in item for k in ["domain", "zone_file"]):
                    logger.error(f"Invalid queue item: {item}")
                    self.save_queue.task_done()
                    batch_failed += 1
                    continue

                # Process with all available backends
                backends = self.backend_registry.get_available_backends()
                if not backends:
                    logger.warning("No active backends available!")

                if len(backends) > 1:
                    # Process backends in parallel for faster sync
                    logger.debug(
                        f"Processing {item['domain']} across "
                        f"{len(backends)} backends concurrently: "
                        f"{', '.join(backends.keys())}"
                    )
                    self._process_backends_parallel(
                        backends, item, session
                    )
                else:
                    # Single backend, no need for thread overhead
                    for backend_name, backend in backends.items():
                        self._process_single_backend(
                            backend_name, backend, item, session
                        )

                self.save_queue.task_done()
                batch_processed += 1
                logger.debug(f"Completed processing for {item['domain']}")

            except Empty:
                # Queue is empty — if we were in a batch, log the summary
                if batch_start is not None:
                    elapsed = time.monotonic() - batch_start
                    total = batch_processed + batch_failed
                    rate = batch_processed / elapsed if elapsed > 0 else 0
                    logger.success(
                        f"📦 Batch complete — {batch_processed}/{total} zone(s) "
                        f"processed successfully in {elapsed:.1f}s "
                        f"({rate:.1f} zones/sec)"
                        + (f", {batch_failed} failed" if batch_failed else "")
                    )
                    batch_start = None
                    batch_processed = 0
                    batch_failed = 0
                continue
            except Exception as e:
                logger.error(f"Unexpected worker error: {e}")
                batch_failed += 1
                time.sleep(1)  # Prevent tight error loops

    def _process_single_backend(self, backend_name, backend, item, session):
        """Process a zone update for a single backend"""
        try:
            logger.debug(f"Using backend: {backend_name}")
            if backend.write_zone(item["domain"], item["zone_file"]):
                logger.debug(
                    f"Successfully updated {item['domain']} in {backend_name}"
                )
                if backend.get_name() == "bind":
                    # Need to update the named.conf
                    backend.update_named_conf(
                        [d.domain for d in session.query(Domain).all()]
                    )
                    # Reload all zones
                    backend.reload_zone()
                else:
                    backend.reload_zone(zone_name=item["domain"])

                # Verify record count matches the source zone from DirectAdmin
                self._verify_backend_record_count(
                    backend_name, backend, item["domain"], item["zone_file"]
                )
            else:
                logger.error(
                    f"Failed to update {item['domain']} in {backend_name}"
                )
        except Exception as e:
            logger.error(f"Error in {backend_name}: {str(e)}")

    def _process_backends_parallel(self, backends, item, session):
        """Process zone updates across multiple backends in parallel"""
        start_time = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=len(backends),
            thread_name_prefix="backend"
        ) as executor:
            futures = {
                executor.submit(
                    self._process_single_backend,
                    backend_name, backend, item, session
                ): backend_name
                for backend_name, backend in backends.items()
            }
            for future in as_completed(futures):
                backend_name = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(
                        f"Unhandled error processing backend "
                        f"{backend_name}: {str(e)}"
                    )
        elapsed = (time.monotonic() - start_time) * 1000
        logger.debug(
            f"Parallel processing of {item['domain']} across "
            f"{len(backends)} backends completed in {elapsed:.0f}ms"
        )

    def _verify_backend_record_count(
        self, backend_name, backend, zone_name, zone_data
    ):
        """Verify and reconcile the backend record count against the
        authoritative BIND zone from DirectAdmin.

        After a successful write, this method checks whether the number of
        records stored in the backend matches the number of records parsed
        from the source zone file.  If there are **extra** records in the
        backend (e.g. from replication drift or stale data) they are
        automatically removed via the backend's reconcile method.

        Args:
            backend_name: Display name of the backend instance
            backend: The backend instance
            zone_name: The zone that was just written
            zone_data: The raw BIND zone file content (authoritative source)
        """
        try:
            expected = count_zone_records(zone_data, zone_name)
            if expected < 0:
                logger.warning(
                    f"[{backend_name}] Could not parse source zone for "
                    f"{zone_name} — skipping record count verification"
                )
                return

            matches, actual = backend.verify_zone_record_count(
                zone_name, expected
            )

            if matches:
                return  # All good

            if actual > expected:
                logger.warning(
                    f"[{backend_name}] Backend has {actual - expected} extra "
                    f"record(s) for {zone_name} — reconciling against "
                    f"DirectAdmin source zone"
                )
                success, removed = backend.reconcile_zone_records(
                    zone_name, zone_data
                )
                if success and removed > 0:
                    # Verify again after reconciliation
                    matches, new_count = backend.verify_zone_record_count(
                        zone_name, expected
                    )
                    if matches:
                        logger.success(
                            f"[{backend_name}] Reconciliation successful for "
                            f"{zone_name}: removed {removed} extra record(s), "
                            f"count now matches source ({new_count})"
                        )
                    else:
                        logger.error(
                            f"[{backend_name}] Reconciliation for {zone_name} "
                            f"removed {removed} record(s) but count still "
                            f"mismatched: expected {expected}, got {new_count}"
                        )
            else:
                logger.warning(
                    f"[{backend_name}] Backend has fewer records than source "
                    f"for {zone_name} (expected {expected}, got {actual}) — "
                    f"this may indicate a write failure; the next zone push "
                    f"from DirectAdmin should correct this"
                )

        except NotImplementedError:
            logger.debug(
                f"[{backend_name}] Record count verification not "
                f"supported — skipping"
            )
        except Exception as e:
            logger.error(
                f"[{backend_name}] Error during record count verification "
                f"for {zone_name}: {e}"
            )

    def start(self):
        """Start background workers"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._process_save_queue, daemon=True, name="save_queue_worker"
        )
        self._thread.start()
        logger.info(f"Started worker thread {self._thread.name}")

    def stop(self):
        """Stop background workers gracefully"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Workers stopped")

    def queue_status(self):
        """Return current queue status"""
        return {
            "save_queue_size": self.save_queue.qsize(),
            "delete_queue_size": self.delete_queue.qsize(),
            "worker_alive": self._thread and self._thread.is_alive(),
        }
