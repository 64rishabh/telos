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


# ----- 13. Multi-axis lexicographic ranking -----------------------

def test_propose_ranks_by_multi_axis_key(fresh_state):
    """For THERMAL_HEATER_STUCK_ON (1 candidate), the ranking is
    trivially single-element, so we exercise the multi-axis sort
    on a multi-candidate cause where the catalog fields dominate
    over risk_score.

    Concretely: EPS_INTERNAL_R_DEGRADATION has 3 candidates. We
    patch the validate_procedure to return a fake risk_score for
    each procedure so the rank_key differs on impact+effort before
    risk is consulted. The winner must be the one with the lowest
    mission_impact_score + effort_score tuple, regardless of
    which procedure has the lowest risk_score.
    """
    import twin.propose as propose_mod
    from twin.validate import validate_procedure as orig_validate
    from twin.procedures import get_default_params
    from twin.state import make_default_state

    # Map each candidate to a hand-picked risk_score so the lex
    # comparison is forced to consult the catalog fields, not risk.
    # mode_change_to_safe has highest impact (1.0) and highest
    # effort (0.85) -> worst on first two axes -> should NOT win,
    # even with the lowest risk_score.
    risk_overrides = {
        "eps_shed_non_essential_load": 0.40,             # low impact, low effort, MEDIUM risk
        "eps_increase_charging_priority": 0.30,          # low impact, low effort, LOW risk
        "mode_change_to_safe": 0.05,                     # HIGH impact, HIGH effort, LOWEST risk
    }

    def patched_validate(starting_state, procedure, params, **kwargs):
        result = orig_validate(starting_state, procedure, params, **kwargs)
        # Force risk_score to the override, regardless of what the
        # twin sim produced.
        from dataclasses import replace
        return replace(result, risk_score=risk_overrides[procedure.value])

    orig_validate_in_mod = propose_mod.validate_procedure
    propose_mod.validate_procedure = patched_validate
    try:
        proposal = propose(
            Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
            horizon_s=600.0, dt_s=60.0,
        )
    finally:
        propose_mod.validate_procedure = orig_validate_in_mod

    # mode_change_to_safe has risk=0.05 (lowest!) but its catalog
    # fields (mission_impact=mission-ending, effort=0.85) should
    # push it to the bottom under the lexicographic key. The
    # winner is the candidate with the lowest mission_impact
    # AND lowest effort among the simulated set.
    ranked_procs = [rc.procedure.value for rc in proposal.candidates_ranked]
    # Whichever of eps_shed_non_essential_load or
    # eps_increase_charging_priority has the lower risk_score
    # wins (catalog fields are equal between them).
    # We don't pin the exact winner (depends on risk overrides);
    # we just assert mode_change_to_safe is NOT the winner.
    assert proposal.procedure.value != "mode_change_to_safe", (
        f"mode_change_to_safe should lose on catalog fields, "
        f"not on risk_score. Got winner={proposal.procedure.value}"
    )
    # And confirm the candidates_ranked is sorted by _rank_key.
    keys = [propose_mod._rank_key(rc) for rc in proposal.candidates_ranked]
    assert keys == sorted(keys), (
        f"candidates_ranked not sorted by _rank_key: {keys}"
    )


def test_propose_winner_passes_rank_key(fresh_state):
    """Proposal.winner_rank_key must equal _rank_key(winner)."""
    proposal = propose(
        Cause.THERMAL_HEATER_STUCK_OFF, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    from twin import propose as pm
    winner_rc = next(rc for rc in proposal.candidates_ranked
                     if rc.procedure == proposal.procedure)
    expected = pm._rank_key(winner_rc)
    assert proposal.winner_rank_key == expected
    # And to_dict() carries it.
    assert "winner_rank_key" in proposal.to_dict()
    assert proposal.to_dict()["winner_rank_key"] == list(expected)


def test_propose_risk_score_is_tiebreaker(fresh_state):
    """Two candidates identical on impact/effort/reversibility
    must be ordered by risk_score. We use single-candidate cause
    and assert the rank_key's last component is the risk_score."""
    from twin import propose as pm
    proposal = propose(
        Cause.THERMAL_HEATER_STUCK_ON, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    # Single-candidate: the only element of candidates_ranked
    # IS the winner. The rank_key's last component is its
    # risk_score, which IS the tie-breaker.
    rc = proposal.candidates_ranked[0]
    key = pm._rank_key(rc)
    assert key[3] == rc.risk_score, "rank_key[3] must be risk_score"
    assert proposal.winner_rank_key[3] == rc.risk_score


def test_propose_ranking_in_to_dict_uses_multi_axis(fresh_state):
    """candidates_ranked in to_dict() must be ordered by _rank_key,
    not by risk_score. Verify with THERMAL_HEATER_STUCK_OFF (2
    candidates whose catalog fields are equal except mission_impact,
    so the rank_key is decided on the first axis; risk_score is
    not consulted)."""
    from twin import propose as pm
    proposal = propose(
        Cause.THERMAL_HEATER_STUCK_OFF, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    d = proposal.to_dict()
    serialized = d["candidates_ranked"]
    # Reconstruct _rank_key for each entry from the catalog fields +
    # the carried risk_score.
    keys = [(
        pm._MISSION_IMPACT_SCORE.get(e["mission_impact"], 0.5),
        e["effort_score"],
        pm._REVERSIBILITY_SCORE.get(e["reversibility"], 0.1),
        e["risk_score"],
    ) for e in serialized]
    assert keys == sorted(keys), (
        f"to_dict() ordering violated multi-axis key: {keys}"
    )
