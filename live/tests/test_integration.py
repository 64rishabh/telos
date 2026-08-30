"""End-to-end test: boot the FastAPI app on a free port, open a
WebSocket client, inject a sustained shift, and assert the stream emits
an `anomaly` message within a reasonable number of ticks.

We use uvicorn in a background thread and httpx to hit /inject, then
asyncio + websockets for the streaming client.
"""
import asyncio
import json
import math
import os
import socket
import tempfile
import threading
import time

import numpy as np
import pytest
import uvicorn

from live.model_runner import build_baseline_arch


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _train_quick_model(path: str) -> None:
    """Train a small LSTM for 5 epochs on synthetic data and save."""
    n = 1200
    freq = 0.05
    raw = np.array([math.sin(freq * i) for i in range(1, n + 1)], dtype=np.float32)
    l_s = 50
    n_pred = 5
    X, y = [], []
    for i in range(len(raw) - l_s - n_pred):
        X.append(raw[i : i + l_s].reshape(-1, 1))
        y.append(raw[i + l_s : i + l_s + n_pred])
    X = np.array(X)
    y = np.array(y)
    model = build_baseline_arch(n_predictions=n_pred, layers=(16, 16), dropout=0.0)
    # build_baseline_arch already compiles; train enough epochs that the
    # predictor actually learns the sinusoid (otherwise the error stream
    # flags noise and the test becomes brittle).
    model.fit(X, y, epochs=5, batch_size=32, verbose=0)
    model.save(path)


def _run_server(app, host: str, port: int) -> threading.Thread:
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)

    def _serve():
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


@pytest.mark.asyncio
async def test_inject_shift_triggers_anomaly_over_websocket():
    """End-to-end: train a quick model, boot the app, open a websocket,
    inject a shift, expect an `anomaly` message within ~500 ticks.
    """
    import httpx
    import websockets

    with tempfile.TemporaryDirectory() as tmp:
        model_path = os.path.join(tmp, "tiny.h5")
        _train_quick_model(model_path)

        # Import here so the app picks up the model_path.
        from live import ws_server
        app = ws_server.create_app(model_path=model_path)
        # The AppState's ErrorStream uses default min_history (2170). For
        # a fast test we override the error-stream's config and the
        # production-loop interval via env? We can't easily do that with
        # the factory, so we just give the test enough wall-clock time.
        port = _free_port()
        thread = _run_server(app, host="127.0.0.1", port=port)

        try:
            # Speed up the producer for the test by monkey-patching the
            # config *before* the startup task reads it. The state is
            # already created by create_app() with the default config.
            # Easiest: set tick_hz to a high value and shrink
            # min_history so we don't have to wait for 2170 ticks.
            live_state = app.state.live
            live_state.config.tick_hz = 50.0  # 50 ticks/sec
            live_state.config.min_history = 80
            live_state.config.batch_size = 10
            live_state.config.window_size = 8
            live_state.config.smoothing_perc = 0.5
            live_state.config.l_s = 20
            live_state.config.error_buffer = 5
            live_state.config.p = 0.1
            # Reconfigure the error stream with the same overrides.
            live_state.error_stream.config = live_state.config

            # Wait for the producer to drain into the error stream enough
            # to clear min_history. At 50 Hz, 200 ticks = 4s.
            deadline = time.time() + 8
            while time.time() < deadline:
                if live_state.error_stream.history >= 80:
                    break
                await asyncio.sleep(0.1)
            assert live_state.error_stream.history >= 80, "error stream did not warm up"

            # Now inject a sustained shift and start consuming the stream.
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"http://127.0.0.1:{port}/inject",
                    params={"type": "shift", "magnitude": "2.0"},
                )
                assert r.status_code == 200, r.text

            uri = f"ws://127.0.0.1:{port}/stream"
            async with websockets.connect(uri) as ws:
                got_anomaly = False
                deadline = time.time() + 30
                while time.time() < deadline and not got_anomaly:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("anomaly") is not None:
                        got_anomaly = True
                        break
                assert got_anomaly, "no anomaly message received within 30s"
        finally:
            # The test server runs uvicorn.Server in a daemon thread; on
            # process exit the thread dies. We rely on `daemon=True` for
            # cleanup. (uvicorn doesn't expose a clean stop without
            # sending a signal.)
            pass
