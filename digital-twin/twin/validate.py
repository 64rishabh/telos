"""
Validation: project the twin forward from a starting state with a proposed
procedure applied, return the predicted trajectory.

This is what the recovery planner's candidates are scored against.

validate_procedure(starting_state, procedure, params, horizon_s)
  -> ValidationResult with:
      - predicted_trajectory: dict of state-field -> 1D array over horizon
      - baseline_trajectory: same fields with no action taken
      - feasible: bool
      - violations: list of constraint violations
      - risk_score: float (0..1)
      - summary: human-readable string
      - postcondition_check: result of running ProcedureSpec.check_postconditions
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import copy
import math
import numpy as np

from twin.state import make_default_state
from twin.eps import step_eps, DEFAULT_EPS_PARAMS
from twin.thermal import step_thermal, DEFAULT_THERMAL_PARAMS
from twin.adcs import step_adcs, DEFAULT_ADCS_PARAMS
from twin.fault_injection import FaultScheduler
from twin.procedures import (
    Procedure, PROCEDURE_REGISTRY, apply_procedure, get_candidate_procedures
)


# Limits for "violation" detection
CONSTRAINTS = {
    "battery_soc":         (0.10, 1.00),    # never below 10%
    "battery_voltage_v":   (24.0, 32.0),    # 24V is LVC for 28V bus
    "battery_temp_c":      (-20.0, 60.0),   # Li-ion survival window
    "electronics_temp_c":  (-20.0, 70.0),
    "payload_temp_c":      (-20.0, 60.0),
    "pointing_error_deg":  (0.0, 10.0),     # 10 deg is unacceptable
}


@dataclass
class ValidationResult:
    feasible: bool
    violations: List[Dict[str, Any]]
    risk_score: float                       # 0..1, lower is safer
    predicted_trajectory: Dict[str, np.ndarray]
    baseline_trajectory: Dict[str, np.ndarray]
    summary: str
    postcondition_check: Optional[bool] = None


def _run_forward(
    starting_state: Dict[str, Any],
    duration_s: float,
    dt_s: float,
    apply_procs: Optional[List[tuple]] = None,
    fault_schedule: Optional[List[Dict[str, Any]]] = None,
    eps_params: Optional[Dict] = None,
    thermal_params: Optional[Dict] = None,
    adcs_params: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """Run the twin forward from starting_state for duration_s seconds.

    apply_procs: list of (Procedure, params, start_t, end_t) to apply during the run
    fault_schedule: list of fault dicts to schedule
    Returns list of state dicts (one per timestep).

    Procedures are applied ONCE at their start_t (not every step) — the
    apply function mutates state in a way that's intended to be persistent
    for the procedure's duration. Use the procedure's duration_s from
    params to know when to "release" by restoring the pre-procedure values.
    """
    if eps_params is None: eps_params = DEFAULT_EPS_PARAMS
    if thermal_params is None: thermal_params = DEFAULT_THERMAL_PARAMS
    if adcs_params is None: adcs_params = DEFAULT_ADCS_PARAMS

    scheduler = FaultScheduler()
    for f_spec in (fault_schedule or []):
        params = f_spec.get("parameters", {})
        scheduler.inject(
            fault_type=f_spec["type"],
            start_t=f_spec["start_t"],
            end_t=f_spec.get("end_t"),
            parameters=params,
        )

    # Deep copy the starting state
    state = copy.deepcopy(starting_state)
    n_steps = max(1, int(duration_s / dt_s))

    # For the validation horizon, we don't have CHESS's orbit/eclipse info.
    # Use a simple time-based approximation
    orbit_period_s = 35 * 60.0
    eclipse_fraction = 0.4

    # Snapshot pre-procedure state for each procedure, so we can release
    pre_procedure_snapshots = {}   # (proc, p_start) -> dict of original values
    for proc, params, p_start, p_end in (apply_procs or []):
        try:
            snapshot = {}
            for k in ("load_a", "attitude_mode", "heater_duty_battery",
                     "heater_duty_payload", "heater_duty_electronics",
                     "payload_power_w", "operating_mode", "star_tracker_ok",
                     "comms_enabled", "solar_input_multiplier"):
                if k in state:
                    snapshot[k] = copy.deepcopy(state[k])
            pre_procedure_snapshots[(proc, p_start)] = snapshot
        except Exception:
            pass

    states: List[Dict[str, Any]] = []
    for step in range(n_steps + 1):
        t_s = step * dt_s
        state["t_s"] = t_s
        orbit_phase = (t_s % orbit_period_s) / orbit_period_s
        in_eclipse = orbit_phase < eclipse_fraction
        sun_angle = 0.0 if not in_eclipse else 60.0

        # Apply faults
        scheduler.apply(t_s, state, eps_params, thermal_params, adcs_params)

        # Manage procedure lifecycle: apply at start_t, release at end_t
        for proc, params, p_start, p_end in (apply_procs or []):
            if t_s == p_start:   # apply once at start
                try:
                    apply_procedure(state, proc, params)
                except ValueError:
                    pass
            elif t_s >= p_end:   # release at end
                snapshot = pre_procedure_snapshots.get((proc, p_start), {})
                for k, v in snapshot.items():
                    state[k] = v

        # Step physics
        step_eps(state, dt_s=dt_s, in_eclipse=in_eclipse, sun_angle_deg=sun_angle, params=eps_params)
        step_thermal(state, dt_s=dt_s, in_eclipse=in_eclipse, sun_angle_deg=sun_angle,
                     payload_power_w=state.get("payload_power_w", 7.98), params=thermal_params)
        step_adcs(state, dt_s=dt_s, params=adcs_params)

        states.append(copy.deepcopy(state))
    return states


def _check_violations(trajectory: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return list of constraint violations across the trajectory."""
    violations = []
    for field_name, (lo, hi) in CONSTRAINTS.items():
        for i, s in enumerate(trajectory):
            v = s.get(field_name, None)
            if v is None:
                continue
            # For pointing error, use absolute value (negative errors are just as bad)
            if field_name == "pointing_error_deg":
                v = abs(v)
            if v < lo or v > hi:
                violations.append({
                    "field": field_name,
                    "value": v,
                    "limit": (lo, hi),
                    "t_s": s.get("t_s", i * 60.0),
                })
                break   # one violation per field is enough
    return violations


def _compute_risk(trajectory: List[Dict[str, Any]], baseline: List[Dict[str, Any]]) -> float:
    """Risk score in [0, 1]. Computed as:
       - fraction of trajectory that violates constraints
       - plus the relative SoC drop compared to baseline (worse outcome = higher risk)
    """
    if not trajectory:
        return 1.0
    constraint_violations = 0
    for s in trajectory:
        for field_name, (lo, hi) in CONSTRAINTS.items():
            v = s.get(field_name, None)
            if v is not None and (v < lo or v > hi):
                constraint_violations += 1
                break
    violation_rate = constraint_violations / len(trajectory)

    # SoC degradation vs baseline
    base_soc_end = baseline[-1].get("battery_soc", 1.0) if baseline else 1.0
    pred_soc_end = trajectory[-1].get("battery_soc", 1.0)
    soc_diff = max(0.0, base_soc_end - pred_soc_end)   # higher = procedure made SoC worse

    return min(1.0, 0.5 * violation_rate + 0.5 * min(1.0, soc_diff * 2.0))


def validate_procedure(
    starting_state: Dict[str, Any],
    procedure: Procedure,
    params: dict,
    horizon_s: float = 14400.0,             # 4 hours default
    dt_s: float = 60.0,                     # 1-minute steps for validation
    fault_schedule: Optional[List[Dict[str, Any]]] = None,
    eps_params: Optional[Dict] = None,
    thermal_params: Optional[Dict] = None,
    adcs_params: Optional[Dict] = None,
) -> ValidationResult:
    """Project the twin forward with the procedure applied. Returns a
    ValidationResult comparing predicted outcome vs no-action baseline.

    Args:
        starting_state: state dict (the current sim state)
        procedure: which Procedure to apply
        params: validated parameters for the procedure
        horizon_s: how far forward to project (default 4 hours)
        dt_s: timestep for the projection (default 1 minute — coarser than sim for speed)

    Returns:
        ValidationResult with predicted/baseline trajectories, feasibility, risk
    """
    # 1. Validate params
    spec = PROCEDURE_REGISTRY[procedure]
    spec.validate_params(params)

    # 2. Run baseline (no procedure applied)
    baseline = _run_forward(
        starting_state=starting_state,
        duration_s=horizon_s,
        dt_s=dt_s,
        apply_procs=[],
        fault_schedule=fault_schedule,
        eps_params=eps_params,
        thermal_params=thermal_params,
        adcs_params=adcs_params,
    )

    # 3. Run with procedure applied for its full duration
    predicted = _run_forward(
        starting_state=starting_state,
        duration_s=horizon_s,
        dt_s=dt_s,
        apply_procs=[(procedure, params, 0.0, params.get("duration_s", horizon_s))],
        fault_schedule=fault_schedule,
        eps_params=eps_params,
        thermal_params=thermal_params,
        adcs_params=adcs_params,
    )

    # 4. Convert state lists to per-field arrays
    def to_arrays(state_list):
        attitude_code = {"nadir": 0, "sun": 1, "ground": 2, "safe_hold": 3, "thomson": 4}
        out = {}
        for k, v0 in state_list[0].items():
            if k.startswith("_") or isinstance(v0, (dict, list, tuple)):
                continue
            if k == "attitude_mode":
                out[k] = np.array(
                    [attitude_code.get(s.get("attitude_mode", "sun"), 1) for s in state_list],
                    dtype=np.float64,
                )
            elif k in ("star_tracker_ok", "comms_enabled", "in_eclipse"):
                out[k] = np.array(
                    [1.0 if s.get(k, True) else 0.0 for s in state_list],
                    dtype=np.float64,
                )
            elif isinstance(v0, str):
                continue   # skip other string fields
            else:
                try:
                    out[k] = np.array([s.get(k, 0.0) for s in state_list], dtype=np.float64)
                except (TypeError, ValueError):
                    continue
        return out

    predicted_arr = to_arrays(predicted)
    baseline_arr = to_arrays(baseline)

    # 5. Check constraints
    violations = _check_violations(predicted)

    # 6. Risk score
    risk = _compute_risk(predicted, baseline)

    # 7. Postcondition check (predicted vs baseline as a crude "did it work?" signal)
    try:
        post_ok = spec.check_postconditions(baseline[-1] if baseline else {}, predicted[-1] if predicted else {})
    except Exception:
        post_ok = None

    # 8. Summary
    base_soc = baseline[-1].get("battery_soc", 0.0)
    pred_soc = predicted[-1].get("battery_soc", 0.0)
    summary = (
        f"Procedure {procedure.value}: "
        f"SoC baseline={base_soc:.3f} predicted={pred_soc:.3f} "
        f"delta={pred_soc - base_soc:+.3f}, "
        f"violations={len(violations)}, risk={risk:.2f}"
    )

    return ValidationResult(
        feasible=(len(violations) == 0),
        violations=violations,
        risk_score=risk,
        predicted_trajectory=predicted_arr,
        baseline_trajectory=baseline_arr,
        summary=summary,
        postcondition_check=post_ok,
    )
