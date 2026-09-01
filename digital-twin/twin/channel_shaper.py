"""
Channel shaper: convert per-step state arrays into Telemanom-shaped .npy files.

Each channel is a .npy file with shape (n_timesteps, n_features):
  - column 0: the channel's own value (predicted by Telemanom)
  - columns 1..N: same-channel context (eclipse flag, mode, etc.)

Channel IDs use SMAP/MSL prefix convention:
  P- = power, B- = battery, T- = thermal, A- = actuator/attitude,
  G- = gyro, D- = data/downlink

Values are pre-scaled to (-1, 1) using min/max of the test set, per
Telemanom's preprocessing requirement.
"""

from __future__ import annotations
from typing import Dict, Any
import os
import numpy as np


# Channel definitions: channel_id -> (value_field, context_fields)
CHANNEL_DEFINITIONS = {
    # Power
    "P-1": ("battery_voltage_v",     ["battery_soc", "battery_internal_r_ohm", "in_eclipse"]),
    "P-2": ("solar_input_w",         ["attitude_mode", "solar_input_multiplier", "in_eclipse"]),
    # Battery / thermal
    "B-1": ("battery_temp_c",        ["heater_duty_battery", "battery_soc", "battery_voltage_v"]),
    "T-1": ("payload_temp_c",        ["heater_duty_payload", "payload_power_w", "in_eclipse"]),
    "T-2": ("electronics_temp_c",    ["heater_duty_electronics", "payload_power_w", "in_eclipse"]),
    # ADCS
    "A-1": ("pointing_error_deg",    ["pointing_rate_deg_s", "attitude_mode", "star_tracker_ok"]),
    "G-1": ("wheel_speed_x_rpm",     ["wheel_speed_y_rpm", "wheel_speed_z_rpm", "attitude_mode"]),
    # Comms
    "D-1": ("link_margin_db",        ["elevation_to_gs_deg", "pointing_error_deg", "comms_enabled"]),
}


def _scale_to_unit(arr: np.ndarray) -> np.ndarray:
    """Min/max scale to (-1, 1). If arr is constant, return zeros.
    This matches Telemanom's documented preprocessing."""
    if arr.size == 0:
        return arr
    a_min = arr.min()
    a_max = arr.max()
    if a_max == a_min:
        return np.zeros_like(arr)
    return 2.0 * (arr - a_min) / (a_max - a_min) - 1.0


def shape_channels(per_step_arrays: Dict[str, np.ndarray], output_dir: str, split: str = "test") -> Dict[str, np.ndarray]:
    """Build and write per-channel .npy files from the per-step state arrays.

    Args:
        per_step_arrays: dict of 1D arrays (one per state field), each of length n_timesteps+1
        output_dir: directory to write the .npy files into
        split: "train" or "test" — written to <output_dir>/<split>/<channel>.npy

    Returns:
        dict of channel_id -> the (n_timesteps, n_features) array that was written
    """
    target_dir = os.path.join(output_dir, split)
    os.makedirs(target_dir, exist_ok=True)
    written = {}

    # Map attitude_mode index to a value Telemanom can use
    if "attitude_mode" in per_step_arrays:
        # Already converted to int code by build_per_step_arrays
        pass

    # Map star_tracker_ok, comms_enabled to floats (already done in build_per_step_arrays)

    # For P-2, we need the in_eclipse flag column. Make sure per_step_arrays has it
    if "in_eclipse" not in per_step_arrays:
        # Assume no eclipse if missing (CHESS may or may not produce it)
        n = len(next(iter(per_step_arrays.values())))
        per_step_arrays["in_eclipse"] = np.zeros(n, dtype=np.float64)

    for chan_id, (value_field, context_fields) in CHANNEL_DEFINITIONS.items():
        if value_field not in per_step_arrays:
            continue
        cols = [per_step_arrays[value_field]]
        for cf in context_fields:
            if cf in per_step_arrays:
                cols.append(per_step_arrays[cf])
            else:
                # If a context field is missing, pad with zeros
                cols.append(np.zeros_like(per_step_arrays[value_field]))
        # Stack into (n_timesteps, n_features)
        raw = np.stack(cols, axis=1)
        # Scale only the value column to (-1, 1); leave context in raw units
        # (Telemanom doesn't strictly require context to be scaled, but it
        # improves numerical stability; the original Telemanom scales all
        # columns independently per the example-combined.png shown in the
        # README, so let's do the same.)
        scaled = np.zeros_like(raw)
        for j in range(raw.shape[1]):
            scaled[:, j] = _scale_to_unit(raw[:, j])
        path = os.path.join(target_dir, f"{chan_id}.npy")
        np.save(path, scaled)
        written[chan_id] = scaled
    return written
