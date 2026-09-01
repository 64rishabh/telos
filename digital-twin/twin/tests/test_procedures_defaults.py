"""Tests for the default_params field on ProcedureSpec and the
get_default_params() helper."""

from __future__ import annotations
import pytest

from twin.procedures import (
    Cause, Procedure, PROCEDURE_REGISTRY, get_candidate_procedures,
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
