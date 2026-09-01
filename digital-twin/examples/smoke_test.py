"""
End-to-end smoke test for the digital twin.

Exercises the full detect -> diagnose -> propose -> validate loop with a
single injected fault. This is the script we'll use as the live-demo
backbone for the hackathon.

Run with:
  cd /home/rishabh/c0de/test/talk-to-claude
  source digital_twin_CubeSat/.venv/bin/activate
  python examples/smoke_test.py

The script:
  1. Runs the twin with an eps_internal_r_ramp fault
  2. Snapshots the post-fault state (simulating "agent sees an anomaly")
  3. Picks Cause.EPS_INTERNAL_R_DEGRADATION as the diagnosis (stub of
     what the diagnostic agent will do)
  4. Looks up candidate procedures for that cause
  5. Validates each candidate procedure against the post-fault state
  6. Ranks candidates by risk_score and prints the recommended plan
  7. Compares the predicted trajectory of the recommended plan vs the
     no-action baseline

In a real run, steps 3-4 would be replaced by the diagnostic agent's
output. We use a stub here so the smoke test is self-contained.
"""

from __future__ import annotations
import os
import sys
import math

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from twin.run_sim import run_sim
from twin.procedures import (
    Cause, Procedure, get_candidate_procedures, PROCEDURE_REGISTRY,
    apply_procedure,
)
from twin.validate import validate_procedure


CHESS_DIR = os.path.join(REPO_ROOT, "digital_twin_CubeSat")
SIM_CONFIG = os.path.join(CHESS_DIR, "data/simulation/simulation_smoke.json")
ORBIT_CONFIG = os.path.join(CHESS_DIR, "data/orbit/orbit_smoke.json")
SC_CONFIG = os.path.join(CHESS_DIR, "data/spacecraft/spacecraft_smoke.json")
GS_CONFIG = os.path.join(CHESS_DIR, "data/ground_station/ground_station_smoke.json")
MD_CONFIG = os.path.join(CHESS_DIR, "data/mission_design/mission_design_smoke.json")


def stub_diagnose(state) -> Cause:
    """Stub for the diagnostic agent. Looks at the post-fault state and
    picks the most likely cause. In a real run, the diagnostic agent
    (your first layer) would do this. For the smoke test, we know the
    fault we injected."""
    # If battery_internal_r_ohm is elevated, the cause is internal_r degradation
    r = state.get("battery_internal_r_ohm", 0.05)
    if r > 0.15:
        return Cause.EPS_INTERNAL_R_DEGRADATION
    if state.get("battery_soc", 1.0) < 0.3:
        return Cause.BATTERY_OVERDISCHARGE
    if state.get("star_tracker_ok", True) is False:
        return Cause.ADCS_STAR_TRACKER_LOST
    if state.get("electronics_temp_c", 20.0) > 50:
        return Cause.THERMAL_RUNAWAY
    return Cause.NO_FAULT_DETECTED


def main():
    print("=" * 70)
    print("STEP 1: Run twin with eps_internal_r_ramp fault")
    print("=" * 70)
    out = run_sim(
        chess_sim_config=SIM_CONFIG,
        chess_orbit_config=ORBIT_CONFIG,
        chess_spacecraft_config=SC_CONFIG,
        chess_ground_station_config=GS_CONFIG,
        chess_mission_design_config=MD_CONFIG,
        fault_schedule=[
            # Inject at t=60s, ramp to magnitude_ohm=0.4 over 300s
            {"type": "eps_internal_r_ramp", "start_t": 60, "end_t": 360,
             "parameters": {"magnitude_ohm": 0.4}},
        ],
        output_dir="/tmp/smoke_test",
    )
    print(f"  Sim completed: {out.run_metadata['n_timesteps']} steps over {out.run_metadata['duration_s']:.0f}s")
    print(f"  Channels written: {list(out.channels.keys())}")
    print(f"  Faults applied: {len(out.fault_log)}")
    for f in out.fault_log:
        print(f"    {f['fault_id']} {f['fault_type']} applied_at={f['applied_at']}")

    print()
    print("=" * 70)
    print("STEP 2: Snapshot the post-fault state (t=300s, mid-fault)")
    print("=" * 70)
    # Find the state at t=300s (deep into the fault)
    target_t = 300.0
    state_idx = None
    for i, s in enumerate(out.state_history):
        if abs(s.get("t_s", 0) - target_t) < 5.0:
            state_idx = i
            break
    if state_idx is None:
        state_idx = len(out.state_history) // 2
    post_fault_state = out.state_history[state_idx].copy()
    print(f"  Using state at t={post_fault_state['t_s']:.0f}s")
    print(f"    battery_soc:           {post_fault_state.get('battery_soc', 0):.3f}")
    print(f"    battery_voltage_v:     {post_fault_state.get('battery_voltage_v', 0):.2f} V")
    print(f"    battery_internal_r:    {post_fault_state.get('battery_internal_r_ohm', 0):.4f} ohm")
    print(f"    battery_temp_c:        {post_fault_state.get('battery_temp_c', 0):.2f} C")
    print(f"    electronics_temp_c:    {post_fault_state.get('electronics_temp_c', 0):.2f} C")
    print(f"    load_a:                {post_fault_state.get('load_a', 0):.2f} A")

    print()
    print("=" * 70)
    print("STEP 3: Diagnose (stub for the diagnostic agent)")
    print("=" * 70)
    cause = stub_diagnose(post_fault_state)
    print(f"  Diagnosed cause: {cause.value}")
    print(f"  Expected channels: {cause.expected_channels()}")
    print(f"  Affected subsystems: {cause.affected_subsystems()}")

    print()
    print("=" * 70)
    print("STEP 4: Look up candidate procedures")
    print("=" * 70)
    candidates = get_candidate_procedures(cause)
    print(f"  Candidates: {[p.value for p in candidates]}")

    print()
    print("=" * 70)
    print("STEP 5: Validate each candidate procedure")
    print("=" * 70)
    # Default parameters for each candidate
    default_params = {
        Procedure.EPS_SHED_LOAD:        {"load_reduction_a": 1.0,  "duration_s": 3600.0},
        Procedure.EPS_PRIORITIZE_CHARGING: {"load_reduction_a": 1.0, "duration_s": 3600.0, "solar_input_multiplier": 1.0},
        Procedure.MODE_CHANGE_TO_SAFE:  {},
        Procedure.WAIT:                 {"duration_s": 600.0},
    }
    results = []
    for proc in candidates:
        if proc not in default_params:
            print(f"  Skipping {proc.value} (no default params)")
            continue
        try:
            r = validate_procedure(
                starting_state=post_fault_state,
                procedure=proc,
                params=default_params[proc],
                horizon_s=4 * 3600.0,   # 4 hours
                dt_s=60.0,              # 1 minute steps
            )
            results.append((proc, default_params[proc], r))
            print(f"  {proc.value}: feasible={r.feasible}, risk={r.risk_score:.3f}, "
                  f"SoC_end_pred={r.predicted_trajectory['battery_soc'][-1]:.3f}, "
                  f"SoC_end_base={r.baseline_trajectory['battery_soc'][-1]:.3f}, "
                  f"postcond={r.postcondition_check}")
        except Exception as e:
            print(f"  {proc.value}: FAILED ({e})")

    print()
    print("=" * 70)
    print("STEP 6: Rank and recommend")
    print("=" * 70)
    results.sort(key=lambda x: x[2].risk_score)
    if results:
        best_proc, best_params, best_result = results[0]
        print(f"  Recommended: {best_proc.value} (risk {best_result.risk_score:.3f})")
        print(f"  Parameters: {best_params}")
        print(f"  Summary: {best_result.summary}")
    else:
        print("  No valid candidates found")
        return

    print()
    print("=" * 70)
    print("STEP 7: Predicted vs baseline trajectory (recommended procedure)")
    print("=" * 70)
    pred = best_result.predicted_trajectory
    base = best_result.baseline_trajectory
    print(f"  {'t (min)':>8s}  {'pred SoC':>9s}  {'base SoC':>9s}  {'pred V':>8s}  {'base V':>8s}  {'pred T_bat':>10s}")
    for i in range(0, len(pred['battery_soc']), 30):
        t_min = pred['t_s'][i] / 60.0 if 't_s' in pred else i
        print(f"  {t_min:8.0f}  {pred['battery_soc'][i]:9.3f}  {base['battery_soc'][i]:9.3f}  "
              f"{pred['battery_voltage_v'][i]:8.2f}  {base['battery_voltage_v'][i]:8.2f}  "
              f"{pred['battery_temp_c'][i]:10.2f}")

    print()
    print("=" * 70)
    print("DEMO READY: All steps completed successfully")
    print("=" * 70)


if __name__ == "__main__":
    main()
