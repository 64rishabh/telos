"""
Stage 2 — Diagnose.

Pure function: take a sliding window of SymptomEvents from the
streaming detector, score every Cause in the catalog against the
window's evidence, and return the ranked list of candidate causes.

The BIBLE §6.2 cause table is the contract: each Cause declares
which channels it makes anomalous (expected_channels) and which
subsystems it touches (affected_subsystems). Diagnose uses those
declarations as the scoring rubric.

Scoring (per Cause, per event):
  - +1.0 if SymptomEvent.channel in Cause.expected_channels()
  - +0.5 if SymptomEvent.subsystem in Cause.affected_subsystems()
  - +0.25 if the same cause has already scored in this window
          (a "stickiness" bonus that prefers causes that explain
          the *whole* window, not just the latest event)
  - 0 (cause ruled out) if no channel overlap at all

Threshold: if no cause scores >= MIN_DIAGNOSE_SCORE (default 0.5),
the window is noise and the diagnosis is NO_FAULT_DETECTED.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from twin.procedures import Cause


# A SymptomEvent is the Phase 1 re-typed AlertEvent (BIBLE §2). It
# carries the channel and subsystem the live detector's anomaly
# originated from, plus the anomaly score and the live stream indices
# it occupies. Defined here (not imported from live/) so twin/ has
# no upstream dependency on live/.
@dataclass(frozen=True)
class SymptomEvent:
    channel: str                       # "P-1" | "P-2" | "B-1" | "T-1" | "T-2" | "A-1" | "G-1" | "D-1"
    subsystem: str                     # "eps" | "thermal" | "adcs" | "comms"
    kind: str = "anomaly"              # "anomaly" | "shift" | "spike" | "dropout" | "noise"
    score: float = 0.0
    seq: tuple = (0, 0)                # (start_t, end_t) in live stream indices
    ts: float = 0.0                    # sim time, if known


@dataclass
class CandidateCause:
    cause: Cause
    score: float
    matched_events: List[SymptomEvent] = field(default_factory=list)
    evidence_subsystems: List[str] = field(default_factory=list)


# If nothing scores at least this, declare NO_FAULT_DETECTED.
MIN_DIAGNOSE_SCORE: float = 0.5

# Stickiness bonus: when a cause has already explained an earlier
# event in the window, give it a small bonus on each subsequent
# matching event. Keeps a single coherent cause at the top of the
# ranking instead of letting two near-equal causes trade places.
STICKINESS_BONUS: float = 0.25


def _event_matches_cause(event: SymptomEvent, cause: Cause) -> bool:
    """True if the event is on a channel or subsystem this cause
    declares would be affected. BIBLE §6.2 is the contract."""
    if event.channel in cause.expected_channels():
        return True
    if event.subsystem in cause.affected_subsystems():
        return True
    return False


def _score_event_for_cause(event: SymptomEvent, cause: Cause) -> float:
    """Per-event contribution to the cause's total score."""
    s = 0.0
    if event.channel in cause.expected_channels():
        s += 1.0
    if event.subsystem in cause.affected_subsystems():
        s += 0.5
    return s


def diagnose(symptom_window: List[SymptomEvent]) -> List[CandidateCause]:
    """Score every Cause against the symptom window.

    Args:
        symptom_window: trailing window of SymptomEvents from the
            streaming detector. Order is oldest-first.

    Returns:
        Ranked list of CandidateCause, highest score first. Empty
        list means nothing matched (the caller should treat this as
        NO_FAULT_DETECTED). The first element is the diagnosis.

    Notes:
        - SENSOR_NOISE is a "soft" cause: it can match any channel
          (its expected_channels() returns []), but it only matches
          via subsystem overlap. So a noisy P-1 channel with
          no other cause matching will fall to NO_FAULT_DETECTED
          unless the subsystem is in sensor_noise.affected_subsystems()
          (which it is for eps/thermal/adcs).
        - The cause enum value is the source of truth for what
          counts as a match; we do not encode any extra knowledge here.
    """
    if not symptom_window:
        # Empty window -> sentinel, no fault.
        return [CandidateCause(cause=Cause.NO_FAULT_DETECTED, score=0.0)]

    scores: Dict[Cause, CandidateCause] = {}

    for event in symptom_window:
        for cause in Cause:
            if not _event_matches_cause(event, cause):
                continue
            contribution = _score_event_for_cause(event, cause)
            if contribution == 0.0:
                continue

            cand = scores.get(cause)
            if cand is None:
                cand = CandidateCause(cause=cause, score=0.0)
                scores[cause] = cand

            # Stickiness: if this cause already matched an earlier
            # event in the window, give it a small bonus on each
            # subsequent matching event.
            if cand.matched_events:
                contribution += STICKINESS_BONUS

            cand.score += contribution
            cand.matched_events.append(event)
            if event.subsystem not in cand.evidence_subsystems:
                cand.evidence_subsystems.append(event.subsystem)

    ranked = sorted(scores.values(), key=lambda c: c.score, reverse=True)

    # If nothing met the threshold, return the sentinel as the only
    # candidate. The caller can take .cause == NO_FAULT_DETECTED to
    # short-circuit (no Propose step needed).
    if not ranked or ranked[0].score < MIN_DIAGNOSE_SCORE:
        return [CandidateCause(cause=Cause.NO_FAULT_DETECTED, score=0.0)]

    return ranked


def top_cause(symptom_window: List[SymptomEvent]) -> Cause:
    """Convenience: return just the top cause. Use this when you
    don't need the evidence chain (e.g., the demo loop)."""
    candidates = diagnose(symptom_window)
    return candidates[0].cause
