"""Thread-safe in-memory runbook store.

This is the persistence layer the live operator dashboard reads from.
When a fault is injected and the Diagnose -> Propose -> Validate
pipeline produces a runbook, the runbook is pushed here and the
operator's frontend can fetch it by id across page refreshes.

The store is intentionally in-process (per BIBLE: Phase 1 has no
external DB). It caps at MAX_RUNBOOKS runbooks; older ones are
evicted FIFO. A future Phase 2+ layer will mirror writes to a
durable store (Postgres / Merkle chain per BIBLE §8).

Concurrency:
  - Multiple worker threads (the ThreadPoolExecutor in propose.py)
    can call add() concurrently when parallel sims push partial
    evidence; a single threading.Lock guards the dict + counter.
  - HTTP handlers in ws_server.py read from list_recent()/get()
    which take the same lock briefly to snapshot the order.
  - The asyncio producer loop does not touch the store directly;
    ws_server.py drains the runbook queue off-thread.

Id format:
  - RB-YYYYMMDD-NNNN where NNNN is a per-process counter starting
    at 1 each calendar day (UTC). The counter resets at midnight
    UTC; collisions across restarts are not avoided (acceptable
    for in-memory store; Phase 3 will add content addressing).
"""
from __future__ import annotations

import datetime as _dt
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional


# Maximum runbooks retained in memory. Older ones are evicted FIFO.
# The cap is small because runbooks carry twin trajectory arrays
# (31 points x 3 fields x 2 series = ~2 KB each) and the dashboard
# only renders the most recent few. Phase 2 will move the cap to
# the DB layer.
MAX_RUNBOOKS: int = 50

# Cap on list_recent() (operator UI: dashboard, runbook list).
DEFAULT_LIST_LIMIT: int = 20

# Cap on a single list_recent() call (defensive: prevent the UI
# from asking for everything in one go and OOM-ing the server).
MAX_LIST_LIMIT: int = 200


class RunbookStore:
    """Thread-safe FIFO store for runbook dicts keyed by runbook_id.

    The dict is an OrderedDict so list_recent() returns the entries
    in insertion order (most recent last). We reverse on the way
    out so callers see "most recent first" without a second sort.
    """

    def __init__(self, max_runbooks: int = MAX_RUNBOOKS) -> None:
        self._max = int(max_runbooks)
        self._lock = threading.Lock()
        self._by_id: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        # Per-calendar-day counter so the id's NNNN segment resets at
        # midnight UTC. _current_day holds the date the counter was
        # last reset to 0; on add() we compare and reset if needed.
        self._current_day: str = ""
        self._counter: int = 0

    # ----- id minting --------------------------------------------------

    def _mint_id(self) -> str:
        """Mint a new runbook id of the form RB-YYYYMMDD-NNNN.

        Must be called with self._lock held.
        """
        today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
        if today != self._current_day:
            self._current_day = today
            self._counter = 0
        self._counter += 1
        return f"RB-{today}-{self._counter:04d}"

    # ----- write path --------------------------------------------------

    def add(self, runbook: Dict[str, Any]) -> str:
        """Add a runbook, return its minted runbook_id.

        If the store is at capacity, the oldest entry (lowest id
        within the current day, or the earliest entry across days)
        is evicted. The evicted id is returned nowhere — the caller
        is expected to only ever care about the id it just minted.
        """
        if not isinstance(runbook, dict):
            raise TypeError(f"runbook must be a dict, got {type(runbook).__name__}")
        with self._lock:
            runbook_id = self._mint_id()
            runbook["runbook_id"] = runbook_id
            self._by_id[runbook_id] = runbook
            while len(self._by_id) > self._max:
                self._by_id.popitem(last=False)
            return runbook_id

    # ----- read path ---------------------------------------------------

    def get(self, runbook_id: str) -> Optional[Dict[str, Any]]:
        """Return the runbook with this id, or None if not found."""
        with self._lock:
            return self._by_id.get(runbook_id)

    def list_recent(self, limit: int = DEFAULT_LIST_LIMIT) -> List[Dict[str, Any]]:
        """Return up to `limit` most-recently-added runbooks (newest first).

        Each entry is the full payload. The list is a shallow copy;
        callers must not mutate the dicts in place (the store holds
        references). For the dashboard's list view, callers should
        project a summary shape (id, fault_id, cause, status, ts)
        before sending to the client.
        """
        if limit <= 0:
            return []
        limit = min(limit, MAX_LIST_LIMIT)
        with self._lock:
            # OrderedDict preserves insertion order; reverse for
            # newest-first. [::-1] on a list view is O(n).
            ids = list(self._by_id.keys())[::-1]
            out: List[Dict[str, Any]] = []
            for rid in ids[:limit]:
                out.append(self._by_id[rid])
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)
