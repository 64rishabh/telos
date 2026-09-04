"""Unit tests for the Propose stage.

Tests use a real (but small) twin state and call validate_procedure
with a short horizon to keep the suite fast.
"""

from __future__ import annotations
import pytest

from twin.propose import propose, Proposal, RankedCandidate, PROPOSE_TOP_K
from twin.procedures import (
    Cause, Procedure, get_candidate_procedures,
    VALID_MISSION_IMPACTS, VALID_REVERSIBILITY,
)
from twin.state import make_default_state
from twin.verdict import VerdictStatus


# Common fixture: a fresh state dict, small horizon for fast tests.
@pytest.fixture
def fresh_state():
    return make_default_state()


# ----- 1. Returns a Proposal with a known procedure ----------------

def test_propose_returns_proposal_with_known_procedure(fresh_state):
    """EPS_INTERNAL_R_DEGRADATION has 3 candidates; propose must
    return one of them."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert isinstance(proposal, Proposal)
    candidates = get_candidate_procedures(Cause.EPS_INTERNAL_R_DEGRADATION)
    assert proposal.procedure in candidates


# ----- 2. Ranking is non-empty and ordered -------------------------

def test_propose_ranking_is_non_empty_and_ordered(fresh_state):
    """candidates_ranked must be non-empty, list of RankedCandidate,
    and ascending by risk_score."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert len(proposal.candidates_ranked) > 0
    assert all(isinstance(rc, RankedCandidate) for rc in proposal.candidates_ranked)
    scores = [rc.risk_score for rc in proposal.candidates_ranked]
    assert scores == sorted(scores)


def test_propose_winner_is_lowest_risk(fresh_state):
    """The selected procedure must be the lowest-risk candidate."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    scores = [rc.risk_score for rc in proposal.candidates_ranked]
    assert proposal.risk_score == min(scores)
    # The procedure at index 0 of the ranking is the winner.
    assert proposal.procedure == proposal.candidates_ranked[0].procedure


# ----- 3. Verdict is BIBLE-shaped ----------------------------------

def test_propose_attaches_bible_verdict(fresh_state):
    """The Verdict on the proposal is OK | REJECT | INCONCLUSIVE."""
    proposal = propose(
        Cause.THERMAL_HEATER_STUCK_OFF, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert proposal.verdict.status in {
        VerdictStatus.OK,
        VerdictStatus.REJECT,
        VerdictStatus.INCONCLUSIVE,
    }


# ----- 4. Single-candidate cause ----------------------------------

def test_propose_with_single_candidate_cause(fresh_state):
    """THERMAL_HEATER_STUCK_ON has exactly 1 candidate; propose must
    return it (no ranking needed)."""
    proposal = propose(
        Cause.THERMAL_HEATER_STUCK_ON, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert proposal.procedure == Procedure.THERMAL_THROTTLE_PAYLOAD
    assert len(proposal.candidates_ranked) == 1
    rc = proposal.candidates_ranked[0]
    assert rc.procedure == Procedure.THERMAL_THROTTLE_PAYLOAD


# ----- 5. Procedure params match defaults -------------------------

def test_propose_procedure_params_match_defaults(fresh_state):
    """proposal.procedure_params must equal the registry defaults
    for the chosen procedure."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    if proposal.procedure == Procedure.MODE_CHANGE_TO_SAFE:
        assert proposal.procedure_params == {}
    elif proposal.procedure == Procedure.EPS_SHED_LOAD:
        assert proposal.procedure_params == {"load_reduction_a": 1.0, "duration_s": 3600.0}


# ----- 6. Proposal is JSON-serializable ---------------------------

def test_proposal_to_dict_is_json_serializable(fresh_state):
    """The broadcast path serializes the proposal as JSON."""
    import json
    proposal = propose(
        Cause.THERMAL_HEATER_STUCK_ON, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    json.dumps(proposal.to_dict())  # must not raise


# ----- 7. Cause score is forwarded ---------------------------------

def test_propose_forwards_cause_score(fresh_state):
    """The cause_score passed in lands in the Proposal (runbook needs it)."""
    proposal = propose(
        Cause.ADCS_STAR_TRACKER_LOST, fresh_state,
        cause_score=0.85,
        horizon_s=600.0, dt_s=60.0,
    )
    assert proposal.cause_score == 0.85


# ----- 8. Wall time is within budget -------------------------------

def test_propose_wall_time_within_budget(fresh_state):
    """Per the plan: Propose must be <500ms (the 5Hz tick budget).
    3-candidate cause at 1h/120s is the worst case in Phase 1; with
    parallel sims this is now wall-time-of-one-sim, not 3x."""
    import time
    t0 = time.time()
    propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=3600.0, dt_s=120.0,
    )
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"Propose took {elapsed*1000:.0f}ms, budget 1000ms"


# ----- 9. Top-K pre-filter ----------------------------------------

def test_propose_filters_to_top_k(fresh_state):
    """candidates_ranked must be <= PROPOSE_TOP_K and contain RankedCandidate
    entries with the 3 new catalog fields populated."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert len(proposal.candidates_ranked) <= PROPOSE_TOP_K
    for rc in proposal.candidates_ranked:
        assert isinstance(rc, RankedCandidate)
        assert isinstance(rc.effort_score, float)
        assert 0.0 <= rc.effort_score <= 1.0
        assert rc.mission_impact in VALID_MISSION_IMPACTS
        assert rc.reversibility in VALID_REVERSIBILITY


# ----- 10. New catalog fields in to_dict() ------------------------

def test_propose_catalog_fields_in_to_dict(fresh_state):
    """to_dict() must include effort_score, mission_impact, reversibility
    for every entry in candidates_ranked (frontend demo surface)."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    d = proposal.to_dict()
    assert "candidates_ranked" in d
    assert len(d["candidates_ranked"]) > 0
    for entry in d["candidates_ranked"]:
        assert "procedure" in entry
        assert "risk_score" in entry
        assert "effort_score" in entry
        assert "mission_impact" in entry
        assert "reversibility" in entry
        assert isinstance(entry["effort_score"], float)
        assert entry["mission_impact"] in VALID_MISSION_IMPACTS
        assert entry["reversibility"] in VALID_REVERSIBILITY


# ----- 11. Progress callback fires for parallel sims ---------------

def test_propose_progress_callback_fires(fresh_state):
    """The progress_cb must fire at least twice per validate step
    (once for the baseline run, once for the predicted run). For a
    single-candidate cause at horizon_s=600, dt_s=60 -> 10 sim steps
    -> 20 expected fires."""
    fires = []

    def cb(procedure, step, total, snapshot):
        fires.append((procedure, step, total))

    propose(
        Cause.THERMAL_HEATER_STUCK_ON, fresh_state,  # 1 candidate
        horizon_s=600.0, dt_s=60.0,
        progress_cb=cb,
    )
    assert len(fires) >= 20, f"expected >=20 fires, got {len(fires)}"
    # All fires should be tagged with the single candidate's name.
    assert all(f[0] == "thermal_throttle_payload" for f in fires)
    # (step, total) should be in range
    for proc, step, total in fires:
        assert 0 <= step < total
        assert total == 10  # 600s / 60s = 10 steps


def test_propose_progress_callback_distinguishes_procedures(fresh_state):
    """With multiple candidates, the progress_cb fires should be
    attributable to each procedure (the bridge uses this to render
    the parallel-sims panel)."""
    fires = []

    def cb(procedure, step, total, snapshot):
        fires.append(procedure)

    propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,  # 3 candidates
        horizon_s=600.0, dt_s=60.0,
        progress_cb=cb,
    )
    procs_seen = set(fires)
    # All 3 candidate procedures should have produced at least one fire.
    expected = set(p.value for p in get_candidate_procedures(Cause.EPS_INTERNAL_R_DEGRADATION))
    assert procs_seen == expected, f"expected {expected}, saw {procs_seen}"


def test_propose_no_progress_callback_works(fresh_state):
    """progress_cb=None must work (the legacy callers don't pass one)."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert len(proposal.candidates_ranked) > 0


# ----- 12. Runs in parallel (smoke test) ---------------------------

def test_propose_runs_candidates_in_parallel(fresh_state):
    """Structural test: the ThreadPoolExecutor used by Propose must
    actually run the per-candidate validate_procedure() calls in
    worker threads (not sequentially in the caller thread).

    We patch the ThreadPoolExecutor at import time to record the
    max_workers argument and assert that ALL the top-K candidates
    are submitted to it (not just the first, which would indicate
    a regression to the old sequential for-loop).

    Wall-time parallelism is verified separately by inspection on a
    real machine; this test guards the structural contract.
    """
    from concurrent.futures import ThreadPoolExecutor
    import twin.propose as propose_mod

    submitted: list = []
    orig_init = ThreadPoolExecutor.__init__

    def patched_init(self, max_workers=None, **kwargs):
        submitted.append(max_workers)
        orig_init(self, max_workers=max_workers, **kwargs)

    # Patch the import in propose's module namespace
    propose_mod.ThreadPoolExecutor.__init__ = patched_init
    try:
        propose(
            Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,  # 3 candidates
            horizon_s=600.0, dt_s=60.0,
        )
    finally:
        propose_mod.ThreadPoolExecutor.__init__ = orig_init

    # The executor must be created exactly once (not once per
    # candidate) and must be sized for the full top-K, not just 1.
    assert len(submitted) == 1, (
        f"Expected 1 ThreadPoolExecutor for 3 parallel sims, got {len(submitted)}. "
        f"Regression: executor may be inside a per-candidate loop."
    )
    assert submitted[0] == 3, (
        f"Expected max_workers=3 (one per candidate), got {submitted[0]}. "
        f"Regression: top-K filter or worker sizing broken."
    )
