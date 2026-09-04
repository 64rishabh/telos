"""D-17 bridge side: run_phase1_pipeline(progress_cb=...) passthrough.

B40 passthrough: events fired == 2 * n_steps * n_top_k, each tagged
     with its procedure value so the 5 parallel sims are attributable.
     Snapshot carries battery_soc / battery_temp_c / payload_temp_c
     (the fields the WS server reads for sim_progress).
B41 snapshot fields: every event's snapshot has the WS-read fields.
B42 no-callback legacy path unchanged (T21 covers shape; here we assert
     the verdict is identical with and without a callback).
"""
from __future__ import annotations

H, DT = 600.0, 120.0


def test_B40_bridge_forwards_progress_events():
    from live.twin_bridge import run_phase1_pipeline
    events = []
    out = run_phase1_pipeline(fault_type="thermal_heater_stuck_on",
                              fault_params={}, alerts=[],
                              channel_hint="T-1",
                              horizon_s=H, dt_s=DT,
                              progress_cb=lambda p, s, t, snap: events.append(
                                  (p, s, t, dict(snap))))
    assert out is not None
    # D-19: the bridge now returns {"proposal": ..., "runbook": ...};
    # the candidates_ranked lives under "proposal".
    proposal = out["proposal"]
    n = len(proposal["candidates_ranked"])
    assert n >= 1
    assert len(events) == 2 * int(H / DT) * n, \
        f"expected {2 * int(H / DT) * n} events, got {len(events)}"
    procs = {e[0] for e in events}
    assert procs == {c["procedure"] for c in proposal["candidates_ranked"]}


def test_B41_event_snapshots_carry_ws_fields():
    from live.twin_bridge import run_phase1_pipeline
    snaps = []
    run_phase1_pipeline(fault_type="thermal_heater_stuck_on",
                        fault_params={}, alerts=[], channel_hint="T-1",
                        horizon_s=H, dt_s=DT,
                        progress_cb=lambda p, s, t, snap: snaps.append(dict(snap)))
    assert len(snaps) > 0
    for snap in snaps:
        for k in ("t_s", "battery_soc", "battery_temp_c", "payload_temp_c"):
            assert k in snap, f"snapshot missing {k}"


def test_B42_verdict_identical_with_and_without_callback():
    from live.twin_bridge import run_phase1_pipeline
    kw = dict(fault_type="thermal_heater_stuck_on", fault_params={},
              alerts=[], channel_hint="T-1", horizon_s=H, dt_s=DT)
    a = run_phase1_pipeline(**kw, progress_cb=None)
    b = run_phase1_pipeline(**kw, progress_cb=lambda *args: None)
    # D-19: bridge returns {"proposal": ..., "runbook": ...}; compare
    # the proposal blocks.
    pa, pb = a["proposal"], b["proposal"]
    assert pa["procedure"] == pb["procedure"]
    assert pa["risk_score"] == pb["risk_score"]
    assert pa["verdict"]["status"] == pb["verdict"]["status"]
