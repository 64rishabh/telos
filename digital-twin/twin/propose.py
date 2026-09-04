"""
Stage 3 — Propose.

Given a Cause and the current twin state, look up the candidate
procedures, pre-filter by catalog-level signals (cheap, no twin), then
simulate the top-K in parallel in the digital twin. Rank the simulated
candidates by risk_score and return the best one with the full ranking
for the runbook.

The cause -> candidate mapping is hardcoded in twin/procedures.py
(BIBLE D-10: catalog and twin are one file). Propose adds the ranking
on top: each candidate is fed to validate_procedure() with the
procedure's default_params, and the one with the lowest risk_score wins.

Catalog-level signals carried in the Proposal (per D-12 "Future
evolution" + the new effort/impact/reversibility fields):
  - effort_score: 0..1, lower = less operator/spacecraft work
  - mission_impact: how much mission capability is lost
  - reversibility: how easy it is to undo the procedure's effect
These are STATIC catalog values, hand-authored per procedure, and
distinct from the runtime risk_score (which is what the twin
computed). The frontend uses them to label the candidates table
without re-reading the registry.

Validation-based ranking (D-12): the twin computes the risk for each
candidate. Propose is a thin ranking layer over Validate — it does
NOT look up by default_procedure_id or risk_class.

Parallel simulation: when the candidate set has more than one
procedure, Propose runs validate_procedure() in a ThreadPoolExecutor
with up to PROPOSE_TOP_K workers. Each worker fires a per-step
on_step callback (see validate.py) so the bridge/WS server can stream
simulation progress to the frontend.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from twin.procedures import (
    Cause, Procedure, PROCEDURE_REGISTRY, get_candidate_procedures, get_default_params,
)
from twin.validate import ValidationResult, validate_procedure
from twin.verdict import Verdict, to_verdict


# Maximum number of candidate procedures we will simulate in the
# digital twin. Catalog signals (coarse_rank) pre-filter to this
# before the expensive twin sim runs. Phase 1's largest cause has
# 3 candidates, so this is just an upper bound; the larger catalog
# of future phases will exercise the pre-filter for real.
PROPOSE_TOP_K: int = 5


# Lexicographic pre-filter scoring. Lower is better. These three
# values are summed (not multiplied) so each axis still has weight
# even when others are zero. The exact weights are catalog-level
# editorial judgments; see D-12 "Future evolution" for the rationale.
_MISSION_IMPACT_SCORE: Dict[str, float] = {
    "none": 0.0,
    "minor": 0.2,
    "major": 0.5,
    "mission-ending": 1.0,
}
_REVERSIBILITY_SCORE: Dict[str, float] = {
    "trivial": 0.0,
    "easy": 0.1,
    "hard": 0.4,
}


def _coarse_rank_key(proc: Procedure) -> tuple:
    """Cheap catalog-level rank key for a procedure. Lower is better.

    Used to pre-filter candidates to PROPOSE_TOP_K before paying the
    twin simulation cost. Mirrors _rank_key but does not include the
    runtime risk_score (which only exists after the twin sim runs).
    """
    spec = PROCEDURE_REGISTRY[proc]
    return (
        _MISSION_IMPACT_SCORE.get(spec.mission_impact, 0.5),
        spec.effort_score,
        _REVERSIBILITY_SCORE.get(spec.reversibility, 0.1),
    )


def _rank_key(rc: "RankedCandidate") -> tuple:
    """Multi-axis lexicographic rank key for a simulated candidate.

    Lower is better. Order:
      1. mission_impact_score  (lowest first: "none" < "minor" < "major" < "mission-ending")
      2. effort_score          (lowest first: cheapest operator/spacecraft work)
      3. reversibility_score   (lowest first: "trivial" < "easy" < "hard")
      4. risk_score            (lowest first: runtime twin-computed risk; tie-breaker)

    Risk is consulted ONLY when the first three axes are tied. The
    lexicographic structure means a procedure that is "cheap" but
    "high risk" loses to a procedure that is "expensive" but
    "low impact" — exactly the BIBLE §2 / D-12 evolution.
    """
    return (
        _MISSION_IMPACT_SCORE.get(rc.mission_impact, 0.5),
        rc.effort_score,
        _REVERSIBILITY_SCORE.get(rc.reversibility, 0.1),
        rc.risk_score,
    )


@dataclass
class RankedCandidate:
    """One row in Proposal.candidates_ranked.

    Carries the runtime risk_score (from validate_procedure) alongside
    the catalog-level editorial signals (effort_score, mission_impact,
    reversibility) so the frontend can render a sortable candidates
    table without re-reading the registry.
    """
    procedure: Procedure
    risk_score: float
    validation: ValidationResult
    effort_score: float
    mission_impact: str
    reversibility: str


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
    candidates_ranked: List[RankedCandidate] = field(default_factory=list)
    # The multi-axis lexicographic key that picked the winner. Stored
    # on the Proposal so the runbook can show the operator which axis
    # decided the comparison. The full list of all candidates' keys
    # is reconstructable from candidates_ranked (each RankedCandidate
    # carries the catalog fields; risk_score is the runtime value).
    winner_rank_key: tuple = field(default_factory=tuple)

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
                {
                    "procedure": rc.procedure.value,
                    "risk_score": rc.risk_score,
                    "effort_score": rc.effort_score,
                    "mission_impact": rc.mission_impact,
                    "reversibility": rc.reversibility,
                }
                for rc in self.candidates_ranked
            ],
            "winner_rank_key": list(self.winner_rank_key),
        }


def _run_one(
    proc: Procedure,
    starting_state: Dict[str, Any],
    horizon_s: float,
    dt_s: float,
    progress_cb: Optional[Callable[[str, int, int, Dict[str, Any]], None]],
) -> RankedCandidate:
    """Validate a single candidate procedure in a worker thread.

    Builds an on_step callback that prefixes the procedure name onto
    the progress event so the consumer (bridge/WS server) can
    distinguish events from the 5 parallel sims without maintaining
    a per-thread registry.
    """
    params = get_default_params(proc)

    def on_step(step: int, total: int, t_s: float, snapshot: Dict[str, Any]) -> None:
        if progress_cb is not None:
            progress_cb(proc.value, step, total, snapshot)

    result = validate_procedure(
        starting_state, proc, params,
        horizon_s=horizon_s, dt_s=dt_s,
        on_step=on_step,
    )
    spec = PROCEDURE_REGISTRY[proc]
    return RankedCandidate(
        procedure=proc,
        risk_score=result.risk_score,
        validation=result,
        effort_score=spec.effort_score,
        mission_impact=spec.mission_impact,
        reversibility=spec.reversibility,
    )


def propose(
    cause: Cause,
    starting_state: Dict[str, Any],
    cause_score: float = 1.0,
    horizon_s: float = 3600.0,
    dt_s: float = 120.0,
    progress_cb: Optional[Callable[[str, int, int, Dict[str, Any]], None]] = None,
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
        progress_cb: optional per-step progress callback fired from
            the worker thread(s) running each validate_procedure call.
            Signature: (procedure_value: str, step: int, total: int,
            snapshot: dict). Used by the bridge/WS server to stream
            simulation progress to the frontend. Consumer must be
            thread-safe (typically wraps in queue.Queue).

    Returns:
        Proposal with the winning procedure, the full top-K ranking
        with catalog fields, and the BIBLE-shaped Verdict. Wall time
        measured: 30-120ms for the demo's 2-3 candidates at the
        default 1h/120s horizon (now parallel; same latency budget).
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
        wait_spec = PROCEDURE_REGISTRY[Procedure.WAIT]
        return Proposal(
            cause=cause,
            cause_score=cause_score,
            procedure=Procedure.WAIT,
            procedure_params=wait_params,
            risk_score=wait_validation.risk_score,
            validation=wait_validation,
            verdict=wait_verdict,
            candidates_ranked=[RankedCandidate(
                procedure=Procedure.WAIT,
                risk_score=wait_validation.risk_score,
                validation=wait_validation,
                effort_score=wait_spec.effort_score,
                mission_impact=wait_spec.mission_impact,
                reversibility=wait_spec.reversibility,
            )],
            winner_rank_key=(
                _MISSION_IMPACT_SCORE.get(wait_spec.mission_impact, 0.5),
                wait_spec.effort_score,
                _REVERSIBILITY_SCORE.get(wait_spec.reversibility, 0.1),
                wait_validation.risk_score,
            ),
        )

    # 1. Pre-filter: keep top K by catalog-level signals (cheap, no twin).
    #    When the candidate set is smaller than PROPOSE_TOP_K, this is
    #    effectively a stable sort by coarse_rank.
    scored = [(_coarse_rank_key(p), p) for p in candidates]
    scored.sort(key=lambda x: x[0])
    top_k = [p for _, p in scored[:PROPOSE_TOP_K]]

    # 2. Simulate all top-K in parallel in the digital twin.
    #    Each worker calls validate_procedure() (which itself runs the
    #    twin forward twice — baseline + predicted) and reports
    #    per-step progress via progress_cb.
    workers = min(PROPOSE_TOP_K, len(top_k))
    results: List[RankedCandidate] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # pool.map preserves order of the input iterable
        for rc in pool.map(
            lambda p: _run_one(p, starting_state, horizon_s, dt_s, progress_cb),
            top_k,
        ):
            results.append(rc)

    # 3. Multi-axis lexicographic sort. Lowest rank_key wins.
    #    Order: (mission_impact, effort_score, reversibility, risk_score).
    #    See _rank_key() docstring for the rationale.
    results.sort(key=_rank_key)
    best = results[0]
    best_verdict = to_verdict(best.validation, best.procedure, cause)

    return Proposal(
        cause=cause,
        cause_score=cause_score,
        procedure=best.procedure,
        procedure_params=get_default_params(best.procedure),
        risk_score=best.risk_score,
        validation=best.validation,
        verdict=best_verdict,
        candidates_ranked=results,
        winner_rank_key=_rank_key(best),
    )
