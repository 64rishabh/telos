"""
Physical ADCS (Attitude Determination and Control) model.

Replaces CHESS's stub Adcs class. 2nd-order damped pointing + reaction
wheel state. Single scalar pointing error (no quaternions — keeps the
math simple while preserving the dynamics the demo cares about).

State:
  - pointing_error_deg: scalar magnitude of pointing offset
  - pointing_rate_deg_s: time derivative of pointing_error
  - wheel_speed_x/y/z_rpm: reaction wheel speeds, saturate at ±6000 rpm

Controller: PD with kp, kd. Target pointing is a function of attitude_mode:
  - "nadir": pointing target = 0 (perfect nadir pointing desired)
  - "sun": pointing target = 0 (perfect sun pointing desired)
  - "ground": pointing target = 0 (perfect ground tracking desired)
  - "safe_hold": controller disabled, only detumble/damping runs
  - "thomson": spin, controller disabled

Disturbance: gravity gradient + aerodynamic drag, modeled as a constant
torque. This is the source of "drift" that the controller has to fight.

Couples to:
  - EPS: attitude_mode → solar incidence angle → solar_input_w
  - Comms: attitude_mode + pointing_error → link_margin

The step_adcs() function sub-steps internally (max 5s per substep) so
the explicit Euler integrator is stable even when the caller passes a
large dt_s (e.g., 60s for fast validation).
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, Tuple


DEFAULT_ADCS_PARAMS = {
    "inertia_kg_m2": 0.05,
    "wheel_inertia_kg_m2": 1e-5,
    "wheel_saturation_rpm": 6000.0,
    # Tuned for stability when sub-stepped at 5s: omega_n ~0.1 rad/s, period ~60s
    "controller_kp": 0.0005,         # N·m per deg of error
    "controller_kd": 0.005,          # N·m per deg/s of rate (zeta ~1.0)
    "disturbance_torque_nm": 1e-7,   # gravity gradient + drag, very small
    "safe_hold_damping": 0.002,      # detumble rate
    "star_tracker_drift_deg_s": 0.05,
    "max_substep_s": 5.0,
}


def _wheel_saturation(rpm: float, sat: float) -> float:
    return max(-sat, min(sat, rpm))


def step_adcs(
    state: Dict[str, Any],
    dt_s: float,
    params: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """Advance ADCS state by one timestep. Sub-steps internally for stability.

    Reads:
      - pointing_error_deg, pointing_rate_deg_s
      - wheel_speed_x/y/z_rpm
      - attitude_mode, star_tracker_ok
      - pointing_drift_rate_deg_s (set by faults/procedures)

    Writes:
      - pointing_error_deg, pointing_rate_deg_s
      - wheel_speed_x/y/z_rpm
    """
    if params is None:
        params = DEFAULT_ADCS_PARAMS
    max_sub = params.get("max_substep_s", 5.0)
    n_sub = max(1, int(dt_s / max_sub) + 1)
    sub_dt = dt_s / n_sub
    for _ in range(n_sub):
        _adcs_substep(state, sub_dt, params)
    return state


def _adcs_substep(
    state: Dict[str, Any],
    dt_s: float,
    params: Dict[str, Any],
) -> None:
    """Single ADCS substep. Internal — use step_adcs() instead."""
    error = state.get("pointing_error_deg", 0.5)
    rate = state.get("pointing_rate_deg_s", 0.0)
    mode = state.get("attitude_mode", "sun")
    star_ok = state.get("star_tracker_ok", True)
    drift = state.get("pointing_drift_rate_deg_s", 0.0)

    # Controller logic per mode
    if mode in ("nadir", "sun", "ground"):
        if star_ok:
            control_torque = (
                -params["controller_kp"] * error
                -params["controller_kd"] * rate
            )
        else:
            control_torque = -params["controller_kd"] * rate * 0.3
            error += drift * dt_s
    elif mode == "safe_hold":
        target_error = 0.0
        control_torque = (
            -params["controller_kp"] * 0.1 * (error - target_error)
            -params["safe_hold_damping"] * rate
        )
    elif mode == "thomson":
        control_torque = 0.0
    else:
        control_torque = 0.0

    disturbance = params["disturbance_torque_nm"]
    I = params["inertia_kg_m2"]
    net_torque = control_torque - disturbance
    angular_accel = net_torque / I
    new_rate = rate + angular_accel * dt_s
    new_error = error + new_rate * dt_s

    wheel_command_rpm = control_torque * 100.0
    new_wheel_x = _wheel_saturation(
        state.get("wheel_speed_x_rpm", 0.0) + wheel_command_rpm * dt_s,
        params["wheel_saturation_rpm"]
    )
    if abs(new_wheel_x) >= params["wheel_saturation_rpm"] - 1.0:
        new_rate = new_rate * 1.1

    state["pointing_error_deg"] = new_error
    state["pointing_rate_deg_s"] = new_rate
    state["wheel_speed_x_rpm"] = new_wheel_x
    state["wheel_speed_y_rpm"] = new_wheel_x * 0.5
    state["wheel_speed_z_rpm"] = new_wheel_x * 0.3
