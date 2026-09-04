"""
Bridge between the live/ streaming detector and the digital-twin/twin/
sidecar simulator.

This module is the one place in live/ that imports from twin/. It
exists as a separate file so:
  - live/ws_server.py stays pure-Python (no astropy / CHESS deps)
  - the live test suite can run on the system Python without the
    CHESS venv
  - the integration test exercises only this module's surface

The bridge is the seam the BIBLE §14 calls for. It translates:
  - HTTP /inject requests into twin FaultScheduler entries
  - AlertEvents from the LSTM detector into SymptomEvents for Diagnose
  - the (Cause, state) tuple into a Proposal via Propose
  - the Proposal's Verdict into a JSON-serializable dict for /stream

All heavy imports are deferred (inside the function) so that
importing this module does NOT pull in the CHESS astropy chain.
That keeps the live server fast to start and lets the live tests
run in any environment.
"""

from __future__ import annotations
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("live.bridge")

# Map (kind, channel) -> (twin fault_type, default params for the fault).
# This is the seam the BIBLE §2 / §14 leaves undefined: how an HTTP
# injection (the live/ vocabulary) becomes a twin fault (the twin's
# vocabulary). The mapping is a fixed table in Phase 1; per-cause or
# per-channel overrides are a Phase 2+ concern.
#
# Channels follow the SMAP/MSL prefix convention used by the twin's
# channel shaper (BIBLE §6.1): P-*, B-*, T-*, A-*, G-*, D-*.
INJECTION_TO_FAULT: Dict[Tuple[str, Optional[str]], Tuple[str, Dict[str, Any]]] = {
    # Legacy 3-kind interface (existing live/ API, kept for backward compat).
    # When the caller does NOT specify a channel, we pick a sensible default.
    ("spike",   None): ("eps_internal_r_ramp",   {"magnitude_ohm": 0.4}),
    ("shift",   None): ("eps_internal_r_ramp",   {"magnitude_ohm": 0.2}),
    ("dropout", None): ("adcs_star_tracker_lost", {}),

    # Channel-specific mappings. These are the demo helpers — picking
    # a channel narrows the fault to a specific subsystem so the
    # dashboard can show "I injected a T-1 fault" and the operator
    # sees the corresponding Diagnose -> Propose -> Verdict path.
    ("spike",   "P-1"): ("eps_load_step",          {"extra_load_a": 1.5}),
    ("spike",   "P-2"): ("solar_degradation",      {"factor": 0.5}),
    ("shift",   "P-1"): ("eps_internal_r_ramp",    {"magnitude_ohm": 0.4}),
    ("shift",   "P-2"): ("solar_degradation",      {"factor": 0.5}),
    ("shift",   "B-1"): ("thermal_heater_stuck_off", {"node": "battery"}),
    ("shift",   "T-1"): ("thermal_heater_stuck_on",  {"node": "payload"}),
    ("shift",   "T-2"): ("thermal_heater_stuck_on",  {"node": "electronics"}),
    ("dropout", "A-1"): ("adcs_star_tracker_lost", {}),
    ("dropout", "G-1"): ("wheel_saturation",       {}),
    ("dropout", "D-1"): ("comm_ground_station_lost", {}),
}


# Channel -> subsystem mapping. Used to build SymptomEvents for
# Diagnose. Mirrors BIBLE §6.1.
CHANNEL_TO_SUBSYSTEM: Dict[str, str] = {
    "P-1": "eps", "P-2": "eps",
    "B-1": "thermal",   # shared between Battery and Thermal per BIBLE
    "T-1": "thermal", "T-2": "thermal",
    "A-1": "adcs", "G-1": "adcs",
    "D-1": "comms",
}


# How long the injected fault stays active by default. Long enough
# for the demo to see it (the twin's run loop is in seconds; the
# live stream is in 200ms ticks), short enough to terminate.
DEFAULT_FAULT_DURATION_S: float = 1200.0  # 20 minutes


def _ensure_twin_on_path() -> None:
    """Add the digital-twin directory to sys.path if it isn't there.

    Done at the top of every twin-using function so that:
      - import time of this module is unchanged
      - the live server can boot without the CHESS venv
      - tests can use a different twin path
    """
    # The twin lives at <repo>/digital-twin/ (sibling of live/).
    here = os.path.dirname(os.path.abspath(__file__))
    twin_root = os.path.normpath(os.path.join(here, "..", "digital-twin"))
    if twin_root not in sys.path:
        sys.path.insert(0, twin_root)


def map_injection_to_fault(
    kind: str,
    magnitude: float,
    channel: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], float]:
    """Translate an HTTP injection into a (fault_type, params, end_t) tuple.

    Args:
        kind: "spike" | "shift" | "dropout" (the live/ vocabulary)
        magnitude: from the query param (used to scale fault params)
        channel: optional channel ID ("P-1", "T-1", etc.) for the
            channel-specific mapping. If None, uses the kind-only
            default mapping.

    Returns:
        (fault_type, fault_params, end_t_offset_s) ready to pass to
        FaultScheduler.inject(). end_t_offset_s is added to the
        current sim time; the fault auto-terminates then.

    Raises:
        ValueError if kind is unknown.
    """
    key = (kind, channel)
    if key not in INJECTION_TO_FAULT:
        raise ValueError(
            f"No injection mapping for kind={kind!r} channel={channel!r}. "
            f"Known: {list(INJECTION_TO_FAULT.keys())}"
        )
    fault_type, base_params = INJECTION_TO_FAULT[key]
    # Scale magnitude into the param. For each known fault type, we
    # adjust the param that uses magnitude so the demo can show a
    # range of severities without code changes.
    params = dict(base_params)
    if fault_type == "eps_internal_r_ramp" and "magnitude_ohm" in params:
        params["magnitude_ohm"] = magnitude * 0.4
    elif fault_type == "eps_load_step" and "extra_load_a" in params:
        params["extra_load_a"] = magnitude
    elif fault_type == "solar_degradation" and "factor" in params:
        params["factor"] = max(0.1, 1.0 - magnitude)
    return fault_type, params, DEFAULT_FAULT_DURATION_S


def alerts_to_symptom_events(alerts) -> List[Dict[str, Any]]:
    """Convert live AlertEvents into the symptom-event dicts Diagnose wants.

    The bridge passes dicts (not the live SymptomEvent dataclass)
    because the twin's SymptomEvent lives in twin/diagnose.py, and
    we want to keep the live side free of twin imports. The dict
    shape matches twin.diagnose.SymptomEvent: {channel, subsystem,
    kind, score, seq, ts}.

    Args:
        alerts: list of live/error_stream.py AlertEvent dataclasses

    Returns:
        list of symptom-event dicts (may be empty)
    """
    events = []
    for a in alerts:
        # The AlertEvent doesn't carry channel/subsystem directly.
        # We use the score magnitude as a proxy: bigger anomaly ->
        # hotter channel. For the demo we surface the anomaly's seq
        # range; Diagnose's ranking is dominated by the score-channel
        # match in the bridge's channel-specific path.
        # TODO(phase2): carry channel/subsystem in AlertEvent itself.
        # For now, when the bridge was triggered by a channel-specific
        # injection, the operator knows what channel the verdict
        # applies to (it's in the fault_id -> cause mapping below).
        # We emit one event with a generic "P-1" channel; Diagnose
        # will score it against P-1 causes. For channel-specific
        # T-1/T-2/A-1 injections, the dashboard's static channel
        # hint is the source of truth in Phase 1.
        # -- replace this with proper event-to-channel plumbing later.
        events.append({
            "channel": "P-1",   # default; the channel-specific bridge
                                  # below overrides this for the
                                  # events that came from the matching
                                  # injection.
            "subsystem": "eps",
            "kind": a.kind if hasattr(a, "kind") else "anomaly",
            "score": float(a.score),
            "seq": list(a.seq) if hasattr(a, "seq") else [0, 0],
            "ts": float(a.t) if hasattr(a, "t") else 0.0,
        })
    return events


def run_phase1_pipeline(
    fault_type: str,
    fault_params: Dict[str, Any],
    alerts,
    channel_hint: Optional[str] = None,
    horizon_s: float = 3600.0,
    dt_s: float = 120.0,
    progress_cb: Optional[Any] = None,
    fault_id: Optional[str] = None,
    injection_meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Run Diagnose -> Propose -> Verdict for the current injection.

    Args:
        fault_type: the twin fault_type from map_injection_to_fault
        fault_params: the fault params from map_injection_to_fault
        alerts: the live AlertEvents that triggered this pipeline call
        channel_hint: if known, the channel the injection targeted;
            used to refine the SymptomEvent for Diagnose
        horizon_s, dt_s: Propose's twin-validation horizon/step
        progress_cb: optional per-step progress callback forwarded to
            propose(). Propose fires this from worker threads when
            running parallel sims. Signature:
            (procedure_value, step, total, snapshot_state). The
            bridge does NOT add thread-safety; the caller (the WS
            server) is responsible for queuing the events onto the
            right thread (typically via queue.Queue).
        fault_id: the FaultScheduler-injected id (e.g. "F-003"). If
            provided, the bridge stamps it on the returned runbook.
        injection_meta: dict of {kind, channel, magnitude,
            injected_at_unix} from the HTTP request. Stamped on the
            runbook's "injection" block. May be None for offline
            tests.

    Returns:
        Dict with two keys:
          - "proposal": JSON-serializable dict (the proposal.to_dict()
            output). The WS server attaches this to the next alert
            tick's broadcast and returns it in the inject response.
          - "runbook": the full enriched runbook payload (without
            runbook_id, which the store mints). The WS server pushes
            this into the runbook store and attaches the minted id
            to the WS message.
        Returns None if the twin isn't installed (CHESS venv missing
        on the system Python); the live server keeps running, the
        operator just doesn't see a verdict this tick.
    """
    _ensure_twin_on_path()
    try:
        # Deferred imports: the twin pulls in astropy and the CHESS
        # simulation; we don't want that cost on every tick of the
        # live server. Only when a fault is actually being validated.
        from twin.diagnose import SymptomEvent, diagnose
        from twin.propose import propose
        from twin.state import make_default_state
        from twin.procedures import (
            Cause, Procedure, PROCEDURE_REGISTRY, get_candidate_procedures,
        )
    except Exception as e:
        logger.warning(
            "[bridge] twin import failed (%s); skipping Phase 1 pipeline", e
        )
        return None

    import time as _time
    pipeline_timestamps: Dict[str, float] = {}

    # Build the symptom window. The bridge's contract (BIBLE §2 +
    # Phase 1 design): the pipeline is *predictive* — it runs at
    # injection time on the fault's expected behavior, not on the
    # (not-yet-detected) LSTM alerts. So we seed the window with
    # synthetic SymptomEvents that match the channel_hint, even if
    # the live alerts list is empty. This is what makes the
    # /inject_twin_fault endpoint useful as a stand-alone API.
    #
    # The two seeding paths:
    # 1. channel_hint + alerts present -> use real alerts with the
    #    channel hint propagated. This is the live-stream case.
    # 2. channel_hint + no alerts -> synthesize one SymptomEvent
    #    with the channel hint. This is the demo case where the
    #    operator injects a fault and wants a verdict immediately.
    # 3. No channel_hint -> fall back to the legacy converter
    #    (which hardcodes "P-1" / "eps" — wrong for non-EPS faults;
    #    a Phase 2 cleanup).
    symptom_events: List[SymptomEvent] = []
    if channel_hint and channel_hint in CHANNEL_TO_SUBSYSTEM:
        if alerts:
            for a in alerts:
                symptom_events.append(SymptomEvent(
                    channel=channel_hint,
                    subsystem=CHANNEL_TO_SUBSYSTEM[channel_hint],
                    kind=a.kind if hasattr(a, "kind") else "anomaly",
                    score=float(a.score),
                    seq=tuple(a.seq) if hasattr(a, "seq") else (0, 0),
                    ts=float(a.t) if hasattr(a, "t") else 0.0,
                ))
        else:
            # Seed with a single synthetic event so Diagnose has
            # something to score. The score is set to 1.0 to clear
            # MIN_DIAGNOSE_SCORE (0.5) easily; the cause_score in
            # the returned verdict reflects this seeding policy.
            symptom_events.append(SymptomEvent(
                channel=channel_hint,
                subsystem=CHANNEL_TO_SUBSYSTEM[channel_hint],
                kind="anomaly",
                score=1.0,
                seq=(0, 0),
                ts=0.0,
            ))
    else:
        # Fall back to the legacy AlertEvent->dict conversion.
        for d in alerts_to_symptom_events(alerts):
            symptom_events.append(SymptomEvent(**d))

    # Diagnose.
    pipeline_timestamps["stage_2_at_unix"] = _time.time()
    candidates = diagnose(symptom_events)
    if not candidates:
        return None
    top = candidates[0]
    if top.cause == Cause.NO_FAULT_DETECTED:
        # Nothing to propose. Tell the operator we considered it.
        # Return a BIBLE-shape-consistent dict (matches the keys
        # that proposal.to_dict() produces) so callers can rely on
        # the same shape regardless of whether a cause was found.
        proposal_dict = {
            "cause": Cause.NO_FAULT_DETECTED.value,
            "cause_score": top.score,
            "procedure": None,
            "procedure_params": {},
            "risk_score": 0.0,
            "verdict": {
                "status": "OK",
                "reason": "No fault detected in symptom window",
                "proposal_id": "",
                "twin_simulation_digest": "",
                "per_step_outcomes": [],
                "notes": [],
            },
            "candidates_ranked": [],
        }
        # Still build a runbook so the operator sees the "no fault"
        # outcome in the runbook list; it has no candidates and no
        # trajectory.
        try:
            from . import runbook_builder
            runbook = runbook_builder.build_runbook(
                fault_id=fault_id or "F-000",
                fault_type=fault_type,
                injection=injection_meta or {},
                pipeline_timestamps=pipeline_timestamps,
                proposal_dict=proposal_dict,
                cause_catalog={
                    "affected_subsystems": Cause.NO_FAULT_DETECTED.affected_subsystems(),
                    "expected_channels": Cause.NO_FAULT_DETECTED.expected_channels(),
                },
                candidates_considered=[],
                symptom_window=[
                    {
                        "channel": e.channel, "subsystem": e.subsystem,
                        "kind": e.kind, "score": e.score, "seq": list(e.seq),
                        "ts": e.ts,
                    }
                    for e in symptom_events
                ],
                horizon_s=horizon_s, dt_s=dt_s,
                primary_subsystem=(
                    CHANNEL_TO_SUBSYSTEM.get(channel_hint) if channel_hint else None
                ),
                channel=channel_hint,
            )
        except Exception as e:
            logger.warning("[bridge] runbook build failed (no-fault path): %s", e)
            runbook = None
        return {"proposal": proposal_dict, "runbook": runbook}

    # Propose. The starting_state for validate_procedure is the
    # default state; per the plan T2, the defaults are safe across
    # a range of starting conditions for the demo. A future phase
    # can pipe the actual post-fault state through here.
    pipeline_timestamps["stage_3_at_unix"] = _time.time()
    starting_state = make_default_state()
    proposal = propose(
        top.cause, starting_state, cause_score=top.score,
        horizon_s=horizon_s, dt_s=dt_s,
        progress_cb=progress_cb,
    )
    pipeline_timestamps["stage_4_at_unix"] = _time.time()

    proposal_dict = proposal.to_dict()

    # Build candidates_considered: every Procedure the cause maps to,
    # each with its spec attached so the runbook builder can pull
    # description / risk_class / approval_required.
    all_candidate_procs = get_candidate_procedures(top.cause)
    simulated_names = {rc.procedure for rc in proposal.candidates_ranked}
    candidates_considered: List[Dict[str, Any]] = []
    for proc in all_candidate_procs:
        spec = PROCEDURE_REGISTRY[proc]
        # The simulated subset also carries risk_score; the builder
        # needs both the catalog spec AND the runtime score.
        risk_score = None
        for rc in proposal.candidates_ranked:
            if rc.procedure == proc:
                risk_score = float(rc.risk_score)
                break
        candidates_considered.append({
            "procedure": proc.value,
            "effort_score": spec.effort_score,
            "mission_impact": spec.mission_impact,
            "reversibility": spec.reversibility,
            "risk_score": risk_score,
            "_spec": {
                "description": spec.description,
                "risk_class": spec.risk_class,
                "approval_required": spec.approval_required,
                "effort_score": spec.effort_score,
                "mission_impact": spec.mission_impact,
                "reversibility": spec.reversibility,
            },
        })

    # Pull the predicted/baseline trajectories from the winner's
    # ValidationResult so the runbook canvas has data to render.
    winner_validation = proposal.validation
    predicted_traj = getattr(winner_validation, "predicted_trajectory", None)
    baseline_traj = getattr(winner_validation, "baseline_trajectory", None)

    try:
        from . import runbook_builder
        runbook = runbook_builder.build_runbook(
            fault_id=fault_id or "F-000",
            fault_type=fault_type,
            injection=injection_meta or {},
            pipeline_timestamps=pipeline_timestamps,
            proposal_dict=proposal_dict,
            cause_catalog={
                "affected_subsystems": list(top.cause.affected_subsystems()),
                "expected_channels": list(top.cause.expected_channels()),
            },
            candidates_considered=candidates_considered,
            symptom_window=[
                {
                    "channel": e.channel, "subsystem": e.subsystem,
                    "kind": e.kind, "score": e.score, "seq": list(e.seq),
                    "ts": e.ts,
                }
                for e in symptom_events
            ],
            predicted_trajectory=predicted_traj,
            baseline_trajectory=baseline_traj,
            horizon_s=horizon_s, dt_s=dt_s,
            primary_subsystem=(
                CHANNEL_TO_SUBSYSTEM.get(channel_hint) if channel_hint else None
            ),
            channel=channel_hint,
        )
    except Exception as e:
        # The runbook is a derived view; if its builder raises
        # (e.g. numpy in scope issues), don't fail the whole pipeline.
        logger.warning("[bridge] runbook build failed: %s", e)
        runbook = None

    return {"proposal": proposal_dict, "runbook": runbook}


def inject_fault(
    scheduler,
    kind: str,
    magnitude: float,
    channel: Optional[str] = None,
    start_t: float = 0.0,
) -> Dict[str, Any]:
    """Translate an HTTP injection and schedule it on the given scheduler.

    Args:
        scheduler: a twin.fault_injection.FaultScheduler instance
        kind, magnitude, channel: from the HTTP query params
        start_t: when the fault starts in sim time (default 0; the
            FaultScheduler treats this as immediate)

    Returns:
        {"ok": True, "fault_id": "F-001", "fault_type": "...",
         "kind": ..., "channel": ..., "magnitude": ...}
    """
    fault_type, params, duration_s = map_injection_to_fault(kind, magnitude, channel)
    fault_id = scheduler.inject(
        fault_type=fault_type,
        start_t=start_t,
        end_t=start_t + duration_s,
        parameters=params,
    )
    logger.info(
        "[bridge] injected %s -> %s id=%s params=%s end_t=%.0f",
        f"{kind}/{channel}" if channel else kind, fault_type,
        fault_id, params, start_t + duration_s,
    )
    return {
        "ok": True,
        "fault_id": fault_id,
        "fault_type": fault_type,
        "kind": kind,
        "channel": channel,
        "magnitude": magnitude,
    }
