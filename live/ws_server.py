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
import queue
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as cfg
from .error_stream import ErrorStream
from .generator import SyntheticGenerator
from .model_runner import LSTMRunner
from .runbook_store import RunbookStore
from .twin_bridge import inject_fault, run_phase1_pipeline, CHANNEL_TO_SUBSYSTEM

logger = logging.getLogger("live.ws")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


STATIC_DIR = Path(__file__).parent / "static"


def _now_unix() -> float:
    """Single source of truth for current time (Unix seconds)."""
    return time.time()


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
        # Phase 1: an in-process twin FaultScheduler. The /inject_twin_fault
        # endpoint calls into it. The FaultScheduler is imported lazily
        # inside the inject handler so live/ stays pure-Python at boot.
        self.twin_scheduler = None   # type: ignore[assignment]
        self._twin_scheduler_factory = None  # set by init_twin()
        # Phase 1: the most recent Phase 1 pipeline result (Diagnose +
        # Propose + Verdict), keyed by fault_id. The broadcast message
        # carries twin_verdict when an alert tick matches a known fault.
        self.latest_verdict: Optional[dict] = None
        self.latest_fault_id: Optional[str] = None
        # Phase 1+ (parallel twin sims): a thread-safe queue of
        # per-step progress events from the worker threads running
        # validate_procedure() in parallel. The producer loop drains
        # this once per tick and attaches the most recent event to
        # the next tick message as the "sim_progress" field. Bounded
        # so a runaway sim can't OOM the process.
        self.sim_progress_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=2000)
        # D-19 runbook store: every /inject_twin_fault pushes a
        # runbook here. The frontend's /runbook page reads from it
        # across page refreshes.
        self.runbook_store: RunbookStore = RunbookStore()
        # The most recent runbook_id minted. The producer loop
        # surfaces it on the next alert tick so the dashboard can
        # link to the runbook page.
        self.latest_runbook_id: Optional[str] = None

    def shutdown(self) -> None:
        self.running = False
        try:
            self._alert_log.close()
        except Exception:
            pass

    def init_twin(self) -> None:
        """Initialize the in-process twin FaultScheduler. Called from
        create_app() so the live server boots even when the CHESS venv
        is unavailable — in that case inject_twin_fault returns 503."""
        # The `twin` package lives at <repo>/digital-twin/, which is not
        # on sys.path by default when this module is run as
        # `python -m uvicorn live.ws_server:app`. Bridge code calls
        # `_ensure_twin_on_path()` at use time; we do the same here so
        # the FaultScheduler import resolves whether the server is
        # booted from the repo root (system python) or the CHESS venv.
        from .twin_bridge import _ensure_twin_on_path
        _ensure_twin_on_path()
        try:
            from twin.fault_injection import FaultScheduler
        except Exception as e:
            logger.warning("[ws_server] twin import failed at init: %s", e)
            self.twin_scheduler = None
            return
        self.twin_scheduler = FaultScheduler()
        logger.info("[ws_server] twin FaultScheduler initialized")


def create_app(model_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Live Telemanom Demo")
    state = AppState(model_path=model_path)
    # Phase 1: initialize the in-process twin FaultScheduler. If the
    # CHESS venv isn't on PYTHONPATH, the scheduler stays None and
    # /inject_twin_fault returns 503; the rest of the live server
    # (LSTM detector, /inject, /stream) keeps working.
    state.init_twin()
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

    @app.post("/inject_twin_fault")
    async def inject_twin_fault(request: Request):
        """Phase 1: translate an HTTP injection into a twin FaultScheduler
        entry and run the Diagnose -> Propose -> Verdict pipeline.

        Query params (all optional except kind):
          - kind: "spike" | "shift" | "dropout" (default "spike")
          - magnitude: float (default 1.0; scaled into the fault param)
          - channel: "P-1" | "P-2" | "B-1" | "T-1" | "T-2" | "A-1" | "G-1" | "D-1" | None
          - fault_type: pass through to FaultScheduler.inject() directly
            (skips the kind->fault_type mapping table)
          - horizon_s: float (default 3600 = 1h) for Propose validation
          - dt_s: float (default 120 = 2 min) for Propose validation

        Returns:
          {"ok": True, "fault_id": "F-001", "fault_type": "...",
           "kind": ..., "channel": ..., "magnitude": ...,
           "verdict": {...} | None}

        The verdict is computed at injection time (predictive, per
        BIBLE §2). It is also stashed in state.latest_verdict and
        included in the next WebSocket broadcast that has an alert.
        """
        if state.twin_scheduler is None:
            raise HTTPException(
                503,
                "Twin FaultScheduler not initialized (CHESS venv missing?). "
                "Run the live server with the CHESS venv active to enable "
                "/inject_twin_fault."
            )
        kind = request.query_params.get("type", "spike")
        if kind not in ("spike", "shift", "dropout"):
            raise HTTPException(400, f"type must be spike|shift|dropout, got {kind!r}")
        try:
            magnitude = float(request.query_params.get("magnitude", "1.0"))
        except ValueError:
            raise HTTPException(400, "magnitude must be a float")
        channel = request.query_params.get("channel") or None
        try:
            horizon_s = float(request.query_params.get("horizon_s", "3600"))
            dt_s = float(request.query_params.get("dt_s", "120"))
        except ValueError:
            raise HTTPException(400, "horizon_s and dt_s must be floats")
        fault_type_override = request.query_params.get("fault_type") or None

        # Schedule the fault.
        if fault_type_override:
            try:
                fault_id = state.twin_scheduler.inject(
                    fault_type=fault_type_override,
                    start_t=0.0,
                    end_t=1200.0,        # 20 min default
                    parameters={},
                )
            except ValueError as e:
                # FaultScheduler rejects unknown fault types and
                # missing required parameters. Surface as 400.
                raise HTTPException(400, f"fault_type rejected: {e}")
            inject_resp = {
                "ok": True, "fault_id": fault_id,
                "fault_type": fault_type_override,
                "kind": kind, "channel": channel, "magnitude": magnitude,
            }
        else:
            try:
                inject_resp = inject_fault(
                    state.twin_scheduler, kind, magnitude, channel,
                )
            except ValueError as e:
                # map_injection_to_fault raises ValueError for
                # unknown (kind, channel) combinations. Surface as
                # 400 — the operator sent something we don't have
                # a bridge for.
                raise HTTPException(400, str(e))

        # Also push the legacy synthetic generator so the LSTM detector
        # has something to flag on the next tick. (Without this, the
        # detector sees a clean sinusoid and never produces an alert
        # to match the verdict we're about to compute.)
        if channel is None:
            state.generator.inject(kind, magnitude)

        # Run the pipeline. We pass an empty alert list here because
        # the pipeline is predictive (per BIBLE §2) — we run it on
        # the fault's expected behavior, not on the (not-yet-detected)
        # LSTM alerts. The next tick's alerts will then trigger the
        # broadcast of the stashed verdict.
        #
        # progress_cb is built fresh per request; it pushes
        # per-step events from the parallel sim worker threads into
        # the AppState queue, which the producer loop drains once
        # per tick and attaches to the next tick message.
        def _make_progress_cb() -> Callable[[str, int, int, dict], None]:
            def cb(
                procedure: str,
                step: int,
                total: int,
                snapshot: dict,
            ) -> None:
                msg = {
                    "procedure": procedure,
                    "step": step,
                    "total": total,
                    "t_s": float(snapshot.get("t_s", 0.0)),
                    "battery_soc": float(snapshot.get("battery_soc", 0.0)),
                    "battery_temp_c": float(snapshot.get("battery_temp_c", 0.0)),
                    "payload_temp_c": float(snapshot.get("payload_temp_c", 0.0)),
                }
                try:
                    state.sim_progress_queue.put_nowait(msg)
                except queue.Full:
                    # Drop the oldest event to make room. This is
                    # defensive — the queue only fills if a sim is
                    # 10x the normal horizon. The producer loop
                    # always reads from the queue, so the drop is
                    # self-healing.
                    try:
                        state.sim_progress_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        state.sim_progress_queue.put_nowait(msg)
                    except queue.Full:
                        pass
            return cb

        verdict = run_phase1_pipeline(
            fault_type=inject_resp["fault_type"],
            fault_params={},   # bridge handled the params already
            alerts=[],
            channel_hint=channel,
            horizon_s=horizon_s, dt_s=dt_s,
            progress_cb=_make_progress_cb(),
            fault_id=inject_resp["fault_id"],
            injection_meta={
                "kind": kind,
                "channel": channel,
                "magnitude": magnitude,
                "horizon_s": horizon_s,
                "dt_s": dt_s,
                "injected_at_unix": _now_unix(),
            },
        )
        if verdict is not None:
            proposal_dict = verdict.get("proposal")
            runbook = verdict.get("runbook")
            if proposal_dict is not None:
                state.latest_verdict = proposal_dict
                state.latest_fault_id = inject_resp["fault_id"]
                inject_resp["verdict"] = proposal_dict
            # Persist the runbook (if the builder succeeded) and
            # stamp its id on both the inject response and the
            # latest_runbook_id (so the WS producer loop can attach
            # it to the next alert tick).
            runbook_id = None
            if runbook is not None:
                runbook_id = state.runbook_store.add(runbook)
                state.latest_runbook_id = runbook_id
            inject_resp["runbook_id"] = runbook_id
        else:
            inject_resp["verdict"] = None
            inject_resp["runbook_id"] = None

        logger.info(
            "[inject_twin_fault] %s -> %s verdict=%s runbook_id=%s",
            inject_resp["fault_id"], inject_resp["fault_type"],
            (verdict or {}).get("verdict", {}).get("status") if verdict else "NONE",
            inject_resp.get("runbook_id"),
        )
        return JSONResponse(inject_resp)

    @app.get("/inject")
    async def inject_get(request: Request):
        return await inject_endpoint(request)

    # ----------------------------------------------------------------
    # Runbook API (D-19)
    # ----------------------------------------------------------------
    # Three endpoints, all read-only:
    #   GET /api/runbooks                     -> list (most recent first)
    #   GET /api/runbooks/{runbook_id}        -> full payload
    #   GET /api/runbooks/{runbook_id}/trajectory -> just the 3-field arrays
    # The store is in-process; these endpoints are local to the live
    # server. Phase 2+ will mirror writes to a durable store.

    @app.get("/api/runbooks")
    async def list_runbooks(limit: int = Query(20, ge=1)):
        """List recent runbooks (most recent first).

        Each entry is a summary view (no trajectory, no symptom window).
        The full payload is fetched by id.
        """
        from .runbook_builder import summary_view
        entries = state.runbook_store.list_recent(limit=limit)
        return JSONResponse([summary_view(e) for e in entries])

    @app.get("/api/runbooks/{runbook_id}")
    async def get_runbook(runbook_id: str):
        """Fetch the full runbook payload by id. 404 if not found."""
        rb = state.runbook_store.get(runbook_id)
        if rb is None:
            raise HTTPException(404, f"runbook {runbook_id} not found")
        return JSONResponse(rb)

    @app.get("/api/runbooks/{runbook_id}/trajectory")
    async def get_runbook_trajectory(runbook_id: str):
        """Lightweight endpoint for the trajectory canvases.

        Returns just the 3-field arrays (battery_soc, battery_temp_c,
        payload_temp_c) with baseline+predicted series. Lets the
        frontend poll the chart data without re-downloading the full
        payload on every tick.
        """
        from .runbook_builder import trajectory_view
        rb = state.runbook_store.get(runbook_id)
        if rb is None:
            raise HTTPException(404, f"runbook {runbook_id} not found")
        return JSONResponse(trajectory_view(rb))

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
        # Drain the most recent twin sim progress event (if any).
        # The producer runs at 5 Hz; the parallel sims may push
        # dozens of events between ticks. We surface only the
        # most recent — the frontend uses it to render a single
        # "this sim is at step N/M" line. If the user wants to
        # see every event, they shorten dt_s or raise tick_hz.
        sim_progress: Optional[dict] = None
        while True:
            try:
                sim_progress = state.sim_progress_queue.get_nowait()
            except queue.Empty:
                break
        msg = {
            "t": state.error_stream.t,
            "value": float(tick.value),
            "pred": float(pred),
            "error": state.error_stream.latest_error(),
            "injected": bool(tick.injected),
            "anomaly": None,
            "twin_verdict": None,
            "sim_progress": sim_progress,
            "runbook_id": state.latest_runbook_id,
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
            # Phase 1: if we have a stashed verdict from a recent
            # /inject_twin_fault call, surface it on this alert tick.
            # The dashboard / integration test then sees:
            #   anomaly detected -> twin has predicted a recovery.
            if state.latest_verdict is not None:
                msg["twin_verdict"] = state.latest_verdict
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
