"""
Twin state: a flat dict view of the entire simulation state at one timestep.

This is the canonical state shape used by:
  - apply_procedure() in procedures.py (mutates state in place)
  - the channel shaper (reads state to produce Telemanom .npy)
  - the validation function (takes a starting state, returns a trajectory)
  - the run simulator (writes state every step, then dumps to per_step_arrays)

If you add a new field, also:
  1. Update the channel shaper to either include or exclude it
  2. Update the reset() defaults
  3. Update TWIN_REFERENCE.md section 3.1 data contract

Key conventions:
  - All temperatures in degrees Celsius
  - All voltages in Volts
  - All currents in Amps
  - All powers in Watts
  - All times in seconds (sim time, not wall clock)
  - attitude_mode is one of: "nadir", "sun", "ground", "safe_hold", "thomson"
  - operating_mode is the CHESS int: 0=IDLE, 1=SAFE, 2=CHARGING, 3=UHF, 4=X_BAND, 5=MEAS
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple
import numpy as np


# Canonical list of state fields, in dependency order
STATE_FIELDS: List[str] = [
    # Time
    "t_s",
    # EPS
    "battery_soc",
    "battery_voltage_v",
    "battery_current_a",
    "battery_internal_r_ohm",
    "battery_energy_ws",
    "load_a",
    "solar_input_w",
    "solar_input_multiplier",
    # Thermal (degrees C)
    "battery_temp_c",
    "payload_temp_c",
    "electronics_temp_c",
    "radiator_temp_c",
    "heater_duty_battery",
    "heater_duty_payload",
    "heater_duty_electronics",
    "heater_power_battery_w",
    "heater_power_payload_w",
    "heater_power_electronics_w",
    # ADCS
    "pointing_error_deg",
    "pointing_rate_deg_s",
    "wheel_speed_x_rpm",
    "wheel_speed_y_rpm",
    "wheel_speed_z_rpm",
    "attitude_mode",
    "star_tracker_ok",
    "pointing_drift_rate_deg_s",
    # Comms
    "link_margin_db",
    "elevation_to_gs_deg",
    "comms_enabled",
    "comms_re_enable_t",
    # Mode / power
    "operating_mode",
    "payload_power_w",
    # Time-limited actions (when do temporary mutations expire)
    "load_a_shed_until_t",
    "payload_throttle_until_t",
    "wait_until_t",
    "star_tracker_re_enable_t",
    # Bookkeeping
    "_last_procedure",
    "_last_procedure_params",
    "_fault_flags",
]


def make_default_state() -> Dict[str, Any]:
    """Return a fresh state dict with sensible defaults matching the CHESS
    spacecraft_template.json. Use as the starting point for the very first
    sim step."""
    return {
        "t_s": 0.0,
        # EPS
        "battery_soc": 1.0,                    # full
        "battery_voltage_v": 28.0,             # nominal 28V bus
        "battery_current_a": 0.0,              # idle
        "battery_internal_r_ohm": 0.05,        # healthy cell
        "battery_energy_ws": 76.32 * 3600.0,   # 76.32 Wh in Ws
        "load_a": 2.0,                         # nominal load
        "solar_input_w": 0.0,                  # computed by EPS step
        "solar_input_multiplier": 1.0,
        # Thermal
        "battery_temp_c": 20.0,
        "payload_temp_c": 20.0,
        "electronics_temp_c": 20.0,
        "radiator_temp_c": 10.0,
        "heater_duty_battery": 0.5,
        "heater_duty_payload": 0.5,
        "heater_duty_electronics": 0.5,
        "heater_power_battery_w": 5.0,
        "heater_power_payload_w": 0.0,
        "heater_power_electronics_w": 3.0,
        # ADCS
        "pointing_error_deg": 0.5,
        "pointing_rate_deg_s": 0.0,
        "wheel_speed_x_rpm": 0.0,
        "wheel_speed_y_rpm": 0.0,
        "wheel_speed_z_rpm": 0.0,
        "attitude_mode": "sun",                # start sun-pointing to charge
        "star_tracker_ok": True,
        "pointing_drift_rate_deg_s": 0.0,
        # Comms
        "link_margin_db": 0.0,
        "elevation_to_gs_deg": 0.0,
        "comms_enabled": True,
        "comms_re_enable_t": 0.0,
        # Mode / power
        "operating_mode": 0,                   # CHESS IDLE
        "payload_power_w": 7.98,
        # Time-limited
        "load_a_shed_until_t": 0.0,
        "payload_throttle_until_t": 0.0,
        "wait_until_t": 0.0,
        "star_tracker_re_enable_t": 0.0,
        # Bookkeeping
        "_last_procedure": None,
        "_last_procedure_params": None,
        "_fault_flags": {},
    }


def state_to_array(states: List[Dict[str, Any]], field_name: str) -> np.ndarray:
    """Stack a list of state dicts into a 1D numpy array for one field."""
    return np.array([s.get(field_name, 0.0) for s in states], dtype=np.float64)


def build_per_step_arrays(state_history: List[Dict[str, Any]]) -> Dict[str, np.ndarray]:
    """Convert a list of state dicts (one per timestep) into a dict of
    1D numpy arrays keyed by STATE_FIELDS. Returns a dict that matches
    the SimulationOutput.per_step_arrays contract from TWIN_REFERENCE.md.

    Special handling:
      - attitude_mode: stored as int code (nadir=0, sun=1, ground=2, safe_hold=3, thomson=4)
      - star_tracker_ok, comms_enabled: stored as float (0.0 / 1.0)
      - _last_procedure, _last_procedure_params, _fault_flags: excluded (debug only)
    """
    attitude_code = {"nadir": 0, "sun": 1, "ground": 2, "safe_hold": 3, "thomson": 4}
    out: Dict[str, np.ndarray] = {}
    for f in STATE_FIELDS:
        if f in ("_last_procedure", "_last_procedure_params", "_fault_flags"):
            continue
        if f == "attitude_mode":
            arr = np.array(
                [attitude_code.get(s.get("attitude_mode", "sun"), 1) for s in state_history],
                dtype=np.float64,
            )
        elif f in ("star_tracker_ok", "comms_enabled"):
            arr = np.array(
                [1.0 if s.get(f, True) else 0.0 for s in state_history],
                dtype=np.float64,
            )
        else:
            arr = state_to_array(state_history, f)
        out[f] = arr
    return out


def snapshot_for_callback(t_s: float, ches_state: Dict[str, Any]) -> Dict[str, Any]:
    """Build a fresh state dict from CHESS's per-step fields + an explicit
    sim time. Use this in the per_step_callback to seed the state dict
    that the new modules will mutate.

    The CHESS fields we can populate at this stage:
      - battery_energy_ws (from Eps.get_battery_energy())
      - operating_mode (from switch_algo.operating_mode)
      - t_s, eclipse, com_window, mode (from the snapshot itself)
    Other fields will be initialized from make_default_state() the first
    time and mutated by EPS/Thermal/ADCS each step.
    """
    state = make_default_state()
    state["t_s"] = t_s
    if "battery_energy_ws" in ches_state:
        state["battery_energy_ws"] = ches_state["battery_energy_ws"]
        state["battery_soc"] = state["battery_energy_ws"] / 274752.0
    if "operating_mode" in ches_state:
        state["operating_mode"] = ches_state["operating_mode"]
    if "eclipse" in ches_state:
        # record but don't store as a state field — handled by orbital env
        pass
    if "com_window" in ches_state:
        state["comms_enabled"] = ches_state["com_window"]
    return state
