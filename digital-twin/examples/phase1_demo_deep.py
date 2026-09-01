"""
Phase 1 deep demo — surfaces the actual digital-twin simulation.

The standard phase1_demo.py prints the verdict (OK | REJECT | INCONCLUSIVE)
and the chosen procedure. This script goes one layer deeper and prints:

  1. The exact Diagnose scoring (per-cause breakdown of why a cause
     won the ranking).
  2. The Propose ranking (every candidate procedure + its risk_score).
  3. The Validate trajectory: the twin's predicted SoC, voltage, and
     battery temp for both the "no action" baseline and the "with
     procedure" prediction, side-by-side over the 1-hour horizon.

This is the script to show the judges at the hackathon if they ask
"is the twin really simulating, or is it just a lookup table?"

Run with:

    cd /home/rishabh/c0de/telos
    python digital-twin/examples/phase1_demo_deep.py

It will pick one EPS scenario and one Thermal scenario, run them
through the full pipeline, and print the trajectory tables.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Tuple

# Path setup so `import twin.*` works.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "digital-twin"))

# Twin imports — pure Python, no CHESS venv required.
from twin.diagnose import (
    SymptomEvent, diagnose, top_cause, MIN_DIAGNOSE_SCORE,
)
from twin.procedures import (
    Cause, Procedure, get_candidate_procedures, get_default_params,
)
from twin.propose import propose
from twin.state import make_default_state
from twin.validate import validate_procedure
from twin.verdict import to_verdict, RISK_THRESHOLD_INCONCLUSIVE


# Pick a couple of scenarios for the deep dive.
SCENARIOS: List[Tuple[str, str, str, str, str]] = [
    # (name, channel, subsystem, fault_type, kind)
    ("EPS: P-1 voltage drop",   "P-1", "eps",     "eps_internal_r_ramp",     "shift"),
    ("Thermal: T-1 stuck on",   "T-1", "thermal", "thermal_heater_stuck_on", "shift"),
]


def _run_diagnose(channel: str, subsystem: str) -> List[Dict[str, Any]]:
    """Seed a 3-event window on the target channel and score every Cause."""
    window = [
        SymptomEvent(channel=channel, subsystem=subsystem, kind="shift", score=1.0)
        for _ in range(3)
    ]
    candidates = diagnose(window)
    out = []
    for c in candidates:
        out.append({
            "cause": c.cause.value,
            "score": c.score,
            "matched_events": len(c.matched_events),
            "evidence_subsystems": c.evidence_subsystems,
        })
    return out


def _print_diagnose_table(channel: str, candidates: List[Dict[str, Any]]) -> None:
    print("  Diagnose: every cause scored against the symptom window")
    print(f"  (3 synthetic SymptomEvents on channel={channel})")
    print()
    print(f"    {'cause':<32s}  {'score':>6s}  {'events':>6s}  {'evidence'}")
    print(f"    {'-'*32}  {'-'*6}  {'-'*6}  {'-'*24}")
    for c in candidates[:8]:
        ev = ",".join(c["evidence_subsystems"]) or "-"
        print(f"    {c['cause']:<32s}  {c['score']:>6.2f}  {c['matched_events']:>6d}  {ev}")
    if candidates:
        print(f"    -> winner: {candidates[0]['cause']}")


def _run_propose(cause: Cause) -> Tuple[Any, List[Tuple[Procedure, float]]]:
    """Run Propose on the chosen cause. Return (Proposal, ranked list)."""
    starting_state = make_default_state()
    proposal = propose(
        cause, starting_state, cause_score=1.5,
        horizon_s=3600.0, dt_s=120.0,  # 1 hour, 2-minute steps
    )
    ranked = [(p, s) for (p, s, _) in proposal.candidates_ranked]
    return proposal, ranked


def _print_propose_table(cause: Cause, proposal: Any, ranked: List[Tuple[Procedure, float]]) -> None:
    print()
    print("  Propose: candidate procedures for this cause, ranked by risk")
    print()
    print(f"    {'#':<3s}  {'procedure':<32s}  {'risk':>6s}  default params")
    print(f"    {'-'*3}  {'-'*32}  {'-'*6}  {'-'*40}")
    for i, (proc, risk) in enumerate(ranked):
        params = get_default_params(proc)
        params_str = ", ".join(f"{k}={v}" for k, v in params.items()) or "{}"
        print(f"    {i+1:<3d}  {proc.value:<32s}  {risk:>6.3f}  {params_str}")
    print()
    print(f"    -> winner: {proposal.procedure.value}")
    print(f"       verdict: {proposal.verdict.status.value} (risk={proposal.risk_score:.3f}, "
          f"threshold={RISK_THRESHOLD_INCONCLUSIVE:.2f})")
    print(f"       reason:  {proposal.verdict.reason}")


def _print_trajectory(cause: Cause, procedure: Procedure, horizon_s: float, dt_s: float) -> None:
    """Run validate_procedure and print the predicted vs baseline trajectory."""
    starting_state = make_default_state()
    params = get_default_params(procedure)
    result = validate_procedure(
        starting_state, procedure, params,
        horizon_s=horizon_s, dt_s=dt_s,
    )
    pred = result.predicted_trajectory
    base = result.baseline_trajectory

    print()
    print(f"  Validate: twin simulates {horizon_s/60:.0f} min forward at {dt_s:.0f}s steps")
    print()
    print(f"    {'t (min)':>8s}  {'pred SoC':>9s}  {'base SoC':>9s}  "
          f"{'pred V':>7s}  {'base V':>7s}  {'pred T_bat':>10s}  {'base T_bat':>10s}")
    print(f"    {'-'*8}  {'-'*9}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*10}  {'-'*10}")
    n = len(pred["battery_soc"])
    for i in range(0, n, max(1, n // 10)):
        t_min = (i * dt_s) / 60.0
        print(
            f"    {t_min:>8.0f}  "
            f"{pred['battery_soc'][i]:>9.3f}  "
            f"{base['battery_soc'][i]:>9.3f}  "
            f"{pred['battery_voltage_v'][i]:>7.2f}  "
            f"{base['battery_voltage_v'][i]:>7.2f}  "
            f"{pred['battery_temp_c'][i]:>10.2f}  "
            f"{base['battery_temp_c'][i]:>10.2f}"
        )
    print()
    print(f"    summary: {result.summary}")
    print(f"    feasible: {result.feasible}, "
          f"violations: {len(result.violations)}, "
          f"postcondition: {result.postcondition_check}")


def _print_causal_chain(channel: str, subsystem: str, cause: Cause) -> None:
    print()
    print("  Causal chain (what the operator injected -> what the system diagnosed):")
    print(f"    channel {channel} ({subsystem}) -> "
          f"Cause.{cause.name} "
          f"(expected_channels={cause.expected_channels()}, "
          f"affected_subsystems={cause.affected_subsystems()})")


def main() -> int:
    horizon_s = 3600.0
    dt_s = 120.0

    for (name, channel, subsystem, fault_type, kind) in SCENARIOS:
        print()
        print("#" * 72)
        print(f"  {name}")
        print(f"  bridge mapping: ({kind}, {channel}) -> {fault_type}")
        print("#" * 72)

        # 1. Diagnose
        candidates = _run_diagnose(channel, subsystem)
        _print_diagnose_table(channel, candidates)
        top = Cause(candidates[0]["cause"])
        _print_causal_chain(channel, subsystem, top)

        # 2. Propose
        proposal, ranked = _run_propose(top)
        _print_propose_table(top, proposal, ranked)

        # 3. Validate (only the winner — the trajectory table for losers is just noise)
        _print_trajectory(top, proposal.procedure, horizon_s, dt_s)

    print()
    print("=" * 72)
    print("  That's the digital twin actually simulating: a step-by-step")
    print("  forward projection of the spacecraft state, with the proposed")
    print("  procedure applied, compared against the no-action baseline.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
