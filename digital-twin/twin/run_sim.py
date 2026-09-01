"""
Run simulator orchestrator.

Wires together:
  - CHESS's simulation loop (orbit, eclipse, comms windows)
  - Our physical modules: EPS, thermal, ADCS
  - FaultScheduler (optional, scheduled via config)
  - Channel shaper (writes Telemanom-shaped .npy at the end)

Single entry point: run_sim(config) -> SimulationOutput
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
import os
import sys
import math
import numpy as np

# Add CHESS src to path
CHESS_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "digital_twin_CubeSat", "src")
if CHESS_SRC not in sys.path:
    sys.path.insert(0, CHESS_SRC)

from digital_twin.utils import parse_data_file
from digital_twin.simulation import Simulation

from twin.state import make_default_state, build_per_step_arrays
from twin.eps import step_eps, DEFAULT_EPS_PARAMS
from twin.thermal import step_thermal, DEFAULT_THERMAL_PARAMS
from twin.adcs import step_adcs, DEFAULT_ADCS_PARAMS
from twin.fault_injection import FaultScheduler
from twin.channel_shaper import shape_channels


@dataclass
class SimulationOutput:
    """Return value of run_sim(). Matches the contract in TWIN_REFERENCE.md."""
    per_step_arrays: Dict[str, np.ndarray]
    channels: Dict[str, np.ndarray]            # Telemanom-shaped
    fault_log: List[Dict[str, Any]]
    run_metadata: Dict[str, Any]
    state_history: List[Dict[str, Any]] = field(default_factory=list)

    def channel_path(self, channel_id: str, split: str = "test") -> str:
        out_dir = self.run_metadata.get("output_dir", "results")
        return os.path.join(out_dir, split, f"{channel_id}.npy")


def _sun_angle_from_rv(rv: np.ndarray, r_earth_sun) -> float:
    """Compute sun angle relative to panel normal. For the demo we use a
    simple model: panel normal is nadir (toward Earth center), so the
    sun angle is the angle between the sun direction and the negative-rv
    direction.
    """
    import numpy as np
    if r_earth_sun is None:
        return 0.0
    # Sun direction from satellite
    if hasattr(r_earth_sun, "to"):
        sun_vec = np.array([r_earth_sun.to("km").value for r_earth_sun in [r_earth_sun]])
    else:
        sun_vec = np.asarray(r_earth_sun)
    # Panel normal = -r_sat (nadir pointing)
    r_sat = rv[:3]
    r_sat_norm = r_sat / (np.linalg.norm(r_sat) + 1e-9)
    # If sun_vec is the r_earth_sun vector, sun direction from sat is r_earth_sun - r_sat
    if sun_vec.shape == (3,):
        sun_dir = sun_vec - r_sat
    else:
        sun_dir = sun_vec.flatten()[:3] - r_sat
    sun_dir_norm = sun_dir / (np.linalg.norm(sun_dir) + 1e-9)
    # Sun angle = angle between -panel_normal (which is +r_sat) and sun_dir
    cos_theta = np.dot(r_sat_norm, sun_dir_norm)
    cos_theta = np.clip(cos_theta, -1.0, 1.0)
    return math.degrees(math.acos(cos_theta))


def _elevation_to_gs(rv: np.ndarray, gs_coords) -> float:
    """Crude elevation: angle from local horizontal at the ground station
    to the satellite. For the demo we approximate as 90 - (angle from
    sat to gs center). If gs_coords is None, return 0.
    """
    if gs_coords is None:
        return 0.0
    if hasattr(gs_coords, "to"):
        gs = np.array([gs_coords.to("km").value for gs_coords in [gs_coords]])
    else:
        gs = np.asarray(gs_coords)
    gs = gs.flatten()[:3]
    r_sat = rv[:3]
    diff = r_sat - gs
    r_sat_norm = r_sat / (np.linalg.norm(r_sat) + 1e-9)
    diff_norm = diff / (np.linalg.norm(diff) + 1e-9)
    cos_zenith = np.dot(r_sat_norm, diff_norm)
    cos_zenith = np.clip(cos_zenith, -1.0, 1.0)
    zenith = math.degrees(math.acos(cos_zenith))
    return max(0.0, 90.0 - zenith)


def run_sim(
    chess_sim_config: str,
    chess_orbit_config: str,
    chess_spacecraft_config: str,
    chess_ground_station_config: str,
    chess_mission_design_config: str,
    fault_schedule: Optional[List[Dict[str, Any]]] = None,
    output_dir: str = "results/twin_output",
    eps_params: Optional[Dict] = None,
    thermal_params: Optional[Dict] = None,
    adcs_params: Optional[Dict] = None,
) -> SimulationOutput:
    """Run the full digital twin with our extensions.

    Args:
        chess_*_config: paths to the 5 CHESS JSON config files
        fault_schedule: list of dicts like {"type": "eps_internal_r_ramp", "start_t": 600, "parameters": {"magnitude_ohm": 0.4}}
        output_dir: where to write Telemanom channels
        eps_params/thermal_params/adcs_params: override our default params

    Returns:
        SimulationOutput with per_step_arrays, channels, fault_log, metadata
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

    sim = Simulation(
        parse_data_file(chess_sim_config),
        parse_data_file(chess_orbit_config),
        parse_data_file(chess_spacecraft_config),
        parse_data_file(chess_ground_station_config),
        parse_data_file(chess_mission_design_config),
    )
    sim.verbose = False

    # State history: a list of state dicts, one per timestep
    state_history: List[Dict[str, Any]] = []

    def callback(t_idx, snapshot):
        t_s = snapshot["t_s"]
        eclipse = snapshot["eclipse"]
        rv = snapshot["rv"]
        com_window = snapshot["com_window"]
        mode = snapshot["mode"]
        r_earth_sun = snapshot.get("r_earth_sun")
        gs_coords = snapshot.get("gs_coords")

        # Build the state at this step
        if not state_history:
            state = make_default_state()
        else:
            # Deep-ish copy of last state (shallow for the demo)
            state = dict(state_history[-1])
        state["t_s"] = t_s
        state["operating_mode"] = mode

        # Compute derived quantities from CHESS outputs
        sun_angle = _sun_angle_from_rv(rv, r_earth_sun)
        elev = _elevation_to_gs(rv, gs_coords)
        state["elevation_to_gs_deg"] = elev
        # Link margin: base 10 dB - path loss (5 dB at 90°, up to 20 dB at 0°)
        # - pointing penalty
        path_loss = 5.0 + 15.0 * max(0.0, 1.0 - math.sin(math.radians(elev + 1e-6)))
        pointing_penalty = 0.0
        if state.get("pointing_error_deg", 0.0) > 0.1:
            pointing_penalty = 2.0 * math.log10(1.0 + state["pointing_error_deg"])
        state["link_margin_db"] = 10.0 - path_loss - pointing_penalty
        if not com_window:
            state["link_margin_db"] = 0.0
        if not state.get("comms_enabled", True):
            state["link_margin_db"] = 0.0

        # Apply faults
        scheduler.apply(t_s, state, eps_params, thermal_params, adcs_params)

        # Run physics
        step_eps(state, dt_s=sim.delta_t.to_value("second"),
                 in_eclipse=eclipse, sun_angle_deg=sun_angle, params=eps_params)
        step_thermal(state, dt_s=sim.delta_t.to_value("second"),
                     in_eclipse=eclipse, sun_angle_deg=sun_angle,
                     payload_power_w=state.get("payload_power_w", 7.98),
                     params=thermal_params)
        step_adcs(state, dt_s=sim.delta_t.to_value("second"), params=adcs_params)

        # Append to history
        state["in_eclipse"] = 1.0 if eclipse else 0.0
        state_history.append(state)

    # Run CHESS (which fires our callback each step)
    data = sim.run(results_folder=os.path.join(output_dir, "chess_report"),
                   per_step_callback=callback)

    # Build per-step arrays
    per_step = build_per_step_arrays(state_history)

    # Write Telemanom channels
    os.makedirs(output_dir, exist_ok=True)
    channels = shape_channels(per_step, output_dir, split="test")

    return SimulationOutput(
        per_step_arrays=per_step,
        channels=channels,
        fault_log=[{
            "fault_id": r.fault_id,
            "fault_type": r.fault_type,
            "start_t": r.start_t,
            "end_t": r.end_t,
            "parameters": r.parameters,
            "applied_at": r.applied_at,
            "resolved_at": r.resolved_at,
        } for r in scheduler.history],
        run_metadata={
            "duration_s": sim.duration_sim.to_value("second"),
            "n_timesteps": sim.n_timesteps,
            "dt_s": sim.delta_t.to_value("second"),
            "output_dir": output_dir,
        },
        state_history=state_history,
    )
