"""
Stage 3 — Propose.

Given a Cause and the current twin state, look up the candidate
procedures, validate each against the twin, rank by risk_score, and
return the best one with the full ranking for the runbook.

The cause -> candidate mapping is hardcoded in twin/procedures.py
(BIBLE D-10: catalog and twin are one file). Propose adds the
ranking on top: each candidate is fed to validate_procedure() with
the procedure's default_params, and the one with the lowest
risk_score wins.

Per the plan (T1), we use validation-based ranking because it's
free (90ms for 3 candidates measured) and matches the BIBLE §2
contract verbatim.

Per the plan (T2), default_params live per-procedure in
PROCEDURE_REGISTRY.default_params. Propose reads them via
get_default_params(). Per-cause overrides are deferred (T2).

Per the plan (T3), we wrap the ValidationResult in a Verdict here
so the operator sees OK | REJECT | INCONCLUSIVE. Propose is the
ONLY place that calls to_verdict; the Verdict flows out through
the WebSocket broadcast unchanged.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from twin.procedures import (
    Cause, Procedure, get_candidate_procedures, get_default_params,
)
from twin.validate import ValidationResult, validate_procedure
from twin.verdict import Verdict, to_verdict


@dataclass
class Proposal:
    """The output of Stage 3, the input to Stage 4 (and, in Phase 2,
    Stage 5 Approve).

    For Phase 1 the Proposal carries enough for the runbook and the
    operator dashboard. Phase 3 will add Merkle-chain linking
    (proposal_id -> verdict -> step receipts) but does not change
    this shape.
    """
    cause: Cause
    cause_score: float
    procedure: Procedure
    procedure_params: Dict[str, Any]
    risk_score: float
    validation: ValidationResult
    verdict: Verdict
    candidates_ranked: List[Tuple[Procedure, float, ValidationResult]] = field(
        default_factory=list
    )

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable form for the WebSocket broadcast."""
        return {
            "cause": self.cause.value,
            "cause_score": self.cause_score,
            "procedure": self.procedure.value,
            "procedure_params": self.procedure_params,
            "risk_score": self.risk_score,
            "verdict": self.verdict.to_dict(),
            "candidates_ranked": [
                {"procedure": p.value, "risk_score": s}
                for (p, s, _) in self.candidates_ranked
            ],
        }


def propose(
    cause: Cause,
    starting_state: Dict[str, Any],
    cause_score: float = 1.0,
    horizon_s: float = 3600.0,
    dt_s: float = 120.0,
) -> Proposal:
    """Pick the best procedure for `cause` by twin-validated risk.

    Args:
        cause: the diagnosed cause (from Stage 2 Diagnose)
        starting_state: the current twin state (the post-fault state
            in the live pipeline, the pre-procedure state in the
            offline demo)
        cause_score: how strongly Diagnose believes the cause
            (forwarded into the Proposal for the runbook)
        horizon_s: how far to project when validating (default 1h
            for the demo loop; 4h in the integration test)
        dt_s: timestep for the projection (default 2 min for demo,
            1 min in the integration test)

    Returns:
        Proposal with the winning procedure, the full ranking, and
        the BIBLE-shaped Verdict. Wall time measured: 30-120ms
        depending on candidate count and horizon.
    """
    candidates = get_candidate_procedures(cause)

    # Edge case: cause has no candidate procedures (shouldn't happen
    # for any of the 13 Cause values, but defensive). Return a WAIT
    # proposal as the safe default so the pipeline never blocks.
    if not candidates:
        wait_params = get_default_params(Procedure.WAIT)
        wait_validation = validate_procedure(
            starting_state, Procedure.WAIT, wait_params,
            horizon_s=horizon_s, dt_s=dt_s,
        )
        wait_verdict = to_verdict(wait_validation, Procedure.WAIT, cause)
        return Proposal(
            cause=cause,
            cause_score=cause_score,
            procedure=Procedure.WAIT,
            procedure_params=wait_params,
            risk_score=wait_validation.risk_score,
            validation=wait_validation,
            verdict=wait_verdict,
            candidates_ranked=[(Procedure.WAIT, wait_validation.risk_score, wait_validation)],
        )

    # Validate every candidate and rank by risk_score (lower is better).
    validated: List[Tuple[Procedure, float, ValidationResult]] = []
    for proc in candidates:
        params = get_default_params(proc)
        # validate_procedure() raises ValueError on bad params; the
        # defaults always validate by construction (see
        # test_procedures_defaults.py), so this is belt-and-braces.
        result = validate_procedure(
            starting_state, proc, params,
            horizon_s=horizon_s, dt_s=dt_s,
        )
        validated.append((proc, result.risk_score, result))

    # Stable sort: equal-risk candidates keep their catalog order,
    # which is the expert-defined priority from get_candidate_procedures.
    validated.sort(key=lambda x: x[1])

    best_proc, best_risk, best_validation = validated[0]
    best_verdict = to_verdict(best_validation, best_proc, cause)

    return Proposal(
        cause=cause,
        cause_score=cause_score,
        procedure=best_proc,
        procedure_params=get_default_params(best_proc),
        risk_score=best_risk,
        validation=best_validation,
        verdict=best_verdict,
        candidates_ranked=validated,
    )
