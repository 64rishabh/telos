"""Unit tests for the Verdict mapper."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List
import pytest

from twin.verdict import Verdict, VerdictStatus, to_verdict, RISK_THRESHOLD_INCONCLUSIVE
from twin.procedures import Cause, Procedure


# Lightweight stand-in for ValidationResult: same shape, no physics.
@dataclass
class FakeValidation:
    feasible: bool = True
    violations: List[Dict[str, Any]] = field(default_factory=list)
    risk_score: float = 0.1
    predicted_trajectory: Dict[str, Any] = field(default_factory=dict)
    baseline_trajectory: Dict[str, Any] = field(default_factory=dict)
    summary: str = "fake summary"
    postcondition_check: bool = True


# ----- 1. OK path ---------------------------------------------------

def test_feasible_low_risk_returns_ok():
    v = FakeValidation(feasible=True, risk_score=0.1)
    verdict = to_verdict(v, Procedure.EPS_SHED_LOAD, Cause.EPS_LOAD_EXCESS)
    assert verdict.status == VerdictStatus.OK
    assert "OK" in verdict.reason
    assert "eps_shed_non_essential_load" in verdict.reason
    assert "0.10" in verdict.reason
    # The validation's summary is forwarded into notes for the runbook.
    assert v.summary in verdict.notes


# ----- 2. REJECT path ----------------------------------------------

def test_infeasible_returns_reject():
    v = FakeValidation(feasible=False, risk_score=0.5)
    verdict = to_verdict(v, Procedure.MODE_CHANGE_TO_SAFE, Cause.BATTERY_OVERDISCHARGE)
    assert verdict.status == VerdictStatus.REJECT
    assert "REJECTED" in verdict.reason
    assert "mode_change_to_safe" in verdict.reason


def test_constraint_violations_return_reject():
    v = FakeValidation(
        feasible=True,
        risk_score=0.5,
        violations=[{"field": "battery_soc", "value": 0.05, "limit": (0.10, 1.00)}],
    )
    verdict = to_verdict(v, Procedure.EPS_SHED_LOAD, Cause.EPS_LOAD_EXCESS)
    assert verdict.status == VerdictStatus.REJECT
    assert "battery_soc" in verdict.reason
    assert verdict.per_step_outcomes  # carries the violations


# ----- 3. INCONCLUSIVE path ----------------------------------------

def test_feasible_but_high_risk_returns_inconclusive():
    v = FakeValidation(feasible=True, risk_score=0.5)
    verdict = to_verdict(v, Procedure.EPS_SHED_LOAD, Cause.EPS_LOAD_EXCESS)
    assert verdict.status == VerdictStatus.INCONCLUSIVE
    assert "INCONCLUSIVE" in verdict.reason
    assert "0.50" in verdict.reason
    assert str(RISK_THRESHOLD_INCONCLUSIVE)[:3] in verdict.reason


def test_at_threshold_returns_inconclusive():
    """risk_score exactly at threshold -> INCONCLUSIVE (boundary)."""
    v = FakeValidation(feasible=True, risk_score=RISK_THRESHOLD_INCONCLUSIVE)
    verdict = to_verdict(v, Procedure.WAIT, Cause.NO_FAULT_DETECTED)
    assert verdict.status == VerdictStatus.INCONCLUSIVE


def test_just_below_threshold_returns_ok():
    """risk_score just below threshold -> OK."""
    v = FakeValidation(feasible=True, risk_score=RISK_THRESHOLD_INCONCLUSIVE - 0.01)
    verdict = to_verdict(v, Procedure.WAIT, Cause.NO_FAULT_DETECTED)
    assert verdict.status == VerdictStatus.OK


# ----- 4. Verdict shape contract -----------------------------------

def test_verdict_has_all_bible_fields():
    """BIBLE §2 mandates: proposal_id, status, reason,
    per_step_outcomes, twin_simulation_digest, notes. All present."""
    v = FakeValidation(feasible=True, risk_score=0.1)
    verdict = to_verdict(v, Procedure.WAIT, Cause.NO_FAULT_DETECTED)
    d = verdict.to_dict()
    assert "proposal_id" in d
    assert "status" in d
    assert "reason" in d
    assert "per_step_outcomes" in d
    assert "twin_simulation_digest" in d
    assert "notes" in d
    assert d["status"] in {"OK", "REJECT", "INCONCLUSIVE"}


def test_proposal_id_and_digest_are_empty_in_phase1():
    """Phase 3 concern; the seam is in place but the crypto is not."""
    v = FakeValidation(feasible=True, risk_score=0.1)
    verdict = to_verdict(v, Procedure.WAIT, Cause.NO_FAULT_DETECTED)
    assert verdict.proposal_id == ""
    assert verdict.twin_simulation_digest == ""


def test_to_dict_is_json_serializable():
    """The verdict must serialize cleanly for the WebSocket broadcast."""
    import json
    v = FakeValidation(feasible=True, risk_score=0.1)
    verdict = to_verdict(v, Procedure.WAIT, Cause.NO_FAULT_DETECTED)
    json.dumps(verdict.to_dict())  # must not raise
