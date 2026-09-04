"""
Shared procedure registry for the digital twin / agent system.

This module is the SINGLE SOURCE OF TRUTH for:
  1. The Cause enum — what the diagnostic agent is allowed to diagnose.
     Each cause name matches a fault_type the FaultScheduler can inject,
     so the runbook can verify "we suspected X and we were right."
  2. The Procedure enum — what the diagnostic agent can recommend.
     Procedure names are independent of cause names.
  3. The ProcedureSpec — parameter schema, preconditions, postconditions,
     and apply_fn for each procedure. The twin imports this to know how
     to mutate state when validating a plan.

The diagnostic agent (your first layer) should:
  - Receive telemetry + the list of Cause values this module exposes
  - Output a ranked list of {cause, procedure, confidence} candidates
  - NEVER invent new cause or procedure names — only use the ones here
  - Validate parameters against each ProcedureSpec's params_schema before
    emitting the recommendation

The twin (digital_twin_CubeSat + our extensions) should:
  - Import Cause to interpret what the diagnostic agent said
  - Import Procedure + ProcedureSpec to know how to apply a recommended plan
  - Use apply_procedure() as the ONE PLACE that mutates state for a procedure

When you add a new cause: add it to the Cause enum AND add a fault_type
with the same name to FaultScheduler in twin/fault_injection.py (when that
file is built). When you add a new procedure: add it to the Procedure
enum, add a ProcedureSpec to PROCEDURE_REGISTRY, and implement the
apply_fn below.

DO NOT import this module from places that have circular dependencies
on the Spacecraft class — keep apply_procedure() pure (takes a state
dict, mutates it, returns None).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


# =============================================================================
# 1. CAUSE ENUM
# =============================================================================
# These are the diagnoses the diagnostic agent is allowed to emit.
# Each cause name MUST match a fault_type in twin/fault_injection.py so
# the runbook can verify "we suspected cause X, we injected it for real,
# the recovery worked / didn't."
#
# Coverage matrix (cause → expected symptoms → expected affected channels):
#   eps_internal_r_degradation: rising P-1 voltage sag, falling P-1 SoC, rising B-1 temp
#   eps_load_excess: falling P-1 SoC faster than eclipse predicts, stable P-1 voltage
#   thermal_heater_stuck_off: falling B-1 / T-1 / T-2 below setpoint
#   thermal_heater_stuck_on: rising B-1 / T-1 / T-2 above setpoint
#   adcs_star_tracker_lost: rising A-1 pointing error, rising G-1 wheel speeds, falling D-1 link margin
#   solar_degradation: falling P-2 solar current in sunlit periods
#   comm_ground_station_lost: D-1 link margin drops to 0 with no pointing change
#   battery_overdischarge: P-1 voltage at floor, SoC near 0
#   battery_undervoltage: P-1 voltage below safe threshold, SoC still nonzero
#   wheel_saturation: G-1 wheel speeds pinned at limit, A-1 error oscillating
#   thermal_runaway: rising B-1 / T-1 / T-2 faster than setpoint can track
#   sensor_noise: P-1 / T-* / A-1 channels with high-frequency oscillation around mean
#   no_fault_detected: telemetry within normal bounds

class Cause(str, Enum):
    """Diagnoses the diagnostic agent can emit. Each name matches a fault_type."""

    # EPS causes
    EPS_INTERNAL_R_DEGRADATION = "eps_internal_r_ramp"
    EPS_LOAD_EXCESS = "eps_load_step"
    BATTERY_OVERDISCHARGE = "battery_overdischarge"
    BATTERY_UNDERVOLTAGE = "battery_undervoltage"

    # Thermal causes
    THERMAL_HEATER_STUCK_OFF = "thermal_heater_stuck_off"
    THERMAL_HEATER_STUCK_ON = "thermal_heater_stuck_on"
    THERMAL_RUNAWAY = "thermal_runaway"

    # ADCS causes
    ADCS_STAR_TRACKER_LOST = "adcs_star_tracker_lost"
    WHEEL_SATURATION = "wheel_saturation"

    # Comms / power causes
    SOLAR_DEGRADATION = "solar_degradation"
    COMM_GROUND_STATION_LOST = "comm_ground_station_lost"

    # Diagnostic / housekeeping
    SENSOR_NOISE = "sensor_noise"
    NO_FAULT_DETECTED = "no_fault_detected"

    def affected_subsystems(self) -> List[str]:
        """Which subsystems the diagnostic agent should expect to see in
        Telemanom's anomaly list when this cause is active. Used by the
        diagnostic layer to cross-check the channel-level evidence."""
        mapping = {
            Cause.EPS_INTERNAL_R_DEGRADATION: ["eps", "thermal"],  # couples to battery temp
            Cause.EPS_LOAD_EXCESS: ["eps"],
            Cause.BATTERY_OVERDISCHARGE: ["eps"],
            Cause.BATTERY_UNDERVOLTAGE: ["eps"],
            Cause.THERMAL_HEATER_STUCK_OFF: ["thermal"],
            Cause.THERMAL_HEATER_STUCK_ON: ["thermal"],
            Cause.THERMAL_RUNAWAY: ["thermal", "eps"],  # thermal runaway affects battery
            Cause.ADCS_STAR_TRACKER_LOST: ["adcs", "comms"],
            Cause.WHEEL_SATURATION: ["adcs"],
            Cause.SOLAR_DEGRADATION: ["eps"],
            Cause.COMM_GROUND_STATION_LOST: ["comms"],
            Cause.SENSOR_NOISE: ["eps", "thermal", "adcs"],  # can appear anywhere
            Cause.NO_FAULT_DETECTED: [],
        }
        return mapping[self]

    def expected_channels(self) -> List[str]:
        """Channel IDs (Telemanom convention) likely to show anomalies
        when this cause is active. Diagnostic agent should look for these
        in the Telemanom E_seq output to confirm or rule out the cause."""
        mapping = {
            Cause.EPS_INTERNAL_R_DEGRADATION: ["P-1", "B-1"],
            Cause.EPS_LOAD_EXCESS: ["P-1"],
            Cause.BATTERY_OVERDISCHARGE: ["P-1"],
            Cause.BATTERY_UNDERVOLTAGE: ["P-1"],
            Cause.THERMAL_HEATER_STUCK_OFF: ["B-1", "T-1", "T-2"],
            Cause.THERMAL_HEATER_STUCK_ON: ["B-1", "T-1", "T-2"],
            Cause.THERMAL_RUNAWAY: ["B-1", "T-1", "T-2", "P-1"],
            Cause.ADCS_STAR_TRACKER_LOST: ["A-1", "G-1", "D-1"],
            Cause.WHEEL_SATURATION: ["G-1", "A-1"],
            Cause.SOLAR_DEGRADATION: ["P-2"],
            Cause.COMM_GROUND_STATION_LOST: ["D-1"],
            Cause.SENSOR_NOISE: [],  # can be any channel
            Cause.NO_FAULT_DETECTED: [],
        }
        return mapping[self]


# =============================================================================
# 2. PROCEDURE ENUM
# =============================================================================
# These are the actions the diagnostic agent can recommend.
# Procedure names are independent of cause names. The diagnostic agent's
# ranking logic decides which procedure to recommend for a given cause.
#
# Coverage matrix (cause → candidate procedures the diagnostic agent should
# consider ranking — NOT a hard mapping; the agent decides which to apply):
#   eps_internal_r_degradation:    [EPS_SHED_LOAD, EPS_PRIORITIZE_CHARGING, MODE_CHANGE_TO_SAFE]
#   eps_load_excess:               [EPS_SHED_LOAD, MODE_CHANGE_TO_SAFE]
#   battery_overdischarge:         [EPS_SHED_LOAD, MODE_CHANGE_TO_SAFE]
#   battery_undervoltage:          [EPS_SHED_LOAD, WAIT]
#   thermal_heater_stuck_off:      [THERMAL_ENABLE_BACKUP_HEATER, THERMAL_THROTTLE_PAYLOAD]
#   thermal_heater_stuck_on:       [THERMAL_THROTTLE_PAYLOAD]
#   thermal_runaway:               [THERMAL_THROTTLE_PAYLOAD, MODE_CHANGE_TO_SAFE]
#   adcs_star_tracker_lost:        [ADCS_SAFE_HOLD, ADCS_RESET_STAR_TRACKER]
#   wheel_saturation:              [ADCS_SAFE_HOLD]
#   solar_degradation:             [EPS_PRIORITIZE_CHARGING, MODE_CHANGE_TO_SAFE]
#   comm_ground_station_lost:      [COMMS_POSTPONE_DOWNLINK]
#   sensor_noise:                  [WAIT]  # likely false positive, observe more
#   no_fault_detected:             [WAIT]

class Procedure(str, Enum):
    """Actions the diagnostic agent can recommend."""

    # EPS recovery
    EPS_SHED_LOAD = "eps_shed_non_essential_load"
    EPS_PRIORITIZE_CHARGING = "eps_increase_charging_priority"

    # Thermal recovery
    THERMAL_ENABLE_BACKUP_HEATER = "thermal_enable_heater_backup"
    THERMAL_THROTTLE_PAYLOAD = "thermal_throttle_payload"

    # ADCS recovery
    ADCS_SAFE_HOLD = "adcs_switch_to_safe_hold"
    ADCS_RESET_STAR_TRACKER = "adcs_reset_star_tracker"

    # Comms recovery
    COMMS_POSTPONE_DOWNLINK = "comms_postpone_downlink"

    # Mode / safety
    MODE_CHANGE_TO_SAFE = "mode_change_to_safe"

    # No-op (observe more)
    WAIT = "wait"


# =============================================================================
# 3. PARAMETER SCHEMA
# =============================================================================

@dataclass(frozen=True)
class ParamSpec:
    """Schema for a single parameter of a procedure."""
    type: type                # one of: int, float, str, bool
    min: Optional[float] = None
    max: Optional[float] = None
    allowed_values: Optional[tuple] = None
    unit: Optional[str] = None
    required: bool = True
    description: str = ""

    def validate(self, value: Any) -> None:
        """Raises ValueError if value doesn't match the spec."""
        if value is None:
            if self.required:
                raise ValueError(f"Required parameter missing")
            return
        if not isinstance(value, self.type):
            # accept ints where floats expected
            if self.type is float and isinstance(value, int):
                return
            raise ValueError(
                f"Expected type {self.type.__name__}, got {type(value).__name__}"
            )
        if self.min is not None and value < self.min:
            raise ValueError(f"Value {value} < min {self.min}")
        if self.max is not None and value > self.max:
            raise ValueError(f"Value {value} > max {self.max}")
        if self.allowed_values is not None and value not in self.allowed_values:
            raise ValueError(
                f"Value {value} not in allowed values {self.allowed_values}"
            )


# =============================================================================
# 4. PROCEDURE SPEC + REGISTRY
# =============================================================================

# Allowed values for the new catalog-level editorial fields. The
# import-time validation at the bottom of this file raises on any
# entry that uses a value outside these sets.
VALID_MISSION_IMPACTS: tuple = ("none", "minor", "major", "mission-ending")
VALID_REVERSIBILITY: tuple = ("trivial", "easy", "hard")


def _validate_catalog_field(proc_name: str, field_name: str, value: Any, allowed: tuple) -> None:
    """Raise ValueError if value is not in the allowed set. Used at
    import time to keep the registry self-consistent."""
    if value not in allowed:
        raise ValueError(
            f"Procedure {proc_name}: {field_name}={value!r} not in {allowed}"
        )


@dataclass
class ProcedureSpec:
    """Full specification of a procedure: what params it takes, what must
    be true to apply it, what should be true after, and how to apply it."""
    name: Procedure
    description: str
    params_schema: Dict[str, ParamSpec] = field(default_factory=dict)
    preconditions: List[Callable[[dict], bool]] = field(default_factory=list)
    postconditions: List[Callable[[dict, dict], bool]] = field(default_factory=list)
    apply_fn: Optional[Callable[[dict, dict], None]] = None
    risk_class: str = "low"  # one of: "low", "medium", "high", "critical"
    # Approval routing: who must approve this procedure
    # "auto" = no human needed, "operator" = on-call engineer, "director" = mission director
    approval_required: str = "operator"
    # Phase 1: default parameters Propose uses when validating this
    # procedure. Chosen as the safe middle of params_schema bounds so
    # the demo ranks meaningful outcomes without per-cause tuning.
    # Per-cause overrides (T2 in the plan) deferred until the demo
    # shows the cause-agnostic defaults are insufficient.
    default_params: Optional[Dict[str, Any]] = None
    # Catalog-level editorial signals used by the Propose pre-filter
    # and surfaced to the frontend for the candidates table demo.
    # All three are STATIC (hand-authored, go through PR review) and
    # orthogonal to risk_class and risk_score.
    #
    #   effort_score: 0..1, lower = less operator/spacecraft work
    #     (e.g., wait=0.05, mode_change_to_safe=0.85).
    #   mission_impact: how much mission capability is lost while
    #     this procedure is in effect. Independent of risk (a
    #     low-risk procedure can have major mission impact if it's
    #     the only way to recover).
    #   reversibility: how easy it is to undo the procedure's effect
    #     once started. "trivial" = instant rollback, "easy" = stop
    #     the procedure and the spacecraft returns to nominal
    #     within a step or two, "hard" = the procedure's effect
    #     persists and recovery requires another procedure.
    effort_score: float = 0.5
    mission_impact: str = "minor"
    reversibility: str = "easy"

    def validate_params(self, params: dict) -> None:
        """Raises ValueError on first invalid parameter."""
        for name, spec in self.params_schema.items():
            if name in params:
                spec.validate(params[name])
            elif spec.required:
                raise ValueError(f"Required parameter '{name}' missing")
        for name in params:
            if name not in self.params_schema:
                raise ValueError(f"Unknown parameter '{name}'")

    def check_preconditions(self, state: dict) -> bool:
        return all(fn(state) for fn in self.preconditions)

    def check_postconditions(self, state_predicted: dict, state_actual: dict) -> bool:
        """Used by the verification step: did the actual outcome match the
        predicted outcome well enough to call this verified?"""
        return all(fn(state_predicted, state_actual) for fn in self.postconditions)


# =============================================================================
# APPLY FUNCTIONS
# =============================================================================
# These are the ONE PLACE that mutates twin state for a given procedure.
# They take a state dict and a params dict, mutate state, and return None.
#
# The state dict shape is what twin/state.py will produce (a flat dict
# of all sim state at a given timestep). For now, document the keys
# each function reads/writes.

# State dict shape (canonical, also used by twin/state.py):
#   {
#     # EPS
#     "battery_soc": float,              # 0..1
#     "battery_voltage_v": float,
#     "load_a": float,                    # current total load in amps
#     "solar_input_w": float,
#     # Thermal
#     "battery_temp_c": float,
#     "payload_temp_c": float,
#     "electronics_temp_c": float,
#     "heater_duty_battery": float,      # 0..1
#     "heater_duty_payload": float,
#     "heater_duty_electronics": float,
#     "payload_power_w": float,
#     # ADCS
#     "pointing_error_deg": float,
#     "wheel_speed_rpm": tuple,          # (wx, wy, wz)
#     "attitude_mode": str,              # "nadir" | "sun" | "ground" | "safe_hold"
#     "star_tracker_ok": bool,
#     # Comms
#     "link_margin_db": float,
#     "comms_enabled": bool,
#     # Mode
#     "operating_mode": int,             # 0=IDLE, 1=SAFE, 2=CHARGING, 3=UHF, 4=X_BAND, 5=MEAS
#     "fault_flags": dict,               # fault_id -> True/False
#     # Bookkeeping
#     "t_s": float,                      # current sim time
#   }

def _apply_eps_shed_load(state: dict, params: dict) -> None:
    """Reduce total load by params['load_reduction_a'] amps for params['duration_s'] seconds."""
    state["load_a"] = state.get("load_a", 3.0) - params["load_reduction_a"]
    state["load_a_shed_until_t"] = state["t_s"] + params["duration_s"]
    # Heuristic: shedding load frees power for thermal, so nudge heater duty up
    # to use the freed capacity. Not physical-perfect but a reasonable coupling.
    state["heater_duty_battery"] = min(1.0, state.get("heater_duty_battery", 0.5) + 0.1)


def _apply_eps_prioritize_charging(state: dict, params: dict) -> None:
    """Switch to sun-pointing attitude and shed non-essential load. Effect:
    - attitude_mode -> "sun"
    - load_a reduced by params['load_reduction_a'] for params['duration_s']"""
    state["attitude_mode"] = "sun"
    state["load_a"] = state.get("load_a", 3.0) - params["load_reduction_a"]
    state["load_a_shed_until_t"] = state["t_s"] + params["duration_s"]
    state["solar_input_multiplier"] = params.get("solar_input_multiplier", 1.0)


def _apply_thermal_enable_backup_heater(state: dict, params: dict) -> None:
    """Turn on the backup heater for the named node. params['node'] in
    {battery, payload, electronics}."""
    node = params["node"]
    key = f"heater_duty_{node}"
    state[key] = 1.0
    state[f"backup_heater_active_{node}"] = True


def _apply_thermal_throttle_payload(state: dict, params: dict) -> None:
    """Reduce payload power to params['payload_power_w'] for params['duration_s'] seconds.
    Effectively reduces heat dissipation from payload electronics."""
    state["payload_power_w"] = params["payload_power_w"]
    state["payload_throttle_until_t"] = state["t_s"] + params["duration_s"]


def _apply_adcs_safe_hold(state: dict, params: dict) -> None:
    """Switch to safe-hold attitude (sun-pointing, no active pointing).
    Frees solar input, deactivates fine pointing control."""
    state["attitude_mode"] = "safe_hold"
    # Safe hold implies we're not trying to point at ground station
    state["link_margin_db"] = max(0.0, state.get("link_margin_db", 0.0) - 3.0)
    state["pointing_drift_rate_deg_s"] = state.get("pointing_drift_rate_deg_s", 0.0)


def _apply_adcs_reset_star_tracker(state: dict, params: dict) -> None:
    """Power-cycle the star tracker estimator. params['hold_off_s'] is how
    long to disable it before re-enabling."""
    state["star_tracker_ok"] = False
    state["star_tracker_re_enable_t"] = state["t_s"] + params["hold_off_s"]
    # During reset, pointing error may drift (controller has no attitude reference)
    state["pointing_drift_rate_deg_s"] = 0.05  # small drift during reset


def _apply_comms_postpone_downlink(state: dict, params: dict) -> None:
    """Defer X-band downlink for params['postpone_s'] seconds."""
    state["comms_enabled"] = False
    state["comms_re_enable_t"] = state["t_s"] + params["postpone_s"]


def _apply_mode_change_to_safe(state: dict, params: dict) -> None:
    """Enter safe mode. Lowers power consumption, switches to sun-pointing,
    disables non-essential subsystems."""
    state["operating_mode"] = 1  # CHESS SAFE mode
    state["attitude_mode"] = "sun"
    state["load_a"] = state.get("load_a", 3.0) * 0.5  # safe mode uses ~50% power
    state["payload_power_w"] = 0.0
    state["heater_duty_payload"] = 0.0
    state["heater_duty_electronics"] = state.get("heater_duty_electronics", 0.5)
    state["heater_duty_battery"] = min(1.0, state.get("heater_duty_battery", 0.5) + 0.2)
    state["comms_enabled"] = True  # safe mode keeps UHF for beacon


def _apply_wait(state: dict, params: dict) -> None:
    """Do nothing for params['duration_s'] seconds. Use when the diagnostic
    agent suspects a transient / false positive and wants to gather more data."""
    state["wait_until_t"] = state["t_s"] + params["duration_s"]


# =============================================================================
# 5. THE REGISTRY
# =============================================================================

PROCEDURE_REGISTRY: Dict[Procedure, ProcedureSpec] = {

    Procedure.EPS_SHED_LOAD: ProcedureSpec(
        name=Procedure.EPS_SHED_LOAD,
        description=(
            "Reduce total spacecraft load by shedding non-essential consumers. "
            "Frees power for thermal survival and battery recovery. "
            "Use for: battery undervoltage, internal_r degradation, load excess."
        ),
        params_schema={
            "load_reduction_a": ParamSpec(
                float, min=0.1, max=5.0, unit="A", required=True,
                description="Amps to shed from the load bus. 0.1-5.0A typical for smallsat.",
            ),
            "duration_s": ParamSpec(
                float, min=60, max=14400, unit="s", required=True,
                description="How long the load reduction stays in effect.",
            ),
        },
        preconditions=[
            lambda s: s.get("operating_mode") != 1,
        ],
        postconditions=[
            # Actual battery_soc should be ≥ predicted_soc - 1% (recovery happened
            # or stayed flat; we don't expect it to get worse)
            lambda s_pred, s_actual: s_actual["battery_soc"] >= s_pred["battery_soc"] - 0.01,
        ],
        apply_fn=_apply_eps_shed_load,
        risk_class="low",
        approval_required="operator",
        default_params={"load_reduction_a": 1.0, "duration_s": 3600.0},
        effort_score=0.30,
        mission_impact="minor",
        reversibility="easy",
    ),

    Procedure.EPS_PRIORITIZE_CHARGING: ProcedureSpec(
        name=Procedure.EPS_PRIORITIZE_CHARGING,
        description=(
            "Switch to sun-pointing attitude + shed non-essential load. "
            "Maximizes solar input while reducing consumption. "
            "Use for: solar degradation, internal_r degradation, persistent undervoltage."
        ),
        params_schema={
            "load_reduction_a": ParamSpec(
                float, min=0.1, max=5.0, unit="A", required=True,
                description="Amps to shed from the load bus.",
            ),
            "duration_s": ParamSpec(
                float, min=60, max=14400, unit="s", required=True,
                description="How long the prioritized charging stays in effect.",
            ),
            "solar_input_multiplier": ParamSpec(
                float, min=0.5, max=1.0, unit="ratio", required=False,
                description="Optional multiplier on solar input (e.g., 0.8 for partial degradation).",
            ),
        },
        preconditions=[
            lambda s: s.get("operating_mode") != 1,
        ],
        postconditions=[
            lambda s_pred, s_actual: s_actual["solar_input_w"] >= s_pred["solar_input_w"] * 0.9,
        ],
        apply_fn=_apply_eps_prioritize_charging,
        risk_class="low",
        approval_required="operator",
        default_params={
            "load_reduction_a": 1.0,
            "duration_s": 3600.0,
            "solar_input_multiplier": 1.0,
        },
        effort_score=0.35,
        mission_impact="minor",
        reversibility="easy",
    ),

    Procedure.THERMAL_ENABLE_BACKUP_HEATER: ProcedureSpec(
        name=Procedure.THERMAL_ENABLE_BACKUP_HEATER,
        description=(
            "Activate the backup heater for a thermal node. Use when the primary "
            "heater is stuck off or the node is drifting below its survival floor."
        ),
        params_schema={
            "node": ParamSpec(
                str, allowed_values=("battery", "payload", "electronics"), required=True,
                description="Which thermal node gets the backup heater.",
            ),
        },
        preconditions=[
            lambda s: s.get("operating_mode") != 1,  # don't override safe mode
        ],
        postconditions=[
            # Heater duty for the node should be at full after the procedure
            lambda s_pred, s_actual: s_actual.get(f"heater_duty_{s_pred.get('_procedure_node', 'battery')}", 0) >= 0.99,
        ],
        apply_fn=_apply_thermal_enable_backup_heater,
        risk_class="low",
        approval_required="operator",
        default_params={"node": "battery"},
        effort_score=0.15,
        mission_impact="none",
        reversibility="trivial",
    ),

    Procedure.THERMAL_THROTTLE_PAYLOAD: ProcedureSpec(
        name=Procedure.THERMAL_THROTTLE_PAYLOAD,
        description=(
            "Reduce payload power consumption to lower heat dissipation. "
            "Use for: thermal runaway, heater stuck on, electronics overtemperature."
        ),
        params_schema={
            "payload_power_w": ParamSpec(
                float, min=0.0, max=15.0, unit="W", required=True,
                description="New payload power setpoint. 0 = disable payload entirely.",
            ),
            "duration_s": ParamSpec(
                float, min=60, max=14400, unit="s", required=True,
                description="How long the throttle stays in effect.",
            ),
        },
        preconditions=[],
        postconditions=[
            # Electronics temp should be ≤ predicted (cooler or equal)
            lambda s_pred, s_actual: s_actual["electronics_temp_c"] <= s_pred["electronics_temp_c"] + 0.5,
        ],
        apply_fn=_apply_thermal_throttle_payload,
        risk_class="medium",  # payload off = mission impact
        approval_required="operator",
        default_params={"payload_power_w": 0.0, "duration_s": 3600.0},
        effort_score=0.40,
        mission_impact="major",
        reversibility="easy",
    ),

    Procedure.ADCS_SAFE_HOLD: ProcedureSpec(
        name=Procedure.ADCS_SAFE_HOLD,
        description=(
            "Switch to safe-hold attitude (sun-pointing, no active fine pointing). "
            "Use for: star tracker lost, wheel saturation, persistent pointing error."
        ),
        params_schema={},
        preconditions=[],
        postconditions=[
            # Pointing error should stabilize (not grow unboundedly)
            lambda s_pred, s_actual: s_actual["pointing_error_deg"] < 5.0,
        ],
        apply_fn=_apply_adcs_safe_hold,
        risk_class="medium",  # lose fine pointing, comms may suffer
        approval_required="operator",
        default_params={},
        effort_score=0.60,
        mission_impact="major",
        reversibility="hard",
    ),

    Procedure.ADCS_RESET_STAR_TRACKER: ProcedureSpec(
        name=Procedure.ADCS_RESET_STAR_TRACKER,
        description=(
            "Power-cycle the star tracker estimator. May briefly worsen pointing "
            "while it re-acquires attitude reference. Use for: transient star tracker faults."
        ),
        params_schema={
            "hold_off_s": ParamSpec(
                float, min=5, max=300, unit="s", required=True,
                description="How long to keep the star tracker off before re-enabling.",
            ),
        },
        preconditions=[],
        postconditions=[
            # After hold-off, star_tracker_ok should be true again
            lambda s_pred, s_actual: s_actual.get("star_tracker_ok", False),
        ],
        apply_fn=_apply_adcs_reset_star_tracker,
        risk_class="medium",  # brief pointing degradation
        approval_required="operator",
        default_params={"hold_off_s": 30.0},
        effort_score=0.45,
        mission_impact="minor",
        reversibility="easy",
    ),

    Procedure.COMMS_POSTPONE_DOWNLINK: ProcedureSpec(
        name=Procedure.COMMS_POSTPONE_DOWNLINK,
        description=(
            "Defer X-band downlink pass. Use when a ground station is unavailable "
            "or comm power draw is competing with thermal/EPS recovery."
        ),
        params_schema={
            "postpone_s": ParamSpec(
                float, min=300, max=86400, unit="s", required=True,
                description="How long to defer the downlink.",
            ),
        },
        preconditions=[],
        postconditions=[
            lambda s_pred, s_actual: not s_actual.get("comms_enabled", True) or s_actual.get("t_s", 0) > s_pred.get("comms_re_enable_t", float("inf")),
        ],
        apply_fn=_apply_comms_postpone_downlink,
        risk_class="low",
        approval_required="auto",  # routine, can auto-approve
        default_params={"postpone_s": 3600.0},
        effort_score=0.10,
        mission_impact="minor",
        reversibility="trivial",
    ),

    Procedure.MODE_CHANGE_TO_SAFE: ProcedureSpec(
        name=Procedure.MODE_CHANGE_TO_SAFE,
        description=(
            "Enter spacecraft safe mode. Lowers power, sun-points, disables "
            "payload. Last-resort recovery for severe anomalies."
        ),
        params_schema={},
        preconditions=[],
        postconditions=[
            # Operating mode should be SAFE (1)
            lambda s_pred, s_actual: s_actual.get("operating_mode") == 1,
        ],
        apply_fn=_apply_mode_change_to_safe,
        risk_class="critical",  # mission-impacting, last resort
        approval_required="director",  # mission director must approve
        default_params={},
        effort_score=0.85,
        mission_impact="mission-ending",
        reversibility="hard",
    ),

    Procedure.WAIT: ProcedureSpec(
        name=Procedure.WAIT,
        description=(
            "Do nothing for a specified duration. Use when the diagnostic agent "
            "is uncertain (e.g., sensor noise) and wants more data before acting."
        ),
        params_schema={
            "duration_s": ParamSpec(
                float, min=60, max=3600, unit="s", required=True,
                description="How long to observe before reconsidering.",
            ),
        },
        preconditions=[],
        postconditions=[],
        apply_fn=_apply_wait,
        risk_class="low",
        approval_required="auto",
        default_params={"duration_s": 600.0},
        effort_score=0.05,
        mission_impact="none",
        reversibility="trivial",
    ),

}


# Import-time validation: every entry's new catalog fields must be
# in the allowed sets. Raises on first bad value.
for _proc, _spec in PROCEDURE_REGISTRY.items():
    _validate_catalog_field(_proc.value, "mission_impact", _spec.mission_impact, VALID_MISSION_IMPACTS)
    _validate_catalog_field(_proc.value, "reversibility", _spec.reversibility, VALID_REVERSIBILITY)
    if not (0.0 <= _spec.effort_score <= 1.0):
        raise ValueError(
            f"Procedure {_proc.value}: effort_score={_spec.effort_score!r} not in [0.0, 1.0]"
        )


def get_default_params(procedure: Procedure) -> Dict[str, Any]:
    """Return the procedure's default parameters (Phase 1).

    Used by Propose when validating a candidate procedure without
    cause-specific tuning. Returns a fresh dict each call so callers
    can mutate without affecting the registry. If the procedure has
    no default_params (theoretical; every entry in PROCEDURE_REGISTRY
    has one as of this change), returns an empty dict.
    """
    spec = PROCEDURE_REGISTRY[procedure]
    return dict(spec.default_params or {})


def apply_procedure(state: dict, procedure: Procedure, params: dict) -> None:
    """The ONE place that mutates twin state for a procedure.

    The diagnostic agent calls this (via the validation layer) to simulate
    what would happen if a recommended procedure were applied. The simulation
    loop also calls this when an approved procedure is actually executed.

    Args:
        state: the current sim state dict (see schema at top of file)
        procedure: one of the Procedure enum values
        params: validated parameter dict for this procedure

    Raises:
        ValueError: if procedure is unknown, or if params don't match schema,
                    or if preconditions aren't met.
    """
    if procedure not in PROCEDURE_REGISTRY:
        raise ValueError(f"Unknown procedure: {procedure}")
    spec = PROCEDURE_REGISTRY[procedure]
    spec.validate_params(params)
    if not spec.check_preconditions(state):
        raise ValueError(f"Preconditions not met for {procedure.value}")
    # Stash the procedure name + params on the state so postconditions can
    # reference which node was targeted (for the heater-enable case)
    state["_last_procedure"] = procedure
    state["_last_procedure_params"] = params
    if spec.apply_fn is not None:
        spec.apply_fn(state, params)


def get_candidate_procedures(cause: Cause) -> List[Procedure]:
    """Return the procedures the diagnostic agent should consider for a given
    cause. This is the suggested candidate set — the agent's ranking logic
    decides which to actually recommend, but it should only choose from this list."""
    mapping = {
        Cause.EPS_INTERNAL_R_DEGRADATION: [
            Procedure.EPS_SHED_LOAD,
            Procedure.EPS_PRIORITIZE_CHARGING,
            Procedure.MODE_CHANGE_TO_SAFE,
        ],
        Cause.EPS_LOAD_EXCESS: [
            Procedure.EPS_SHED_LOAD,
            Procedure.MODE_CHANGE_TO_SAFE,
        ],
        Cause.BATTERY_OVERDISCHARGE: [
            Procedure.EPS_SHED_LOAD,
            Procedure.MODE_CHANGE_TO_SAFE,
        ],
        Cause.BATTERY_UNDERVOLTAGE: [
            Procedure.EPS_SHED_LOAD,
            Procedure.WAIT,
        ],
        Cause.THERMAL_HEATER_STUCK_OFF: [
            Procedure.THERMAL_ENABLE_BACKUP_HEATER,
            Procedure.THERMAL_THROTTLE_PAYLOAD,
        ],
        Cause.THERMAL_HEATER_STUCK_ON: [
            Procedure.THERMAL_THROTTLE_PAYLOAD,
        ],
        Cause.THERMAL_RUNAWAY: [
            Procedure.THERMAL_THROTTLE_PAYLOAD,
            Procedure.MODE_CHANGE_TO_SAFE,
        ],
        Cause.ADCS_STAR_TRACKER_LOST: [
            Procedure.ADCS_SAFE_HOLD,
            Procedure.ADCS_RESET_STAR_TRACKER,
        ],
        Cause.WHEEL_SATURATION: [
            Procedure.ADCS_SAFE_HOLD,
        ],
        Cause.SOLAR_DEGRADATION: [
            Procedure.EPS_PRIORITIZE_CHARGING,
            Procedure.MODE_CHANGE_TO_SAFE,
        ],
        Cause.COMM_GROUND_STATION_LOST: [
            Procedure.COMMS_POSTPONE_DOWNLINK,
        ],
        Cause.SENSOR_NOISE: [
            Procedure.WAIT,
        ],
        Cause.NO_FAULT_DETECTED: [
            Procedure.WAIT,
        ],
    }
    return mapping.get(cause, [])
