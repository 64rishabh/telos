"""Tests for the default_params field on ProcedureSpec and the
get_default_params() helper."""

from __future__ import annotations
import pytest

from twin.procedures import (
    Cause, Procedure, PROCEDURE_REGISTRY, VALID_MISSION_IMPACTS,
    VALID_REVERSIBILITY, get_candidate_procedures,
    get_default_params, apply_procedure,
)


def test_every_procedure_has_defaults():
    """BIBLE D-10 says catalog and twin are one file; every procedure
    must have a default_params entry so Propose can validate without
    cause-specific tuning in Phase 1."""
    for proc in Procedure:
        spec = PROCEDURE_REGISTRY[proc]
        assert spec.default_params is not None, (
            f"{proc.value} is missing default_params"
        )


def test_get_default_params_returns_fresh_dict():
    """Mutating the returned dict must not affect the registry."""
    p = get_default_params(Procedure.EPS_SHED_LOAD)
    p["load_reduction_a"] = 999.0
    p2 = get_default_params(Procedure.EPS_SHED_LOAD)
    assert p2["load_reduction_a"] == 1.0


def test_get_default_params_validates_against_schema():
    """The defaults must validate against each procedure's params_schema.
    Catches a typo in the catalog at import-time."""
    for proc in Procedure:
        params = get_default_params(proc)
        # validate_params raises on any mismatch.
        PROCEDURE_REGISTRY[proc].validate_params(params)


def test_get_default_params_returns_empty_dict_for_paramless_procedures():
    """MODE_CHANGE_TO_SAFE and ADCS_SAFE_HOLD take no params; default
    is {}. The helper must return {} (not None) so callers can
    .update() it without a NoneType check."""
    assert get_default_params(Procedure.MODE_CHANGE_TO_SAFE) == {}
    assert get_default_params(Procedure.ADCS_SAFE_HOLD) == {}


def test_eps_shed_default_params_match_smoke_test():
    """Smoke test (examples/smoke_test.py) hardcodes the same values;
    we keep them identical so the smoke test is not lying."""
    p = get_default_params(Procedure.EPS_SHED_LOAD)
    assert p == {"load_reduction_a": 1.0, "duration_s": 3600.0}


# ----- Catalog-level editorial signals (effort / impact / reversibility) ---

def test_every_procedure_has_effort_score_in_unit_range():
    """effort_score must be a float in [0.0, 1.0] for every entry.
    The import-time validation already enforces this; the test
    guards against a future relaxation."""
    for proc in Procedure:
        spec = PROCEDURE_REGISTRY[proc]
        assert isinstance(spec.effort_score, float)
        assert 0.0 <= spec.effort_score <= 1.0, (
            f"{proc.value}: effort_score={spec.effort_score} out of [0,1]"
        )


def test_every_procedure_has_valid_mission_impact():
    """mission_impact must be one of the allowed values."""
    for proc in Procedure:
        spec = PROCEDURE_REGISTRY[proc]
        assert spec.mission_impact in VALID_MISSION_IMPACTS, (
            f"{proc.value}: mission_impact={spec.mission_impact!r}"
        )


def test_every_procedure_has_valid_reversibility():
    """reversibility must be one of the allowed values."""
    for proc in Procedure:
        spec = PROCEDURE_REGISTRY[proc]
        assert spec.reversibility in VALID_REVERSIBILITY, (
            f"{proc.value}: reversibility={spec.reversibility!r}"
        )


def test_wait_is_lowest_effort_and_most_reversible():
    """Spot-check the editorial judgments: WAIT is the cheapest and
    safest procedure in the catalog (no work, no impact, trivial
    rollback). MODE_CHANGE_TO_SAFE is the most expensive and
    hardest to reverse."""
    wait = PROCEDURE_REGISTRY[Procedure.WAIT]
    assert wait.effort_score == 0.05
    assert wait.mission_impact == "none"
    assert wait.reversibility == "trivial"

    mode_change = PROCEDURE_REGISTRY[Procedure.MODE_CHANGE_TO_SAFE]
    assert mode_change.effort_score == 0.85
    assert mode_change.mission_impact == "mission-ending"
    assert mode_change.reversibility == "hard"
