"""D-19 runbook store: thread-safe FIFO with id minting.

R10 store_mints_id_and_indexes: add() returns a unique id, list_recent
    returns it; the id has the form RB-YYYYMMDD-NNNN.
R11 store_caps_at_max: adding 51 to a 50-cap store drops the oldest;
    the most recent 50 are still accessible.
R12 store_thread_safe: adding from 4 threads in parallel yields 4
    distinct ids, no duplicates; len(store) == 4.
R13 list_recent_respects_limit: limit=2 returns 2; limit=0 returns [].
R14 get_unknown_returns_none: get("RB-99999999-9999") -> None.
R15 add_rejects_non_dict: TypeError on add("not a dict").
"""
from __future__ import annotations

import re
import threading
import time

import pytest


def _make_runbook(fault_id: str = "F-001") -> dict:
    """Minimal valid runbook dict for store tests. Real shape is
    built by runbook_builder; the store only cares that it's a dict
    and that fault_id is present (used in some operator UIs)."""
    return {
        "fault_id": fault_id,
        "cause": {"id": "thermal_heater_stuck_on", "score": 0.9},
        "winner": {"procedure": "thermal_throttle_payload", "risk_score": 0.12},
        "verdict": {"status": "OK", "reason": "test"},
    }


def test_R10_store_mints_id_and_indexes():
    from live.runbook_store import RunbookStore
    s = RunbookStore()
    rid = s.add(_make_runbook("F-001"))
    assert re.match(r"^RB-\d{8}-\d{4}$", rid), f"bad id shape: {rid!r}"
    assert s.get(rid) is not None
    recent = s.list_recent()
    assert len(recent) == 1
    assert recent[0]["runbook_id"] == rid


def test_R11_store_caps_at_max():
    from live.runbook_store import RunbookStore
    s = RunbookStore(max_runbooks=5)
    ids = [s.add(_make_runbook(f"F-{i:03d}")) for i in range(7)]
    assert len(s) == 5
    # First two evicted; latest 5 retainable.
    assert s.get(ids[0]) is None
    assert s.get(ids[1]) is None
    for rid in ids[2:]:
        assert s.get(rid) is not None
    # list_recent returns most-recent-first.
    recent = s.list_recent()
    assert [r["runbook_id"] for r in recent] == list(reversed(ids[2:]))


def test_R12_store_thread_safe():
    """4 threads each add 5 runbooks = 20 total. No duplicate ids."""
    from live.runbook_store import RunbookStore
    s = RunbookStore(max_runbooks=100)
    def worker(start: int) -> None:
        for i in range(5):
            s.add(_make_runbook(f"F-{start + i:03d}"))
            # Tiny sleep to encourage interleaving; tests the lock.
            time.sleep(0.001)
    threads = [threading.Thread(target=worker, args=(t * 5,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s) == 20
    # All ids are unique.
    all_ids = [r["runbook_id"] for r in s.list_recent(limit=100)]
    assert len(set(all_ids)) == 20


def test_R13_list_recent_respects_limit():
    from live.runbook_store import RunbookStore
    s = RunbookStore()
    for i in range(10):
        s.add(_make_runbook(f"F-{i:03d}"))
    assert len(s.list_recent(limit=2)) == 2
    assert len(s.list_recent(limit=0)) == 0
    # limit > MAX_LIST_LIMIT is clamped, not unlimited.
    big = s.list_recent(limit=10_000)
    assert len(big) == 10  # only 10 in the store


def test_R14_get_unknown_returns_none():
    from live.runbook_store import RunbookStore
    s = RunbookStore()
    assert s.get("RB-99999999-9999") is None
    assert s.get("nonsense") is None


def test_R15_add_rejects_non_dict():
    from live.runbook_store import RunbookStore
    s = RunbookStore()
    with pytest.raises(TypeError):
        s.add("not a dict")
    with pytest.raises(TypeError):
        s.add(42)
    with pytest.raises(TypeError):
        s.add(None)
