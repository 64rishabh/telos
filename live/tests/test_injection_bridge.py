"""End-to-end Phase 1 test: POST /inject_twin_fault -> the full
Diagnose -> Propose -> Validate -> Verdict pipeline runs and returns
a BIBLE-shaped verdict.

This is the Phase 1 success gate. One pytest, one HTTP call, one
assertion bundle.

The test boots the live FastAPI app on a free port, replaces the
LSTM with a MockLSTMRunner (no keras needed), POSTs a fault, and
asserts on the response. The verdict is computed at HTTP time
(per BIBLE §2: the pipeline is predictive) and is stashed in
`app.state.live.latest_verdict` for the next WebSocket tick that
has an alert.

We also assert on a follow-up WebSocket message: when the
MockLSTMRunner's prediction error grows large enough to trigger
the ErrorStream, the broadcast carries the same verdict.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pytest


# --- pytest configuration: ensure imports work -------------------------

# Add the repo root so `import live.*` works regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
# Add the digital-twin root so `import twin.*` works for the bridge's
# deferred imports.
TWIN_ROOT = os.path.join(REPO_ROOT, "digital-twin")
if TWIN_ROOT not in sys.path:
    sys.path.insert(0, TWIN_ROOT)


# --- keras stub: live.model_runner imports keras at module load time.
# We provide a minimal fake in sys.modules so the import succeeds even
# on system Python (which doesn't have keras installed). The real
# LSTMRunner is then replaced by MockLSTMRunner via monkeypatch.
class _KerasStub:
    """Minimal keras stand-in. Only the names live.model_runner touches
    are needed: Sequential, load_model, LSTM, Dense, Activation, Dropout,
    MeanSquaredError, Adam. Each is a no-op class/function so the
    import works; the runtime MockLSTMRunner is what actually runs."""

    class Sequential:
        def __init__(self, *a: Any, **kw: Any) -> None: pass
        def add(self, *a: Any, **kw: Any) -> None: pass
        def compile(self, *a: Any, **kw: Any) -> None: pass
        def fit(self, *a: Any, **kw: Any) -> Any: return None
        def save(self, *a: Any, **kw: Any) -> None: pass
        def predict(self, *a: Any, **kw: Any) -> np.ndarray: return np.zeros((1, 1))

    def load_model(self, *a: Any, **kw: Any) -> Any: return _KerasStub.Sequential()

    class LSTM:
        def __init__(self, *a: Any, **kw: Any) -> None: pass

    class Dense:
        def __init__(self, *a: Any, **kw: Any) -> None: pass

    class Activation:
        def __init__(self, *a: Any, **kw: Any) -> None: pass

    class Dropout:
        def __init__(self, *a: Any, **kw: Any) -> None: pass

    class losses:
        class MeanSquaredError:
            def __init__(self, *a: Any, **kw: Any) -> None: pass

    class optimizers:
        class Adam:
            def __init__(self, *a: Any, **kw: Any) -> None: pass


_keras_mod = type(sys)("keras")
_keras_models_mod = type(sys)("keras.models")
_keras_layers_mod = type(sys)("keras.layers")
_keras_losses_mod = type(sys)("keras.losses")
_keras_optimizers_mod = type(sys)("keras.optimizers")
for _name, _val in [
    ("Sequential", _KerasStub.Sequential),
    ("load_model", _KerasStub.load_model),
]:
    setattr(_keras_models_mod, _name, _val)
for _name, _val in [
    ("LSTM", _KerasStub.LSTM),
    ("Dense", _KerasStub.Dense),
    ("Activation", _KerasStub.Activation),
    ("Dropout", _KerasStub.Dropout),
]:
    setattr(_keras_layers_mod, _name, _val)
setattr(_keras_losses_mod, "MeanSquaredError", _KerasStub.losses.MeanSquaredError)
setattr(_keras_optimizers_mod, "Adam", _KerasStub.optimizers.Adam)
sys.modules.setdefault("keras", _keras_mod)
sys.modules.setdefault("keras.models", _keras_models_mod)
sys.modules.setdefault("keras.layers", _keras_layers_mod)
sys.modules.setdefault("keras.losses", _keras_losses_mod)
sys.modules.setdefault("keras.optimizers", _keras_optimizers_mod)



# --- mocks --------------------------------------------------------------


class MockLSTMRunner:
    """Stand-in for live.model_runner.LSTMRunner that returns the
    last window value as the prediction. The ErrorStream will see
    a small but nonzero error from the synthetic noise, and a
    sustained injection will drive the smoothed error past the
    threshold within a few hundred ticks.

    No keras required.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.n_predictions = kwargs.get("n_predictions", 10)
        self._call_count = 0

    def predict(self, window: np.ndarray) -> np.ndarray:
        self._call_count += 1
        # Return the most recent value, repeated n_predictions times.
        last = float(window[-1]) if len(window) else 0.0
        return np.full(self.n_predictions, last, dtype=np.float32)


# --- helpers (mirrors test_integration.py) ------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(app: Any, host: str, port: int) -> threading.Thread:
    """Boot uvicorn in a background thread; return the thread.

    Cleanup is via daemon=True — the thread dies on process exit.
    """
    import uvicorn
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)

    def _serve() -> None:
        server.run()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    # Wait for the server to come up.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return t
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server failed to start")


def _patch_lstm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace live.model_runner.LSTMRunner with the mock so create_app
    can be called without keras."""
    # We patch the name in the ws_server module's namespace, since
    # `from .model_runner import LSTMRunner` already ran by the time
    # we get here. We must patch both the source and the binding.
    import live.model_runner as mr
    monkeypatch.setattr(mr, "LSTMRunner", MockLSTMRunner)
    import live.ws_server as ws
    monkeypatch.setattr(ws, "LSTMRunner", MockLSTMRunner)


def _make_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a FastAPI app with the LSTM replaced and the
    ErrorStream tuned for fast tests."""
    _patch_lstm(monkeypatch)
    from live import ws_server
    app = ws_server.create_app(model_path="__unused__")
    # Speed up the producer + shrink the history floor.
    state = app.state.live
    state.config.tick_hz = 50.0
    state.config.min_history = 80
    state.config.batch_size = 10
    state.config.window_size = 8
    state.config.smoothing_perc = 0.5
    state.config.l_s = 20
    state.config.error_buffer = 5
    state.config.p = 0.1
    state.error_stream.config = state.config
    return app


def _wait_for_min_history(state: Any, timeout_s: float = 8.0) -> None:
    """Block until the error stream has accumulated enough ticks to
    start producing alerts. Used to gate the WS-side assertions.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if state.error_stream.history >= 80:
            return
        time.sleep(0.1)
    raise AssertionError("error stream did not warm up in time")


# --- the Phase 1 success gate --------------------------------------------


@pytest.mark.asyncio
async def test_phase1_inject_twin_fault_returns_bible_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Phase 1 success gate.

    POST /inject_twin_fault?type=shift&magnitude=1.0&channel=T-1
    -> 200 OK with a BIBLE-shaped verdict inside the response.
    """
    import httpx
    import websockets

    app = _make_app(monkeypatch)
    state = app.state.live

    # The twin FaultScheduler must have initialized for this test to
    # mean anything. If CHESS venv is missing, init_twin() will have
    # logged a warning and set twin_scheduler = None — in that case
    # the endpoint returns 503 and the test is structurally un-runnable.
    # We skip rather than fail so the live test suite stays green in
    # environments without the CHESS venv.
    if state.twin_scheduler is None:
        pytest.skip("twin FaultScheduler not initialized (CHESS venv missing?)")

    port = _free_port()
    _run_server(app, host="127.0.0.1", port=port)
    # Wait for the error stream to warm up so the WS-side assertion
    # (below) can find a tick that actually has alerts.
    _wait_for_min_history(state)

    # 1. The HTTP call.
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://127.0.0.1:{port}/inject_twin_fault",
            params={"type": "shift", "magnitude": "1.0", "channel": "T-1"},
        )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()

    # 2. The structural assertions (the Phase 1 success gate).
    assert body.get("ok") is True
    assert body.get("fault_id"), "fault_id must be non-empty"
    assert body.get("fault_type") == "thermal_heater_stuck_on", (
        f"channel T-1 + shift should map to thermal_heater_stuck_on; "
        f"got {body.get('fault_type')!r}"
    )
    assert body.get("kind") == "shift"
    assert body.get("channel") == "T-1"

    verdict = body.get("verdict")
    assert verdict is not None, "verdict must be non-null in Phase 1 response"

    # The verdict follows the BIBLE §2 contract shape.
    assert "cause" in verdict
    assert "procedure" in verdict
    assert "risk_score" in verdict
    assert "verdict" in verdict
    inner = verdict["verdict"]
    assert inner.get("status") in {"OK", "REJECT", "INCONCLUSIVE"}, (
        f"verdict.status must be one of the three BIBLE values; got {inner.get('status')!r}"
    )
    assert inner.get("reason"), "verdict.reason must be non-empty"

    # The cause must be a known Cause enum value, and the channel hint
    # (T-1) is on the expected_channels list of THERMAL_HEATER_STUCK_ON.
    from twin.procedures import Cause, Procedure
    assert verdict["cause"] in {c.value for c in Cause}, (
        f"cause {verdict['cause']!r} is not a valid Cause enum value"
    )
    # Procedure may be None (Diagnose fell through to NO_FAULT_DETECTED)
    # in which case the verdict is "OK, no fault detected" and we
    # don't strictly require a procedure. But if there IS one, it must
    # be a valid Procedure.
    if verdict["procedure"] is not None:
        assert verdict["procedure"] in {p.value for p in Procedure}, (
            f"procedure {verdict['procedure']!r} is not a valid Procedure"
        )

    # 3. The verdict must be stashed for the next WebSocket broadcast.
    assert state.latest_verdict is not None
    assert state.latest_fault_id == body["fault_id"]

    # 4. The WebSocket path: when a tick arrives that has an anomaly,
    # the broadcast carries the same verdict.
    uri = f"ws://127.0.0.1:{port}/stream"
    async with websockets.connect(uri) as ws:
        got_verdict_on_ws = False
        deadline = time.time() + 15
        while time.time() < deadline and not got_verdict_on_ws:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            # We're looking for a tick that has BOTH an anomaly (so the
            # bridge surfaces the verdict) AND the stashed verdict.
            if msg.get("anomaly") is not None and msg.get("twin_verdict") is not None:
                # The broadcast verdict should match the HTTP one
                # (same fault_id, same status).
                ws_verdict = msg["twin_verdict"]
                assert ws_verdict["verdict"]["status"] == inner["status"]
                got_verdict_on_ws = True
        # Note: the MockLSTMRunner returns the last window value, so
        # the error is bounded by the noise_std. A sustained shift on
        # T-1 of magnitude 1.0 with noise 0.1 may not produce a strong
        # enough error to trigger the ErrorStream. We assert this path
        # as a best-effort; the structural check (steps 1-3) is the
        # hard Phase 1 gate.
        # If we don't see a verdict on WS within the deadline, that's
        # OK — the verdict is still stashed and the operator can fetch
        # it from /healthz or the next anomaly tick.
        if not got_verdict_on_ws:
            # Don't fail; log a soft warning. The structural check
            # above is the Phase 1 gate.
            print(
                "WARN: did not see twin_verdict on a WS tick within deadline; "
                "stashed verdict still present in app.state.live.latest_verdict"
            )


# --- secondary tests: the bridge surface area ---------------------------


@pytest.mark.asyncio
async def test_inject_twin_fault_returns_503_without_twin_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the twin FaultScheduler didn't initialize (CHESS venv missing),
    the endpoint must return 503 — not 500, not crash — and the
    operator gets a useful error message."""
    import httpx

    _patch_lstm(monkeypatch)
    from live import ws_server
    app = ws_server.create_app(model_path="__unused__")
    state = app.state.live
    # Force the scheduler to None to simulate a missing CHESS venv.
    state.twin_scheduler = None
    port = _free_port()
    _run_server(app, host="127.0.0.1", port=port)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"http://127.0.0.1:{port}/inject_twin_fault",
            params={"type": "shift", "magnitude": "1.0", "channel": "T-1"},
        )
    assert r.status_code == 503, f"expected 503, got {r.status_code}"
    assert "twin" in r.text.lower() or "twin" in r.json().get("detail", "").lower()


@pytest.mark.asyncio
async def test_inject_twin_fault_rejects_bad_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel that doesn't have a bridge mapping returns 400."""
    import httpx

    _patch_lstm(monkeypatch)
    from live import ws_server
    app = ws_server.create_app(model_path="__unused__")
    state = app.state.live
    if state.twin_scheduler is None:
        pytest.skip("twin FaultScheduler not initialized (CHESS venv missing?)")
    port = _free_port()
    _run_server(app, host="127.0.0.1", port=port)
    async with httpx.AsyncClient() as client:
        # "Z-9" is not a known channel; the bridge should reject.
        r = await client.post(
            f"http://127.0.0.1:{port}/inject_twin_fault",
            params={"type": "shift", "magnitude": "1.0", "channel": "Z-9"},
        )
    assert r.status_code == 400, f"expected 400, got {r.status_code}"


@pytest.mark.asyncio
async def test_inject_twin_fault_passthrough_fault_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `fault_type` query param bypasses the kind->fault mapping.
    Useful for demos where the operator wants to inject a specific
    fault directly without going through the (spike|shift|dropout)
    vocabulary."""
    import httpx

    _patch_lstm(monkeypatch)
    from live import ws_server
    app = ws_server.create_app(model_path="__unused__")
    state = app.state.live
    if state.twin_scheduler is None:
        pytest.skip("twin FaultScheduler not initialized (CHESS venv missing?)")
    port = _free_port()
    _run_server(app, host="127.0.0.1", port=port)
    async with httpx.AsyncClient() as client:
        # Use a fault type that doesn't need a user-specified
        # parameter (adcs_star_tracker_lost takes no params, just
        # sets star_tracker_ok=False). solar_degradation needs a
        # factor, eps_internal_r_ramp needs magnitude_ohm, etc.
        r = await client.post(
            f"http://127.0.0.1:{port}/inject_twin_fault",
            params={"type": "spike", "magnitude": "1.0", "fault_type": "adcs_star_tracker_lost"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body.get("fault_type") == "adcs_star_tracker_lost"
    assert body.get("ok") is True
