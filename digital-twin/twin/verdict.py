"""
Stage 4 — Validate wrapper: map a ValidationResult to a BIBLE §2 Verdict.

BIBLE §2 says Validate emits:
  Verdict {proposal_id, status, reason, per_step_outcomes,
           twin_simulation_digest, notes}
  where status is OK | REJECT(reason) | INCONCLUSIVE(what_we_need_to_know)

`validate_procedure()` (in twin/validate.py) returns a ValidationResult
with feasible, risk_score, violations, predicted/baseline trajectories.
This module is the BIBLE-shaped wrapper: it consumes a ValidationResult
and produces a Verdict the operator (and, eventually, the runbook) reads.

Phase 1 leaves proposal_id and twin_simulation_digest as empty strings —
those are Phase 3 concerns (Merkle chain, content addressing). The
fields are reserved in the dataclass so the BIBLE §2 contract is honored
structurally; the integration test asserts they exist as keys.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List

from twin.procedures import Cause, Procedure
from twin.validate import ValidationResult


class VerdictStatus(str, Enum):
    """The three outcomes the operator sees."""
    OK = "OK"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class Verdict:
    """BIBLE §2 Verdict: the operator-facing outcome of Stage 4.

    proposal_id and twin_simulation_digest are reserved for Phase 3.
    Phase 1 fills them with "" so the contract is structurally
    satisfied without committing to a hashing scheme yet.
    """
    proposal_id: str
    status: VerdictStatus
    reason: str
    per_step_outcomes: List[Dict[str, Any]]
    twin_simulation_digest: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable form for the WebSocket broadcast."""
        d = asdict(self)
        d["status"] = self.status.value
        return d


# Threshold above which we don't claim OK. Tunable; operational
# policy, not a physics constant. Documented in the BIBLE §7 when
# the policy engine lands; for Phase 1 we choose 0.3 as a demo
# default that lets plausible procedures through and flags the
# marginal ones.
RISK_THRESHOLD_INCONCLUSIVE: float = 0.3


def to_verdict(
    validation: ValidationResult,
    procedure: Procedure,
    cause: Cause,
) -> Verdict:
    """Wrap a ValidationResult in the BIBLE §2 Verdict shape.

    Decision order:
      1. If !feasible OR there are constraint violations -> REJECT
         (the twin predicted the procedure cannot complete safely).
      2. If feasible but risk_score >= threshold -> INCONCLUSIVE
         (the twin says it works, but the margin is thin; operator
         review is warranted).
      3. Otherwise -> OK.

    Args:
        validation: the ValidationResult from validate_procedure()
        procedure: the procedure that was validated (for the reason string)
        cause: the cause that produced this procedure (also for the reason)

    Returns:
        Verdict with status OK | REJECT | INCONCLUSIVE and a human-readable
        reason naming the procedure, cause, and the threshold involved.
    """
    # 1. REJECT path: infeasible OR has constraint violations
    if not validation.feasible or validation.violations:
        violation_summary = (
            "; ".join(v["field"] for v in validation.violations[:3])
            if validation.violations
            else "preconditions failed"
        )
        return Verdict(
            proposal_id="",
            status=VerdictStatus.REJECT,
            reason=(
                f"{procedure.value} REJECTED for {cause.value}: {violation_summary}"
            ),
            per_step_outcomes=[v for v in validation.violations[:5]],
            twin_simulation_digest="",
            notes=[validation.summary],
        )

    # 2. INCONCLUSIVE path: feasible but risk score is high
    if validation.risk_score >= RISK_THRESHOLD_INCONCLUSIVE:
        return Verdict(
            proposal_id="",
            status=VerdictStatus.INCONCLUSIVE,
            reason=(
                f"{procedure.value} INCONCLUSIVE for {cause.value}: "
                f"risk_score={validation.risk_score:.2f} >= "
                f"threshold={RISK_THRESHOLD_INCONCLUSIVE:.2f}; "
                f"operator review recommended"
            ),
            per_step_outcomes=[],
            twin_simulation_digest="",
            notes=[validation.summary],
        )

    # 3. OK path
    return Verdict(
        proposal_id="",
        status=VerdictStatus.OK,
        reason=(
            f"{procedure.value} OK for {cause.value}: "
            f"risk={validation.risk_score:.2f} < "
            f"threshold={RISK_THRESHOLD_INCONCLUSIVE:.2f}"
        ),
        per_step_outcomes=[],
        twin_simulation_digest="",
        notes=[validation.summary],
    )
