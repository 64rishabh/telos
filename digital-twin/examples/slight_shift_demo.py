"""
Single-scenario demo: a "slight shift" injection, end-to-end.

The owner has emphasized this story: a small, persistent level
shift on one channel, detected by the live LSTM, mapped to a
real twin fault, diagnosed, and a procedure recommended with
a real twin simulation behind it.

This script shows that exact arc with one scenario: a 0.2 unit
sustained shift on T-1 (payload temperature channel). That maps
via the bridge to thermal_heater_stuck_on; Diagnose identifies
the thermal cluster; Propose ranks the two candidate procedures;
Validate projects the spacecraft forward 1 hour with each.

It prints the same deep table as phase1_demo_deep.py but with
a smaller magnitude so the trajectory curves are gentle and
the story is "subtle fault, real diagnosis".

Run:

    cd /home/rishabh/c0de/telos
    python digital-twin/examples/slight_shift_demo.py
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "digital-twin"))

from twin.diagnose import (
    SymptomEvent, diagnose, MIN_DIAGNOSE_SCORE,
)
from twin.procedures import (
    Cause, Procedure, get_candidate_procedures, get_default_params,
)
from twin.propose import propose
from twin.state import make_default_state
from twin.validate import validate_procedure
from twin.verdict import to_verdict, RISK_THRESHOLD_INCONCLUSIVE


# The single scenario: a small sustained shift on T-1.
# Bridge mapping: (shift, T-1) -> thermal_heater_stuck_on
# magnitude 0.2 keeps the curves gentle so the story is readable.
CHANNEL = "T-1"
SUBSYSTEM = "thermal"
MAGNITUDE = 0.2
HORIZON_S = 3600.0
DT_S = 120.0


def main() -> int:
    print()
    print("=" * 72)
    print(f"  Slight-shift demo: a {MAGNITUDE}-unit shift on {CHANNEL}")
    print("=" * 72)
    print()
    print(f"  Story: an operator (or the live LSTM detector) sees")
    print(f"  telemetry on channel {CHANNEL} ({SUBSYSTEM}) drift")
    print(f"  upward by {MAGNITUDE} units and stay there. The bridge")
    print(f"  translates this to the twin fault thermal_heater_stuck_on")
    print(f"  and runs Diagnose -> Propose -> Validate.")

    # ---- Stage 1: Detect (synthetic — no live stream here) -----------
    # The live stream would produce AlertEvents after the EWMA-smoothed
    # error crosses threshold. We seed the symptom window with three
    # small events to simulate what the detector would emit.
    window = [
        SymptomEvent(
            channel=CHANNEL, subsystem=SUBSYSTEM,
            kind="shift", score=MAGNITUDE,
            seq=(i * 10, i * 10 + 9), ts=float(i * 10),
        )
        for i in range(3)
    ]
    print()
    print(f"  Stage 1 — Detect: 3 SymptomEvents on {CHANNEL} (synthetic)")
    for ev in window:
        print(f"    t={ev.ts:>5.0f}  channel={ev.channel}  score={ev.score}  kind={ev.kind}")

    # ---- Stage 2: Diagnose -------------------------------------------
    candidates = diagnose(window)
    print()
    print(f"  Stage 2 — Diagnose: every Cause scored against the window")
    print()
    print(f"    {'cause':<32s}  {'score':>6s}  {'matched':>7s}  evidence")
    print(f"    {'-'*32}  {'-'*6}  {'-'*7}  {'-'*24}")
    for c in candidates[:6]:
        ev = ",".join(c.evidence_subsystems) or "-"
        print(f"    {c.cause.value:<32s}  {c.score:>6.2f}  "
              f"{len(c.matched_events):>7d}  {ev}")
    if not candidates:
        print("    (no candidates — Diagnose returned empty)")
        return 1
    top = candidates[0]
    print()
    print(f"    -> top cause: {top.cause.value} (score {top.score:.2f})")

    # ---- Stage 3: Propose --------------------------------------------
    if top.cause == Cause.NO_FAULT_DETECTED:
        print()
        print("  Stage 3 — Propose: skipped (no fault detected)")
        return 0

    starting_state = make_default_state()
    proposal = propose(
        top.cause, starting_state, cause_score=top.score,
        horizon_s=HORIZON_S, dt_s=DT_S,
    )
    print()
    print(f"  Stage 3 — Propose: ranked candidate procedures for {top.cause.value}")
    print()
    print(f"    {'#':<3s}  {'procedure':<32s}  {'risk':>6s}  default params")
    print(f"    {'-'*3}  {'-'*32}  {'-'*6}  {'-'*40}")
    for i, (proc, risk, _) in enumerate(proposal.candidates_ranked):
        params = get_default_params(proc)
        params_str = ", ".join(f"{k}={v}" for k, v in params.items()) or "{}"
        print(f"    {i+1:<3d}  {proc.value:<32s}  {risk:>6.3f}  {params_str}")
    print()
    print(f"    -> chosen: {proposal.procedure.value} (risk {proposal.risk_score:.3f})")
    print(f"       verdict: {proposal.verdict.status.value}")
    print(f"       reason:  {proposal.verdict.reason}")

    # ---- Stage 4: Validate -------------------------------------------
    params = get_default_params(proposal.procedure)
    result = validate_procedure(
        starting_state, proposal.procedure, params,
        horizon_s=HORIZON_S, dt_s=DT_S,
    )
    pred = result.predicted_trajectory
    base = result.baseline_trajectory

    print()
    print(f"  Stage 4 — Validate: twin simulates 1 hour forward at 2-min steps")
    print()
    print(f"    {'t (min)':>8s}  {'pred SoC':>9s}  {'base SoC':>9s}  "
          f"{'pred V':>7s}  {'base V':>7s}  "
          f"{'pred T_bat':>10s}  {'base T_bat':>10s}  "
          f"{'pred T_pay':>10s}  {'base T_pay':>10s}")
    print(f"    {'-'*8}  {'-'*9}  {'-'*9}  {'-'*7}  {'-'*7}  "
          f"{'-'*10}  {'-'*10}  {'-'*10}  {'-'*10}")
    n = len(pred["battery_soc"])
    for i in range(0, n, max(1, n // 12)):
        t_min = (i * DT_S) / 60.0
        print(
            f"    {t_min:>8.0f}  "
            f"{pred['battery_soc'][i]:>9.3f}  "
            f"{base['battery_soc'][i]:>9.3f}  "
            f"{pred['battery_voltage_v'][i]:>7.2f}  "
            f"{base['battery_voltage_v'][i]:>7.2f}  "
            f"{pred['battery_temp_c'][i]:>10.2f}  "
            f"{base['battery_temp_c'][i]:>10.2f}  "
            f"{pred['payload_temp_c'][i]:>10.2f}  "
            f"{base['payload_temp_c'][i]:>10.2f}"
        )
    print()
    print(f"    summary: {result.summary}")
    print(f"    feasible: {result.feasible}, violations: {len(result.violations)}, "
          f"postcondition: {result.postcondition_check}")

    # ---- What does the operator do? ----------------------------------
    print()
    print("=" * 72)
    print("  The verdict, in one line:")
    print(f"    \"{proposal.procedure.value} for {top.cause.value} -> "
          f"{proposal.verdict.status.value}\"")
    print()
    if proposal.verdict.status.value == "OK":
        print("  -> Phase 1 ends here. In Phase 2 the policy engine would")
        print("     route this to the right approver (operator, director, etc.)")
        print("     and the executor would carry out the procedure.")
    elif proposal.verdict.status.value == "INCONCLUSIVE":
        print("  -> The twin says the procedure is feasible but the margin")
        print("     is thin. Operator review is warranted before approval.")
    else:  # REJECT
        print("  -> The twin says the procedure cannot complete safely.")
        print("     Try the next-ranked candidate, or escalate to a")
        print("     more aggressive procedure.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
