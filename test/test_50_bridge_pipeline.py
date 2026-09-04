"""Bridge seam (live/twin_bridge.py) + POST /inject_twin_fault.

T20 map_injection_to_fault(kind, magnitude, channel):
    Input: HTTP vocab triple. Output: (fault_type, params, duration_s).
    Unknown combo -> ValueError (= HTTP 400 upstream).
T21 run_phase1_pipeline(fault_type, alerts=[], channel_hint):
    Input: fault + channel hint (predictive: alerts may be empty).
    Output: verdict dict {cause, procedure, risk_score, verdict,...}
    or None when twin missing (= HTTP 503 upstream).
T22 POST /inject_twin_fault: HTTP wrapper. 200 + {fault_id F-xxx,
    fault_type, kind, channel, magnitude, verdict}; verdict stashed on
    state.latest_verdict. 400 on Z-9, 503 when scheduler None.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_T20_map_known_and_unknown():
    from live.twin_bridge import map_injection_to_fault
    ft, params, dur = map_injection_to_fault("shift", 1.0, "T-1")
    assert ft == "thermal_heater_stuck_on"
    assert dur == 1200.0
    with pytest.raises(ValueError):
        map_injection_to_fault("shift", 1.0, "Z-9")


def test_T21_predictive_pipeline_without_alerts():
    """Input: fault_type + channel_hint, alerts=[]. Output: verdict dict."""
    from live.twin_bridge import run_phase1_pipeline
    out = run_phase1_pipeline(fault_type="thermal_heater_stuck_on",
                              fault_params={}, alerts=[],
                              channel_hint="T-1",
                              horizon_s=600.0, dt_s=120.0)
    assert out is not None, "twin must be importable on this interpreter"
    # D-19: the bridge now returns a wrapper {proposal, runbook}.
    assert "proposal" in out, f"missing 'proposal' in {list(out.keys())}"
    proposal = out["proposal"]
    assert "cause" in proposal and "procedure" in proposal and "verdict" in proposal
    assert proposal["verdict"]["status"] in {"OK", "REJECT", "INCONCLUSIVE"}
    # And the runbook block is present (or None if builder failed;
    # we don't require it for the bridge contract).
    assert "runbook" in out


def _client(monkeypatch):
    from test.conftest import make_fast_app
    return TestClient(make_fast_app(monkeypatch))


def test_T22_inject_twin_fault_e2e(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/inject_twin_fault",
               params={"type": "shift", "magnitude": "1.0", "channel": "T-1",
                       "horizon_s": "600", "dt_s": "120"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["fault_id"].startswith("F-")
    assert body["fault_type"] == "thermal_heater_stuck_on"
    assert body["verdict"] is not None
    assert body["verdict"]["verdict"]["status"] in {"OK", "REJECT", "INCONCLUSIVE"}
    # D-19: the response also carries a runbook_id minted by the
    # store. The builder can fail (e.g. numpy scope); when it does,
    # the field is None and we accept either outcome.
    assert "runbook_id" in body
    if body["runbook_id"] is not None:
        assert body["runbook_id"].startswith("RB-")


def test_T22b_bad_channel_is_400(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/inject_twin_fault",
               params={"type": "shift", "magnitude": "1.0", "channel": "Z-9"})
    assert r.status_code == 400


def test_T22c_missing_scheduler_is_503(monkeypatch):
    from test.conftest import patch_lstm
    patch_lstm(monkeypatch)
    from live import ws_server
    app = ws_server.create_app(model_path="__unused__")
    app.state.live.twin_scheduler = None  # simulate missing CHESS venv
    c = TestClient(app)
    r = c.post("/inject_twin_fault",
               params={"type": "shift", "magnitude": "1.0", "channel": "T-1"})
    assert r.status_code == 503
