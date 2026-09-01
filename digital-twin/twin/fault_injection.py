"""
Fault injection for the digital twin.

Each fault name matches a Cause enum value in twin.procedures so the
runbook can verify "we suspected cause X, we injected it for real, the
recovery worked / didn't."

Faults are scheduled in advance (start_t, end_t, parameters). The
simulation loop calls FaultScheduler.active_at(t) each step to get the
list of faults that should be applied at time t, and applies them by
mutating the state dict.

Types of faults (each is a subclass of Fault below):
  - RampFault: linear ramp of a parameter from nominal to nominal+delta
  - StepFault: instant change to a parameter at start_t
  - StuckFault: holds a parameter at a fixed value for the duration
  - MaskFault: a logical flag (e.g., star_tracker_ok=False)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Type
import math


@dataclass
class FaultRecord:
    """Audit-trail record for one fault instance."""
    fault_id: str
    fault_type: str
    start_t: float
    end_t: Optional[float]
    parameters: Dict[str, Any]
    applied_at: Optional[float] = None
    resolved_at: Optional[float] = None


class Fault:
    """Base class for faults."""
    type_name: str = "base"

    def __init__(self, fault_id: str, start_t: float, end_t: Optional[float], parameters: Dict[str, Any]):
        self.fault_id = fault_id
        self.start_t = start_t
        self.end_t = end_t
        self.parameters = parameters

    def is_active(self, t: float) -> bool:
        if t < self.start_t:
            return False
        if self.end_t is not None and t >= self.end_t:
            return False
        return True

    def apply(self, t: float, state: Dict[str, Any], eps_params: Dict, thermal_params: Dict, adcs_params: Dict) -> None:
        """Mutate state in place. Subclasses override."""
        raise NotImplementedError


class RampFault(Fault):
    """Linear ramp of a parameter. parameters: {target_field, base_value, peak_value, ramp_field}
    The parameter ramps from base_value at start_t to peak_value at end_t.
    """
    type_name = "ramp"

    def apply(self, t: float, state, eps_params, thermal_params, adcs_params):
        if self.end_t is None:
            return
        ramp = (t - self.start_t) / max(1e-9, (self.end_t - self.start_t))
        ramp = max(0.0, min(1.0, ramp))
        target = self.parameters["target_field"]
        base = self.parameters["base_value"]
        peak = self.parameters["peak_value"]
        state[target] = base + (peak - base) * ramp


class StepFault(Fault):
    """Instant step at start_t. parameters: {target_field, value}"""
    type_name = "step"

    def apply(self, t: float, state, eps_params, thermal_params, adcs_params):
        if t < self.start_t:
            return
        target = self.parameters["target_field"]
        state[target] = self.parameters["value"]


class StuckFault(Fault):
    """Holds a parameter at a fixed value. parameters: {target_field, value}"""
    type_name = "stuck"

    def apply(self, t: float, state, eps_params, thermal_params, adcs_params):
        target = self.parameters["target_field"]
        state[target] = self.parameters["value"]


class MaskFault(Fault):
    """Logical flag. parameters: {target_field, value (bool)}"""
    type_name = "mask"

    def apply(self, t: float, state, eps_params, thermal_params, adcs_params):
        target = self.parameters["target_field"]
        state[target] = self.parameters["value"]


# Registry: cause_name -> Fault subclass + factory
FAULT_REGISTRY: Dict[str, Dict[str, Any]] = {
    "eps_internal_r_ramp": {
        "cls": RampFault,
        "params": {
            "target_field": "battery_internal_r_ohm",
            "base_value": 0.05,        # healthy cell
            "peak_value": "user_specified",  # caller fills via magnitude_ohm
        },
        "user_param": {"magnitude_ohm": "peak_value"},
        "default_duration_s": 1800.0,
    },
    "eps_load_step": {
        "cls": StepFault,
        "params": {
            "target_field": "load_a",
            "value": "user_specified",   # caller fills via extra_load_a
        },
        "user_param": {"extra_load_a": "value"},
        "default_duration_s": None,    # permanent unless end_t given
    },
    "thermal_heater_stuck_off": {
        "cls": StuckFault,
        "params": {
            "target_field": "heater_duty_<NODE>",   # template, replaced at inject time
            "value": 0.0,
        },
        "default_duration_s": None,
    },
    "thermal_heater_stuck_on": {
        "cls": StuckFault,
        "params": {
            "target_field": "heater_duty_<NODE>",
            "value": 1.0,
        },
        "default_duration_s": None,
    },
    "adcs_star_tracker_lost": {
        "cls": MaskFault,
        "params": {
            "target_field": "star_tracker_ok",
            "value": False,
        },
        "default_duration_s": None,
    },
    "solar_degradation": {
        "cls": StepFault,
        "params": {
            "target_field": "solar_input_multiplier",
            "value": "user_specified",   # caller fills via factor
        },
        "user_param": {"factor": "value"},
        "default_duration_s": None,
    },
    "comm_ground_station_lost": {
        "cls": MaskFault,
        "params": {
            "target_field": "comms_enabled",
            "value": False,
        },
        "default_duration_s": None,
    },
    # Cascading / multi-cause
    "thermal_runaway": {
        "cls": StepFault,
        "params": {
            "target_field": "_thermal_runaway_active",
            "value": True,
        },
        "default_duration_s": None,
    },
    "wheel_saturation": {
        "cls": StepFault,
        "params": {
            "target_field": "wheel_speed_x_rpm",
            "value": 6000.0,
        },
        "default_duration_s": None,
    },
}


class FaultScheduler:
    """Schedule and apply faults during a simulation run."""

    def __init__(self):
        self.scheduled: List[Fault] = []
        self.history: List[FaultRecord] = []
        self._next_id = 1

    def inject(
        self,
        fault_type: str,
        start_t: float,
        end_t: Optional[float] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Schedule a fault. Returns the fault_id (e.g., 'F-001').

        Each fault type has a "user_param" key in FAULT_REGISTRY that maps
        the friendly caller name to the internal field name. For example,
        eps_internal_r_ramp has user_param={"magnitude_ohm": "peak_value"}.
        Pass magnitude_ohm=0.4 and it gets stored as peak_value=0.4.
        """
        if fault_type not in FAULT_REGISTRY:
            raise ValueError(f"Unknown fault type: {fault_type}. Known: {list(FAULT_REGISTRY.keys())}")
        spec = FAULT_REGISTRY[fault_type]
        params = dict(spec["params"])  # start with template
        user_param_map = spec.get("user_param", {})  # e.g., {"magnitude_ohm": "peak_value"}
        if end_t is None:
            end_t = start_t + (spec.get("default_duration_s") or 3600.0)
        # Resolve user-specified values
        if parameters:
            for k, v in parameters.items():
                # If this user-facing key has a mapping, use the internal name
                internal_key = user_param_map.get(k, k)
                params[internal_key] = v
        # Template substitution for <NODE> placeholders
        node = parameters.get("node") if parameters else None
        if node and "<NODE>" in params.get("target_field", ""):
            params["target_field"] = params["target_field"].replace("<NODE>", node)
        # Validate: no remaining "user_specified" placeholders
        for k, v in params.items():
            if v == "user_specified":
                raise ValueError(
                    f"Missing required parameter for {fault_type}: "
                    f"the {k} field is required (set via parameters=)"
                )
        fault_id = f"F-{self._next_id:03d}"
        self._next_id += 1
        cls = spec["cls"]
        fault = cls(fault_id, start_t, end_t, params)
        self.scheduled.append(fault)
        self.history.append(FaultRecord(
            fault_id=fault_id,
            fault_type=fault_type,
            start_t=start_t,
            end_t=end_t,
            parameters=params,
        ))
        return fault_id

    def active_at(self, t: float) -> List[Fault]:
        return [f for f in self.scheduled if f.is_active(t)]

    def apply(self, t: float, state: Dict[str, Any], eps_params: Dict, thermal_params: Dict, adcs_params: Dict) -> None:
        """Apply all active faults at time t. Mutates state in place."""
        for fault in self.active_at(t):
            was_applied = fault.fault_id in [r.fault_id for r in self.history if r.applied_at is not None]
            fault.apply(t, state, eps_params, thermal_params, adcs_params)
            if not was_applied:
                # Mark as applied
                for rec in self.history:
                    if rec.fault_id == fault.fault_id and rec.applied_at is None:
                        rec.applied_at = t
                        break
        # Update fault_flags on state for the channel shaper
        state["_fault_flags"] = {
            f.fault_id: f.is_active(t) for f in self.scheduled
        }
