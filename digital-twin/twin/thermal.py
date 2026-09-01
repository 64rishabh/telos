"""
Multi-node lumped RC thermal model.

Ported from PASEOS's single-node thermal_model.py equation (Martínez, EQ 20):
    mc * dT/dt = Q_solar + Q_albedo + Q_IR - Q_actor_emission + heater_power
                + sum_neighbors( G_nm * (T_m - T_n) )

Extended to 4 nodes (battery, payload, electronics, radiator) with inter-node
conductances. The radiator is the primary heat sink to deep space.

Heater inputs: per-node duty cycle [0, 1] converted to watts via
heater_capacity_w (default 5W per node at full duty).

Couples to:
  - EPS: battery_temp_c → internal_r (in eps.py via compute_internal_r)
  - ADCS: sun_angle_deg affects solar input to each node
  - Comms: not direct

Defaults match a smallsat thermal setup: 4 nodes, ~5W heaters, panel sun
input, radiator to space.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List


BOLTZMANN = 5.670374419e-8  # W/m^2/K^4

DEFAULT_THERMAL_PARAMS = {
    # Per-node thermal properties
    "nodes": {
        "battery": {
            "mass_kg": 1.2,
            "thermal_capacity_j_kg_k": 1000,    # typical Li-ion
            "sun_facing_area_m2": 0.005,        # small fraction of cell
            "central_body_facing_area_m2": 0.01,
            "emissive_area_m2": 0.02,
            "sun_absorptance": 0.5,
            "infrared_absorptance": 0.85,
            "emissivity": 0.85,
            "init_temp_c": 20.0,
        },
        "payload": {
            "mass_kg": 2.5,
            "thermal_capacity_j_kg_k": 800,
            "sun_facing_area_m2": 0.003,
            "central_body_facing_area_m2": 0.015,
            "emissive_area_m2": 0.025,
            "sun_absorptance": 0.4,
            "infrared_absorptance": 0.85,
            "emissivity": 0.85,
            "init_temp_c": 20.0,
        },
        "electronics": {
            "mass_kg": 0.8,
            "thermal_capacity_j_kg_k": 900,
            "sun_facing_area_m2": 0.002,
            "central_body_facing_area_m2": 0.008,
            "emissive_area_m2": 0.015,
            "sun_absorptance": 0.3,
            "infrared_absorptance": 0.85,
            "emissivity": 0.85,
            "init_temp_c": 22.0,                # runs slightly warmer
        },
        "radiator": {
            "mass_kg": 0.5,
            "thermal_capacity_j_kg_k": 500,    # thin aluminum
            "sun_facing_area_m2": 0.0,         # radiator faces deep space, not sun
            "central_body_facing_area_m2": 0.0,
            "emissive_area_m2": 0.05,           # large, exposed to space
            "sun_absorptance": 0.05,            # low absorptance coating
            "infrared_absorptance": 0.05,
            "emissivity": 0.90,                 # high emissivity
            "init_temp_c": 10.0,
        },
    },
    # Inter-node conductances (W/K) — like a thermal resistance network
    "conductances": {
        ("battery", "payload"): 0.5,
        ("payload", "electronics"): 0.4,
        ("electronics", "radiator"): 0.8,
        ("battery", "radiator"): 0.1,          # small direct path
    },
    # Heater capacity (W per node at full duty)
    "heater_capacity_w": {
        "battery": 5.0,
        "payload": 0.0,                        # no payload heater
        "electronics": 3.0,
        "radiator": 0.0,                       # no radiator heater
    },
    # Central body (Earth) thermal environment
    "body_solar_irradiance": 1360.0,
    "body_surface_temperature_k": 288.0,      # ~15 C
    "body_emissivity": 0.6,
    "body_reflectance": 0.3,
    # Heuristic: power dissipation from electronics becomes heat
    "power_to_heat_ratio": 0.5,
}


def _node_param(params: Dict[str, Any], node: str, key: str) -> Any:
    return params["nodes"][node][key]


def step_thermal(
    state: Dict[str, Any],
    dt_s: float,
    in_eclipse: bool,
    sun_angle_deg: float,
    payload_power_w: float,
    params: Dict[str, Any] = None,
) -> Dict[str, float]:
    """Advance the thermal state by one timestep. Returns dict of new
    node temperatures in Celsius.

    Reads from state:
      - battery_temp_c, payload_temp_c, electronics_temp_c, radiator_temp_c
      - heater_duty_battery, heater_duty_payload, heater_duty_electronics

    Writes back the same four fields.
    """
    if params is None:
        params = DEFAULT_THERMAL_PARAMS
    nodes = ["battery", "payload", "electronics", "radiator"]
    T = {n: state.get(f"{n}_temp_c", _node_param(params, n, "init_temp_c")) for n in nodes}
    T_kelvin = {n: T[n] + 273.15 for n in nodes}
    T_body_kelvin = params["body_surface_temperature_k"]

    # Heat flows (W) for each node
    Q = {n: 0.0 for n in nodes}

    # 1. Solar input (zero in eclipse)
    for n in nodes:
        a_sun = _node_param(params, n, "sun_facing_area_m2")
        a_sun_abs = _node_param(params, n, "sun_absorptance")
        if not in_eclipse and a_sun > 0:
            import math
            cos_theta = math.cos(math.radians(min(sun_angle_deg, 89.0)))
            if cos_theta < 0:
                cos_theta = 0.0
            Q[n] += a_sun_abs * a_sun * params["body_solar_irradiance"] * cos_theta

    # 2. Albedo (sun reflecting off Earth). For LEO, ~30% of solar reaches
    # the satellite. Only nodes facing Earth get this.
    for n in nodes:
        a_body = _node_param(params, n, "central_body_facing_area_m2")
        a_sun_abs = _node_param(params, n, "sun_absorptance")
        if not in_eclipse and a_body > 0:
            Q[n] += (
                a_sun_abs * a_body
                * params["body_reflectance"]
                * params["body_solar_irradiance"]
                * 0.5
            )

    # 3. Earth IR (Earth emits as a ~288K blackbody)
    for n in nodes:
        a_body = _node_param(params, n, "central_body_facing_area_m2")
        a_ir_abs = _node_param(params, n, "infrared_absorptance")
        if a_body > 0:
            Q[n] += (
                a_ir_abs * params["body_emissivity"] * a_body
                * BOLTZMANN * T_body_kelvin ** 4
            )

    # 4. Stefan-Boltzmann emission to space
    for n in nodes:
        a_emit = _node_param(params, n, "emissive_area_m2")
        emiss = _node_param(params, n, "emissivity")
        a_ir_abs = _node_param(params, n, "infrared_absorptance")
        Q[n] -= a_ir_abs * a_emit * emiss * BOLTZMANN * T_kelvin[n] ** 4

    # 5. Heater power (W per node at full duty * duty cycle)
    for n in nodes:
        duty = state.get(f"heater_duty_{n}", 0.0)
        cap = params["heater_capacity_w"].get(n, 0.0)
        if n == "radiator":
            cap = 0.0    # never
        Q[n] += duty * cap

    # 6. Electronics power dissipation (the EPS load becomes heat in electronics)
    if "electronics" in nodes:
        Q["electronics"] += params["power_to_heat_ratio"] * payload_power_w

    # 7. Inter-node conduction (the RC network)
    conductances = params["conductances"]
    for (a, b), G in conductances.items():
        Q[a] += G * (T[b] - T[a])
        Q[b] += G * (T[a] - T[b])

    # 8. Integrate (explicit Euler — fine for our 1-5s timesteps)
    for n in nodes:
        mass = _node_param(params, n, "mass_kg")
        cap_j_kg_k = _node_param(params, n, "thermal_capacity_j_kg_k")
        dT = (Q[n] * dt_s) / (mass * cap_j_kg_k)
        T[n] += dT
        # Clamp to physically reasonable range
        T[n] = max(-50.0, min(120.0, T[n]))

    # Write back
    for n in nodes:
        state[f"{n}_temp_c"] = T[n]
    return T
