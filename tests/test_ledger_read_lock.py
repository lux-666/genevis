"""Readers wait for the writer's committed prefix, not a partial JSON line."""

import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.storage.ledger import Ledger, LedgerFormatError
from app.storage.paths import SessionPaths, ensure_run_layout


def test_reader_waits_for_complete_append(tmp_path):
    paths = SessionPaths(tmp_path, "locked")
    ensure_run_layout(paths)
    ledger = Ledger(paths.run_dir)
    row = ledger.record_event("locked", "probe", {"value": 1})
    text = json.dumps(row) + "\n"
    started = Event()

    def read():
        started.set()
        return ledger.read_event_rows("locked")

    with ledger.events_path.open("r+", encoding="utf-8") as writer:
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX)
        writer.truncate(0)
        writer.write(text[:20])
        writer.flush()
        with ThreadPoolExecutor() as pool:
            future = pool.submit(read)
            try:
                assert started.wait(2)
                from concurrent.futures import TimeoutError

                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
                writer.write(text[20:])
                writer.flush()
            finally:
                fcntl.flock(writer.fileno(), fcntl.LOCK_UN)
            assert future.result(timeout=2) == [row]
    ledger.events_path.write_text('{"incomplete":')
    with pytest.raises(LedgerFormatError):
        ledger.read_event_rows("locked")


def test_read_missing_ledger_does_not_create_run(tmp_path):
    paths = SessionPaths(tmp_path, "missing")
    ledger = Ledger(paths.run_dir)
    assert ledger.read_event_rows("missing") == []
    assert not paths.run_dir.exists()
