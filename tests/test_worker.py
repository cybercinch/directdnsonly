"""Tests for WorkerManager — debounce/batching behaviour.

Strict TDD: these tests were written before the debounce fix and initially fail.
"""

import sys
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from loguru import logger

# worker.py uses short-form imports (from app.utils …) that only resolve when
# the directdnsonly/ package directory is on sys.path — the same manipulation
# __main__.py performs at runtime.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "directdnsonly"))

import directdnsonly.worker as worker_module
from directdnsonly.worker import WorkerManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zone_item(domain: str) -> dict:
    return {
        "domain": domain,
        "zone_file": f"$ORIGIN {domain}.\n@ 300 IN SOA ns1 hostmaster 1 3600 900 604800 300\n",
        "hostname": "da.example.com",
        "username": "admin",
    }


def _fast_backend() -> MagicMock:
    b = MagicMock()
    b.write_zone.return_value = True
    b.get_name.return_value = "mock"
    b.reload_zone.return_value = None
    b.verify_zone_record_count.side_effect = NotImplementedError
    return b


def _registry(backend: MagicMock) -> MagicMock:
    r = MagicMock()
    r.get_available_backends.return_value = {"mock": backend}
    return r


def _mock_session() -> MagicMock:
    s = MagicMock()
    # scalar_one_or_none used in _store_zone_data and _process_delete_queue
    s.execute.return_value.scalar_one_or_none.return_value = None
    s.execute.return_value.scalars.return_value.all.return_value = []
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_worker_internals(monkeypatch):
    """Patch all external I/O the save worker touches so tests stay hermetic."""
    monkeypatch.setattr(worker_module, "connect", lambda: _mock_session())
    monkeypatch.setattr(worker_module, "check_zone_exists", lambda d: False)
    monkeypatch.setattr(worker_module, "put_zone_index", lambda *a: None)
    monkeypatch.setattr(worker_module, "update_zone_hostname", lambda *a: None)


@pytest.fixture
def wm(tmp_path, monkeypatch):
    backend = _fast_backend()
    registry = _registry(backend)
    manager = WorkerManager(
        queue_path=str(tmp_path / "queues"),
        backend_registry=registry,
    )
    manager._backend = backend
    return manager


# ---------------------------------------------------------------------------
# Debounce / batching tests
# ---------------------------------------------------------------------------


def _capture_batch_complete_messages() -> tuple[list, int]:
    """Add a loguru sink and return (messages_list, handler_id)."""
    msgs: list[str] = []
    handler_id = logger.add(
        lambda m: msgs.append(m),
        format="{message}",
        level="SUCCESS",
        colorize=False,
    )
    return msgs, handler_id


def _run_save_worker(wm: WorkerManager, duration: float) -> threading.Thread:
    """Start the save worker and return the thread (caller must stop it)."""
    wm._running = True
    t = threading.Thread(target=wm._process_save_queue, daemon=True)
    t.start()
    if duration:
        time.sleep(duration)
        wm._running = False
        t.join(timeout=3)
    return t


class TestSaveQueueDebounce:
    def test_burst_items_land_in_single_batch(self, wm, monkeypatch):
        """3 items arriving within the debounce window must be processed as one batch.

        Without a debounce the first item is processed immediately; get_nowait()
        returns Empty before items 2+3 arrive, so each item becomes its own batch
        (observed as '1/1' log lines).  With debounce, the worker waits after
        picking up item 1, the other items accumulate, and one batch fires.
        """
        monkeypatch.setattr(worker_module, "BATCH_DEBOUNCE_SECONDS", 0.2)

        msgs, hid = _capture_batch_complete_messages()
        try:
            wm._running = True
            t = threading.Thread(target=wm._process_save_queue, daemon=True)
            t.start()

            # item 1 lands first; items 2+3 land within the debounce window
            wm.save_queue.put(_zone_item("a.example.com"))
            time.sleep(0.05)  # 50 ms — well inside the 200 ms debounce
            wm.save_queue.put(_zone_item("b.example.com"))
            wm.save_queue.put(_zone_item("c.example.com"))

            time.sleep(0.8)  # give the worker time to finish
            wm._running = False
            t.join(timeout=3)
        finally:
            logger.remove(hid)

        completed = [m for m in msgs if "Batch complete" in m]
        assert len(completed) == 1, (
            f"Expected 1 batch, got {len(completed)}: {completed}"
        )
        assert "3/3" in completed[0], f"Expected 3/3 in batch summary: {completed[0]}"

    def test_items_outside_debounce_window_start_new_batch(self, wm, monkeypatch):
        """Items arriving after the debounce window has elapsed start a new batch."""
        monkeypatch.setattr(worker_module, "BATCH_DEBOUNCE_SECONDS", 0.05)

        msgs, hid = _capture_batch_complete_messages()
        try:
            t = _run_save_worker(wm, duration=0)  # start without auto-stop

            wm.save_queue.put(_zone_item("first.example.com"))
            # Wait well beyond the debounce window before sending the second item
            time.sleep(0.4)
            wm.save_queue.put(_zone_item("second.example.com"))

            time.sleep(0.4)
            wm._running = False
            t.join(timeout=3)
        finally:
            logger.remove(hid)

        completed = [m for m in msgs if "Batch complete" in m]
        assert len(completed) == 2, (
            f"Expected 2 separate batches, got {len(completed)}: {completed}"
        )

    def test_preloaded_queue_still_batches(self, wm, monkeypatch):
        """Items already in the queue before the worker starts are batched together."""
        monkeypatch.setattr(worker_module, "BATCH_DEBOUNCE_SECONDS", 0.1)

        for i in range(5):
            wm.save_queue.put(_zone_item(f"zone{i}.example.com"))

        msgs, hid = _capture_batch_complete_messages()
        try:
            _run_save_worker(wm, duration=1.0)
        finally:
            logger.remove(hid)

        completed = [m for m in msgs if "Batch complete" in m]
        assert len(completed) == 1, f"Expected 1 batch, got {len(completed)}: {completed}"
        assert "5/5" in completed[0], f"Expected 5/5: {completed[0]}"
