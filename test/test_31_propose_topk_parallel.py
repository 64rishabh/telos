"""D-16/D-17/D-18: top-5 pre-filter, parallel sims, catalog fields.

U16 catalog fields: every ProcedureSpec has effort_score in [0,1],
    mission_impact in VALID_MISSION_IMPACTS, reversibility in
    VALID_REVERSIBILITY. WAIT = cheapest/most reversible extreme,
    MODE_CHANGE_TO_SAFE = most expensive extreme.
U17 top-5 table: propose() returns <= PROPOSE_TOP_K (5) RankedCandidates,
    each carrying procedure + risk_score + effort_score +
    mission_impact + reversibility + approval/risk_class cross-check
    against the registry. Winner = index 0 = lowest risk_score.
U18 pre-filter trims: with 9 forced candidates, only 5 reach Validate
    (counted by wrapping validate_procedure). The 5 are the cheapest
    by _coarse_rank_key (effort, impact, reversibility lexicographic).
U19 parallel executor: ThreadPoolExecutor created once with
    max_workers=min(5, len(top_k)); progress events arrive from
    distinct procedures (attribution works across the 5 sims).
U20 progress_cb None still works (legacy callers: offline demos).
"""
from __future__ import annotations

import threading

import pytest

H, DT = 600.0, 120.0


def test_U16_every_procedure_has_valid_catalog_fields():
    from twin.procedures import (PROCEDURE_REGISTRY, VALID_MISSION_IMPACTS,
                                 VALID_REVERSIBILITY)
    assert len(PROCEDURE_REGISTRY) == 9
    for proc, spec in PROCEDURE_REGISTRY.items():
        assert 0.0 <= spec.effort_score <= 1.0, f"{proc} effort out of range"
        assert spec.mission_impact in VALID_MISSION_IMPACTS, f"{proc} bad impact"
        assert spec.reversibility in VALID_REVERSIBILITY, f"{proc} bad reversibility"
        # Pre-existing fields the frontend also reads per row:
        assert spec.risk_class in {"low", "medium", "high", "critical"}
        assert spec.approval_required in {"auto", "operator", "director"}


def test_U16_extremes_wait_vs_safe():
    from twin.procedures import PROCEDURE_REGISTRY, Procedure
    wait = PROCEDURE_REGISTRY[Procedure.WAIT]
    safe = PROCEDURE_REGISTRY[Procedure.MODE_CHANGE_TO_SAFE]
    assert wait.effort_score < safe.effort_score
    assert wait.reversibility == "trivial"
    assert safe.approval_required == "director"


def test_U17_top5_table_shape_and_winner(monkeypatch):
    """Force 9 candidates so the pre-filter bites; assert top-5 table.
    Multi-axis ranking (D-19): candidates_ranked is ordered by
    _rank_key (mission_impact, effort_score, reversibility, risk_score)
    ascending, NOT by risk_score alone.
    """
    from twin import propose as pm
    from twin.procedures import (PROCEDURE_REGISTRY, Procedure,
                                 get_candidate_procedures, Cause)
    from twin.state import make_default_state
    # Pick a cause, then widen to all 9 procedures to force trimming.
    monkeypatch.setattr(pm, "get_candidate_procedures",
                        lambda _c: list(Procedure))
    p = pm.propose(Cause.THERMAL_HEATER_STUCK_ON, make_default_state(),
                   horizon_s=H, dt_s=DT)
    assert len(p.candidates_ranked) == pm.PROPOSE_TOP_K == 5
    for rc in p.candidates_ranked:
        spec = PROCEDURE_REGISTRY[rc.procedure]
        assert rc.effort_score == spec.effort_score
        assert rc.mission_impact == spec.mission_impact
        assert rc.reversibility == spec.reversibility
        assert 0.0 <= rc.risk_score <= 1.0
    # Multi-axis ordering: candidates_ranked must be sorted by
    # _rank_key ascending, not by risk_score alone.
    keys = [pm._rank_key(rc) for rc in p.candidates_ranked]
    assert keys == sorted(keys), \
        "candidates_ranked must be ordered by multi-axis _rank_key"
    # Winner is index 0; its rank_key matches proposal.winner_rank_key.
    assert p.procedure == p.candidates_ranked[0].procedure
    assert p.winner_rank_key == pm._rank_key(p.candidates_ranked[0])


def test_U18_prefilter_keeps_cheapest_five(monkeypatch):
    from twin import propose as pm
    from twin.procedures import Cause, Procedure
    from twin.state import make_default_state
    monkeypatch.setattr(pm, "get_candidate_procedures",
                        lambda _c: list(Procedure))
    seen = []
    orig_run_one = pm._run_one
    def spy(proc, *a, **k):
        seen.append(proc)
        return orig_run_one(proc, *a, **k)
    monkeypatch.setattr(pm, "_run_one", spy)
    pm.propose(Cause.THERMAL_HEATER_STUCK_ON, make_default_state(),
               horizon_s=H, dt_s=DT)
    assert len(seen) == 5, f"expected 5 sims, got {len(seen)}"
    expected = sorted(list(Procedure), key=pm._coarse_rank_key)[:5]
    # NOTE: worker execution order is nondeterministic; compare as sets.
    assert set(seen) == set(expected), "pre-filter must keep cheapest 5 by coarse key"


def test_U19_executor_workers_and_attribution(monkeypatch):
    import concurrent.futures as cf
    from twin import propose as pm
    from twin.procedures import Cause
    from twin.state import make_default_state
    calls = []
    RealPool = cf.ThreadPoolExecutor
    class SpyPool(RealPool):
        def __init__(self, max_workers=None, *a, **k):
            calls.append(max_workers)
            super().__init__(max_workers=max_workers, *a, **k)
    monkeypatch.setattr(pm, "ThreadPoolExecutor", SpyPool)
    events, tids = [], set()
    def cb(proc_value, step, total, snapshot):
        events.append((proc_value, step, total))
        tids.add(threading.get_ident())
    p = pm.propose(Cause.THERMAL_HEATER_STUCK_ON, make_default_state(),
                   horizon_s=H, dt_s=DT, progress_cb=cb)
    n = len(p.candidates_ranked)
    assert calls == [min(pm.PROPOSE_TOP_K, n)], f"executor calls: {calls}"
    procs = {e[0] for e in events}
    assert procs == {rc.procedure.value for rc in p.candidates_ranked}, \
        "every simulated procedure must be attributable in progress events"
    assert len(events) == 2 * int(H / DT) * n, \
        f"expected 2 runs x steps x {n} sims, got {len(events)}"


def test_U20_no_progress_callback_still_works():
    from twin.procedures import Cause
    from twin.propose import propose
    from twin.state import make_default_state
    p = propose(Cause.THERMAL_HEATER_STUCK_ON, make_default_state(),
                horizon_s=H, dt_s=DT, progress_cb=None)
    assert len(p.candidates_ranked) >= 1
    assert p.verdict.status.value in {"OK", "REJECT", "INCONCLUSIVE"}
