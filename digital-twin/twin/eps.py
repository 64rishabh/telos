"""
Physical EPS (Electrical Power System) model.

Replaces CHESS's bookkeeping Eps class. Adds:
  - Terminal voltage: V_terminal = V_oc(SoC) - internal_r(T) * load
  - Temperature-dependent internal resistance: r(T) = r_nom * (1 + alpha*(T-T_ref))
  - Coulomb counting for SoC
  - Heater power draws from the load budget (thermal-EPS coupling)
  - Solar power from cell area * solar_flux * efficiency * cos(theta) * multiplier

Couples to:
  - Thermal: battery_temp_c → internal_r → terminal voltage
  - ADCS: attitude_mode → solar_input_w via cos(theta)
  - Comms: elevation_to_gs_deg → comms power draw in load_a
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any


# Defaults match CHESS's spacecraft_template.json
DEFAULT_EPS_PARAMS = {
    "capacity_wh": 76.32,
    "capacity_ws": 76.32 * 3600.0,             # 274,752 Ws
    "v_nominal": 28.0,                         # bus voltage nominal
    "v_slope": 4.0,                            # V_oc = 28 + 4*(SoC - 0.5), so 26-30V across SoC
    "internal_r_nominal_ohm": 0.05,            # healthy cell at T_ref
    "internal_r_temp_coeff": 0.01,             # d(ln r)/dT ≈ 1%/K for Li-ion
    "t_ref_c": 20.0,                           # reference temperature
    "n_cells": 28,
    "cell_area_m2": 0.0028355,
    "cell_efficiency": 0.25,
    "solar_flux_w_m2": 1367.0,                 # W/m^2 at Earth orbit
    "heater_to_load_factor": 0.072,            # W per (1.0 duty) per node → A at 28V
}


@dataclass
class EpsStepResult:
    """Returned by step_eps() for downstream logging/inspection."""
    battery_soc: float
    battery_voltage_v: float
    battery_current_a: float
    solar_input_w: float
    net_power_w: float
    load_a_total: float
    internal_r_ohm: float


def compute_internal_r(temperature_c: float, params: Dict[str, Any]) -> float:
    """Internal resistance rises with temperature. Temperature coefficient
    is positive (resistance rises with T) for typical Li-ion cells."""
    t_ref = params["t_ref_c"]
    alpha = params["internal_r_temp_coeff"]
    r_nom = params["internal_r_nominal_ohm"]
    return r_nom * (1.0 + alpha * (temperature_c - t_ref))


def compute_terminal_voltage(soc: float, internal_r: float, load_a: float, params: Dict[str, Any]) -> float:
    """V_terminal = V_oc(SoC) - I * R.  Load is treated as +ve discharge."""
    v_oc = params["v_nominal"] + params["v_slope"] * (soc - 0.5)
    return max(0.0, v_oc - internal_r * load_a)


def compute_solar_input(
    in_eclipse: bool,
    sun_angle_deg: float,
    attitude_mode: str,
    params: Dict[str, Any],
    solar_multiplier: float = 1.0,
) -> float:
    """Solar panel power generation. cos(theta) depends on attitude mode.

    For simplicity in this model:
      - "sun":       theta = 0, full power
      - "nadir":     theta = 30 deg, ~87% of full
      - "ground":    theta = 45 deg, ~71% of full
      - "safe_hold": theta = 0, full power (sun-pointing is the safe default)
      - "thomson":   theta = 60 deg, ~50% of full (random tumble)
    """
    if in_eclipse:
        return 0.0
    incidence = {
        "sun": 0.0, "safe_hold": 0.0,
        "nadir": 30.0, "ground": 45.0, "thomson": 60.0,
    }.get(attitude_mode, 30.0)
    import math
    cos_theta = math.cos(math.radians(min(incidence + sun_angle_deg, 89.0)))
    if cos_theta < 0:
        return 0.0
    base = (
        params["n_cells"]
        * params["cell_area_m2"]
        * params["solar_flux_w_m2"]
        * params["cell_efficiency"]
        * cos_theta
    )
    return base * solar_multiplier


def compute_heater_load_a(state: Dict[str, Any], params: Dict[str, Any]) -> float:
    """Convert heater duty cycles into a current draw on the EPS bus.
    Heaters draw from the power bus, so their consumption must be
    included in the total load. Each unit of duty on each node converts
    to a current via heater_to_load_factor (W per duty) / V_nominal."""
    factor = params["heater_to_load_factor"]
    total_duty = (
        state.get("heater_duty_battery", 0.0)
        + state.get("heater_duty_payload", 0.0)
        + state.get("heater_duty_electronics", 0.0)
    )
    return total_duty * factor


def step_eps(
    state: Dict[str, Any],
    dt_s: float,
    in_eclipse: bool,
    sun_angle_deg: float,
    params: Dict[str, Any],
) -> EpsStepResult:
    """Advance EPS state by one timestep.

    Reads:
      - state['battery_soc'], state['battery_energy_ws']
      - state['battery_temp_c']
      - state['load_a'] (user-specified base load, before heaters)
      - state['attitude_mode'], state['solar_input_multiplier']

    Writes:
      - state['battery_soc'], state['battery_energy_ws']
      - state['battery_voltage_v'], state['battery_current_a']
      - state['battery_internal_r_ohm']
      - state['solar_input_w']
    """
    # 1. Internal resistance. If a fault has set it directly (e.g. eps_internal_r_ramp),
    # use that value; otherwise compute from temperature.
    if "battery_internal_r_ohm" in state and state["battery_internal_r_ohm"] > 0:
        internal_r = state["battery_internal_r_ohm"]
    else:
        internal_r = compute_internal_r(state.get("battery_temp_c", params["t_ref_c"]), params)

    # 2. Heater load adds to the base load
    heater_load_a = compute_heater_load_a(state, params)
    total_load_a = state.get("load_a", 2.0) + heater_load_a

    # 3. Solar input
    solar_w = compute_solar_input(
        in_eclipse=in_eclipse,
        sun_angle_deg=sun_angle_deg,
        attitude_mode=state.get("attitude_mode", "sun"),
        params=params,
        solar_multiplier=state.get("solar_input_multiplier", 1.0),
    )

    # 4. Terminal voltage (depends on SoC, internal_r, total load)
    soc = state.get("battery_soc", 1.0)
    v_term = compute_terminal_voltage(soc, internal_r, total_load_a, params)

    # 5. Battery current. Positive = charging (solar into battery), negative = discharging.
    # Use v_term to convert solar watts to charging current.
    if v_term > 0.5:
        charge_current_a = solar_w / v_term
    else:
        charge_current_a = 0.0
    net_current_a = charge_current_a - total_load_a
    state["battery_current_a"] = net_current_a

    # 6. Coulomb counting
    capacity_ws = params["capacity_ws"]
    # Source of truth: battery_soc. Derive energy_ws from it. This avoids
    # desync if a caller updates SoC without updating energy_ws.
    soc = max(0.0, min(1.0, state.get("battery_soc", 1.0)))
    base_energy_ws = soc * capacity_ws
    delta_ws = net_current_a * v_term * dt_s   # energy in joules (Ws)
    new_energy_ws = base_energy_ws + delta_ws
    new_energy_ws = max(0.0, min(capacity_ws, new_energy_ws))
    new_soc = new_energy_ws / capacity_ws

    # 7. Update state
    state["battery_energy_ws"] = new_energy_ws
    state["battery_soc"] = new_soc
    state["battery_voltage_v"] = v_term
    state["battery_internal_r_ohm"] = internal_r
    state["solar_input_w"] = solar_w

    return EpsStepResult(
        battery_soc=new_soc,
        battery_voltage_v=v_term,
        battery_current_a=net_current_a,
        solar_input_w=solar_w,
        net_power_w=net_current_a * v_term,
        load_a_total=total_load_a,
        internal_r_ohm=internal_r,
    )
