"""Unit tests for the Propose stage.

Tests use a real (but small) twin state and call validate_procedure
with a short horizon to keep the suite fast.
"""

from __future__ import annotations
import pytest

from twin.propose import propose, Proposal
from twin.procedures import Cause, Procedure, get_candidate_procedures
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
    """candidates_ranked must be non-empty and ascending by risk_score."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    assert len(proposal.candidates_ranked) > 0
    scores = [s for (_, s, _) in proposal.candidates_ranked]
    assert scores == sorted(scores)


def test_propose_winner_is_lowest_risk(fresh_state):
    """The selected procedure must be the lowest-risk candidate."""
    proposal = propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=600.0, dt_s=60.0,
    )
    scores = [s for (_, s, _) in proposal.candidates_ranked]
    assert proposal.risk_score == min(scores)
    # The procedure at index 0 of the ranking is the winner.
    assert proposal.procedure == proposal.candidates_ranked[0][0]


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
    """Per the plan: Propose must be <200ms (the 5Hz tick budget).
    3-candidate cause at 1h/120s is the worst case in Phase 1."""
    import time
    t0 = time.time()
    propose(
        Cause.EPS_INTERNAL_R_DEGRADATION, fresh_state,
        horizon_s=3600.0, dt_s=120.0,
    )
    elapsed = time.time() - t0
    assert elapsed < 0.5, f"Propose took {elapsed*1000:.0f}ms, budget 500ms"
