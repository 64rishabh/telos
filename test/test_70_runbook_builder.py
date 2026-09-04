"""D-19 runbook builder: enrichment layer tests.

B50 builder_populates_cause_catalog_fields: cause's
    affected_subsystems + expected_channels come from the catalog
    table (the bridge passes them in via cause_catalog).
B51 builder_populates_procedure_catalog_fields: for every entry in
    candidates_ranked, description/risk_class/approval_required
    come from the spec (the bridge attaches it via _spec).
B52 candidates_considered_preserves_full_set:
    candidates_considered_detail has every procedure the bridge
    listed, not just the simulated ones.
B52b considered_detail_marks_simulated: for each entry,
    simulated=true iff it appears in candidates_ranked; the
    drop_reason is set when simulated=false.
B53 winner_factors_explain_choice: winner.factors has reason text
    + rank_key_comparison list; for each loser,
    factors.rejected_because names the deciding axis.
B54 trajectory_has_three_fields: for horizon=3600, dt=120, each
    of battery_soc / battery_temp_c / payload_temp_c has 31 points
    (n_steps + 1); baseline + predicted present.
B55 pipeline_stage_timestamps_monotonic: detected_at <= stage_2
    <= stage_3 <= stage_4.
B56 verdict_passthrough: verdict block identical to the
    proposal_dict input.
B57 payload_is_json_serializable: json.dumps on the full payload
    succeeds (numpy arrays coerced via .tolist()).
B58 winner_ranking_algorithm_described: winner.ranking_algorithm
    is the lexicographic description.
B59 single_candidate_no_runner_up: when ranked has 1 entry,
    winner.factors.reason is the "only candidate" text and
    rank_key_comparison is [].
B60 summary_view_and_trajectory_view: the projection helpers
    return the lightweight shapes used by the HTTP endpoints.
"""
from __future__ import annotations

import json
import time

import numpy as np
import pytest


# Helpers ---------------------------------------------------------------

def _sample_cause_catalog() -> dict:
    return {
        "affected_subsystems": ["thermal"],
        "expected_channels": ["B-1", "T-1", "T-2"],
    }


def _sample_candidates_considered() -> list:
    """Three procedures with their specs attached, in the order
    the bridge would pass them in (catalog order)."""
    return [
        {
            "procedure": "thermal_enable_heater_backup",
            "effort_score": 0.15,
            "mission_impact": "none",
            "reversibility": "trivial",
            "_spec": {
                "description": "Activate the backup heater for a thermal node.",
                "risk_class": "low",
                "approval_required": "operator",
                "effort_score": 0.15,
                "mission_impact": "none",
                "reversibility": "trivial",
            },
        },
        {
            "procedure": "thermal_throttle_payload",
            "effort_score": 0.40,
            "mission_impact": "major",
            "reversibility": "easy",
            "_spec": {
                "description": "Reduce payload power consumption.",
                "risk_class": "medium",
                "approval_required": "operator",
                "effort_score": 0.40,
                "mission_impact": "major",
                "reversibility": "easy",
            },
        },
        {
            "procedure": "mode_change_to_safe",
            "effort_score": 0.85,
            "mission_impact": "mission-ending",
            "reversibility": "hard",
            "_spec": {
                "description": "Enter safe mode.",
                "risk_class": "critical",
                "approval_required": "director",
                "effort_score": 0.85,
                "mission_impact": "mission-ending",
                "reversibility": "hard",
            },
        },
    ]


def _sample_ranked() -> list:
    """The simulated top-K, sorted by multi-axis rank_key. The
    winner is thermal_enable_heater_backup because mission_impact
    is none(0.0) < major(0.5)."""
    return [
        {
            "procedure": "thermal_enable_heater_backup",
            "risk_score": 0.21,
            "effort_score": 0.15,
            "mission_impact": "none",
            "reversibility": "trivial",
        },
        {
            "procedure": "thermal_throttle_payload",
            "risk_score": 0.12,
            "effort_score": 0.40,
            "mission_impact": "major",
            "reversibility": "easy",
        },
    ]


def _sample_proposal_dict() -> dict:
    return {
        "cause": "thermal_heater_stuck_on",
        "cause_score": 0.9,
        "procedure": "thermal_enable_heater_backup",
        "procedure_params": {"node": "battery"},
        "risk_score": 0.21,
        "verdict": {
            "status": "OK",
            "reason": "thermal_enable_heater_backup OK for thermal_heater_stuck_on: risk=0.21 < threshold=0.30",
            "proposal_id": "",
            "twin_simulation_digest": "",
            "per_step_outcomes": [],
            "notes": [],
        },
        "candidates_ranked": _sample_ranked(),
        "winner_rank_key": [0.0, 0.15, 0.0, 0.21],
    }


def _sample_injection() -> dict:
    return {
        "kind": "shift",
        "channel": "T-1",
        "magnitude": 1.0,
        "horizon_s": 3600.0,
        "dt_s": 120.0,
        "injected_at_unix": 1725512345.67,
    }


def _sample_trajectories(n: int = 31):
    pred = {
        "battery_soc": np.linspace(0.85, 0.83, n, dtype=np.float64),
        "battery_temp_c": np.linspace(22.0, 22.5, n, dtype=np.float64),
        "payload_temp_c": np.linspace(30.0, 28.0, n, dtype=np.float64),
    }
    base = {
        "battery_soc": np.linspace(0.85, 0.80, n, dtype=np.float64),
        "battery_temp_c": np.linspace(22.0, 23.0, n, dtype=np.float64),
        "payload_temp_c": np.linspace(30.0, 33.0, n, dtype=np.float64),
    }
    return pred, base


def _sample_pipeline_timestamps() -> dict:
    return {
        "stage_1_at_unix": 1725512345.50,
        "stage_2_at_unix": 1725512345.62,
        "stage_3_at_unix": 1725512345.71,
        "stage_4_at_unix": 1725512345.78,
    }


def _build_sample():
    pred, base = _sample_trajectories(31)
    return live.runbook_builder.build_runbook(
        fault_id="F-003",
        fault_type="thermal_heater_stuck_on",
        injection=_sample_injection(),
        pipeline_timestamps=_sample_pipeline_timestamps(),
        proposal_dict=_sample_proposal_dict(),
        cause_catalog=_sample_cause_catalog(),
        candidates_considered=_sample_candidates_considered(),
        symptom_window=[
            {"channel": "T-1", "subsystem": "thermal",
             "kind": "anomaly", "score": 1.0, "seq": [0, 0], "ts": 0.0},
        ],
        predicted_trajectory=pred,
        baseline_trajectory=base,
        horizon_s=3600.0,
        dt_s=120.0,
        primary_subsystem="thermal",
        channel="T-1",
    )


# Import the module under test once; lazy to keep test collection fast.
import live.runbook_builder  # noqa: E402


# Tests -----------------------------------------------------------------

def test_B50_builder_populates_cause_catalog_fields():
    rb = _build_sample()
    assert rb["cause"]["affected_subsystems"] == ["thermal"]
    assert rb["cause"]["expected_channels"] == ["B-1", "T-1", "T-2"]


def test_B51_builder_populates_procedure_catalog_fields():
    rb = _build_sample()
    for entry in rb["candidates_ranked"]:
        assert "description" in entry
        assert "risk_class" in entry
        assert "approval_required" in entry
        assert "rank" in entry
    # Winner (rank 1) is thermal_enable_heater_backup -> "low", "operator".
    winner = rb["candidates_ranked"][0]
    assert winner["procedure"] == "thermal_enable_heater_backup"
    assert winner["rank"] == 1
    assert winner["risk_class"] == "low"
    assert winner["approval_required"] == "operator"
    # Runner-up is thermal_throttle_payload -> "medium", "operator".
    runner = rb["candidates_ranked"][1]
    assert runner["procedure"] == "thermal_throttle_payload"
    assert runner["rank"] == 2
    assert runner["risk_class"] == "medium"


def test_B52_candidates_considered_preserves_full_set():
    rb = _build_sample()
    procs = {e["procedure"] for e in rb["candidates_considered_detail"]}
    assert procs == {
        "thermal_enable_heater_backup",
        "thermal_throttle_payload",
        "mode_change_to_safe",
    }


def test_B52b_considered_detail_marks_simulated():
    rb = _build_sample()
    by_proc = {e["procedure"]: e for e in rb["candidates_considered_detail"]}
    assert by_proc["thermal_enable_heater_backup"]["simulated"] is True
    assert by_proc["thermal_throttle_payload"]["simulated"] is True
    assert by_proc["mode_change_to_safe"]["simulated"] is False
    assert by_proc["mode_change_to_safe"]["risk_score"] is None
    assert by_proc["mode_change_to_safe"]["drop_reason"] == "coarse_rank above top-K"
    # Simulated ones have non-null risk_score.
    assert by_proc["thermal_enable_heater_backup"]["risk_score"] == 0.21
    assert by_proc["thermal_throttle_payload"]["risk_score"] == 0.12


def test_B53_winner_factors_explain_choice():
    rb = _build_sample()
    factors = rb["winner"]["factors"]
    assert "reason" in factors
    assert "rank_key_comparison" in factors
    # Winner is thermal_enable_heater_backup; the deciding axis is
    # mission_impact (none < major).
    assert "mission_impact" in factors["reason"]
    # rank_key_comparison has 4 axes; the first deciding one is
    # mission_impact.
    cmp = factors["rank_key_comparison"]
    assert len(cmp) == 4
    deciding = [r for r in cmp if r["deciding"]]
    assert len(deciding) == 1
    assert deciding[0]["axis"] == "mission_impact"
    assert deciding[0]["winner_better"] is True
    # The runner-up's factors.rejected_because names the deciding axis.
    runner_factors = rb["candidates_ranked"][1]["factors"]
    assert runner_factors["selected"] is False
    assert "mission_impact" in runner_factors["rejected_because"]


def test_B54_trajectory_has_three_fields_with_31_points():
    rb = _build_sample()
    traj = rb["trajectory"]
    assert len(traj["t_s"]) == 31
    for field in ("battery_soc", "battery_temp_c", "payload_temp_c"):
        assert field in traj
        assert len(traj[field]["baseline"]) == 31
        assert len(traj[field]["predicted"]) == 31


def test_B55_pipeline_stage_timestamps_monotonic():
    rb = _build_sample()
    pl = rb["pipeline"]
    t = lambda k: pl[k]["at_unix"]
    assert t("stage_1_detect") <= t("stage_2_diagnose")
    assert t("stage_2_diagnose") <= t("stage_3_propose")
    assert t("stage_3_propose") <= t("stage_4_validate")


def test_B56_verdict_passthrough():
    rb = _build_sample()
    assert rb["verdict"]["status"] == "OK"
    assert "thermal_enable_heater_backup" in rb["verdict"]["reason"]


def test_B57_payload_is_json_serializable():
    rb = _build_sample()
    # Must not raise; numpy arrays are coerced via .tolist() in
    # _trajectory_payload.
    text = json.dumps(rb)
    assert len(text) > 0
    # And the round-trip parses back.
    parsed = json.loads(text)
    assert parsed["fault_id"] == "F-003"


def test_B58_winner_ranking_algorithm_described():
    rb = _build_sample()
    assert "lexicographic" in rb["winner"]["ranking_algorithm"]
    assert "mission_impact" in rb["winner"]["ranking_algorithm"]
    assert "risk_score" in rb["winner"]["ranking_algorithm"]


def test_B59_single_candidate_no_runner_up():
    """When only one procedure is ranked, factors.reason is the
    "only candidate" text and rank_key_comparison is []."""
    pred, base = _sample_trajectories(31)
    proposal = _sample_proposal_dict()
    proposal["candidates_ranked"] = proposal["candidates_ranked"][:1]
    proposal["procedure"] = "thermal_enable_heater_backup"
    proposal["risk_score"] = 0.21
    rb = live.runbook_builder.build_runbook(
        fault_id="F-100",
        fault_type="thermal_heater_stuck_on",
        injection=_sample_injection(),
        pipeline_timestamps=_sample_pipeline_timestamps(),
        proposal_dict=proposal,
        cause_catalog=_sample_cause_catalog(),
        candidates_considered=_sample_candidates_considered(),
        symptom_window=[],
        predicted_trajectory=pred,
        baseline_trajectory=base,
    )
    assert rb["winner"]["factors"]["reason"].startswith("thermal_enable_heater_backup was the only candidate")
    assert rb["winner"]["factors"]["rank_key_comparison"] == []


def test_B60_summary_view_and_trajectory_view():
    rb = _build_sample()
    rb["runbook_id"] = "RB-TEST-0001"
    s = live.runbook_builder.summary_view(rb)
    assert s["runbook_id"] == "RB-TEST-0001"
    assert s["fault_id"] == "F-003"
    assert s["cause"] == "thermal_heater_stuck_on"
    assert s["winner"] == "thermal_enable_heater_backup"
    assert s["verdict_status"] == "OK"
    assert s["approval_status"] == "PENDING"
    # The summary must NOT carry the heavy fields.
    assert "candidates_ranked" not in s
    assert "trajectory" not in s
    assert "symptom_window" not in s
    # Trajectory view: just the trajectory block.
    t = live.runbook_builder.trajectory_view(rb)
    assert "t_s" in t
    assert "battery_soc" in t
