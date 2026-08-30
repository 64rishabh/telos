"""FastAPI server: WebSocket /stream + POST /inject + GET / (dashboard).

Architecture:
  - One in-process generator (synthetic telemetry).
  - One in-process consumer (LSTM + ErrorStream).
  - One asyncio task runs the producer loop, fanning out one JSON message
    per tick to every connected WebSocket.
  - HTTP /inject mutates the generator's injection queue. The producer
    loop will pick it up on the next tick.

For the demo the producer and consumer are in one process. To migrate to
Kafka, replace the SyntheticGenerator with a KafkaConsumer and add a
KafkaProducer AlertSink — the FastAPI surface stays the same.

This module exposes a `create_app(model_path=None)` factory so the
integration test can spin up an app pointing at a tmp-trained model
without env-var hackery.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import List, Optional, Set

import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from .error_stream import ErrorStream
from .generator import SyntheticGenerator
from .model_runner import LSTMRunner

logger = logging.getLogger("live.ws")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    def __init__(self, model_path: Optional[str] = None):
        self.config: cfg.LiveConfig = cfg.load()
        if model_path is not None:
            self.config.model_path = model_path
        self.generator = SyntheticGenerator(
            amp=self.config.gen_amp,
            freq=self.config.gen_freq,
            noise_std=self.config.gen_noise_std,
            seed=None,
        )
        self.model = LSTMRunner(
            model_path=self.config.model_path,
            n_predictions=self.config.n_predictions,
            layers=tuple(self.config.layers),
            dropout=self.config.dropout,
        )
        self.error_stream = ErrorStream(self.config)
        self.window: deque = deque(maxlen=self.config.l_s)
        for v in self.generator.prefill(self.config.l_s):
            self.window.append(float(v))
        self.clients: Set[WebSocket] = set()
        self.producer_task: Optional[asyncio.Task] = None
        self.running = False
        # JSONL alert sink
        os.makedirs(self.config.out_dir, exist_ok=True)
        self._alert_log = open(self.config.alerts_jsonl, "a", buffering=1)

    def shutdown(self) -> None:
        self.running = False
        try:
            self._alert_log.close()
        except Exception:
            pass


def create_app(model_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Live Telemanom Demo")
    state = AppState(model_path=model_path)
    app.state.live = state

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        index = STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(500, f"dashboard missing at {index}")
        return HTMLResponse(index.read_text())

    @app.post("/inject")
    async def inject_endpoint(request: Request):
        kind = request.query_params.get("type", "spike")
        try:
            magnitude = float(request.query_params.get("magnitude", "1.0"))
        except ValueError:
            raise HTTPException(400, "magnitude must be a float")
        if kind not in ("spike", "shift", "dropout"):
            raise HTTPException(400, f"type must be spike|shift|dropout, got {kind!r}")
        state.generator.inject(kind, magnitude)
        logger.info(
            "[inject] kind=%s magnitude=%.2f queue=%d",
            kind, magnitude, state.generator.pending(),
        )
        return JSONResponse(
            {"ok": True, "kind": kind, "magnitude": magnitude, "queue": state.generator.pending()}
        )

    @app.get("/inject")
    async def inject_get(request: Request):
        return await inject_endpoint(request)

    async def _broadcast(message: dict) -> None:
        if not state.clients:
            return
        text = json.dumps(message)
        dead: List[WebSocket] = []
        for ws in list(state.clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            state.clients.discard(ws)

    def _predict_aggregate(window: np.ndarray, n_pred: int) -> float:
        y_hat = state.model.predict(window)
        return float(y_hat[0])

    def _step_once() -> dict:
        tick = state.generator.next()
        state.window.append(float(tick.value))
        window_arr = np.array(list(state.window), dtype=np.float32)
        pred = _predict_aggregate(window_arr, state.config.n_predictions)
        alerts = state.error_stream.tick(actual=float(tick.value), predicted=pred)
        msg = {
            "t": state.error_stream.t,
            "value": float(tick.value),
            "pred": float(pred),
            "error": state.error_stream.latest_error(),
            "injected": bool(tick.injected),
            "anomaly": None,
        }
        if alerts:
            latest = max(alerts, key=lambda a: a.score)
            msg["anomaly"] = {"score": latest.score, "seq": list(latest.seq)}
            for a in alerts:
                logger.info(
                    "[ALERT] t=%d score=%.3f seq=(%d,%d)",
                    a.t, a.score, a.seq[0], a.seq[1],
                )
                state._alert_log.write(
                    json.dumps({"t": a.t, "score": a.score, "seq": list(a.seq)}) + "\n"
                )
        return msg

    async def _producer_loop() -> None:
        interval = 1.0 / state.config.tick_hz
        state.running = True
        try:
            while state.running:
                loop = asyncio.get_running_loop()
                msg = await loop.run_in_executor(None, _step_once)
                await _broadcast(msg)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    @app.on_event("startup")
    async def on_startup():
        if state.producer_task is None or state.producer_task.done():
            state.producer_task = asyncio.create_task(_producer_loop())
            logger.info(
                "[ws_server] producer started; tick_hz=%.2f model=%s",
                state.config.tick_hz, state.config.model_path,
            )

    @app.on_event("shutdown")
    async def on_shutdown():
        state.shutdown()
        if state.producer_task:
            state.producer_task.cancel()

    @app.websocket("/stream")
    async def stream_ws(ws: WebSocket):
        await ws.accept()
        state.clients.add(ws)
        logger.info("[ws] client connected; total=%d", len(state.clients))
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.discard(ws)
            logger.info("[ws] client disconnected; total=%d", len(state.clients))

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "history": state.error_stream.history,
            "clients": len(state.clients),
            "running": state.running,
        }

    return app


# Module-level app for `uvicorn live.ws_server:app`. Tests should call
# create_app() instead.
app = create_app()
