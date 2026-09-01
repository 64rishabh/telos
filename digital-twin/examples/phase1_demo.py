"""
Phase 1 end-to-end demo.

This script demonstrates the full Stage 1 -> Stage 4 loop IN-PROCESS:
  - Boot the live FastAPI app inside this script (no separate uvicorn
    subprocess) on a free port.
  - For each (kind, channel) demo pair, POST /inject_twin_fault over
    HTTP. The live server schedules the fault on the in-process twin
    FaultScheduler, then runs Diagnose -> Propose -> Validate and
    returns a BIBLE-shaped verdict in the HTTP response.
  - Open a WebSocket to /stream and consume a few ticks to show the
    verdict propagating onto the broadcast.
  - Print a roll-up table of every cause -> procedure -> verdict we
    observed.

This is the demo backbone for the hackathon. It replaces the stub-
based smoke_test.py for Phase 1 verification (smoke_test.py is kept
for regression).

Run with the CHESS venv active (or with system Python — see LSTM
note below):

    cd /home/rishabh/c0de/telos
    python digital-twin/examples/phase1_demo.py [--pause 1.0] [--read-ticks 50]

LSTM NOTE: the live server imports keras at startup (via
live.model_runner). This script provides a tiny keras stub via
sys.modules so it runs on a system Python that doesn't have keras
installed. The stub's LSTMRunner is a no-op predictor that always
returns the last value of the input window, so the ErrorStream
will flag sustained shifts within ~100 ticks. For the live demo
at the hackathon, the real .h5 model will be loaded instead.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
from typing import Any, Dict, List, Tuple

import numpy as np


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "digital-twin"))


# --- keras stub (so live.model_runner can be imported on system Python)
# This is the same pattern the integration test uses. The stub only
# needs to satisfy the import-time graph of live.model_runner; the
# actual LSTMRunner is replaced by MockLSTMRunner below.

class _KerasStub:
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


def _install_keras_stub() -> None:
    """Install a minimal keras stub in sys.modules so live.model_runner
    can be imported even when keras is not installed."""
    if "keras" in sys.modules and not isinstance(sys.modules.get("keras"), type(sys)):
        # Real keras is installed; don't stub it.
        return
    _keras_mod = type(sys)("keras")
    _keras_models_mod = type(sys)("keras.models")
    _keras_layers_mod = type(sys)("keras.layers")
    _keras_losses_mod = type(sys)("keras.losses")
    _keras_optimizers_mod = type(sys)("keras.optimizers")
    for n, v in [("Sequential", _KerasStub.Sequential), ("load_model", _KerasStub.load_model)]:
        setattr(_keras_models_mod, n, v)
    for n, v in [("LSTM", _KerasStub.LSTM), ("Dense", _KerasStub.Dense),
                  ("Activation", _KerasStub.Activation), ("Dropout", _KerasStub.Dropout)]:
        setattr(_keras_layers_mod, n, v)
    setattr(_keras_losses_mod, "MeanSquaredError", _KerasStub.losses.MeanSquaredError)
    setattr(_keras_optimizers_mod, "Adam", _KerasStub.optimizers.Adam)
    sys.modules["keras"] = _keras_mod
    sys.modules["keras.models"] = _keras_models_mod
    sys.modules["keras.layers"] = _keras_layers_mod
    sys.modules["keras.losses"] = _keras_losses_mod
    sys.modules["keras.optimizers"] = _keras_optimizers_mod


_install_keras_stub()


# --- demo scenarios ---------------------------------------------------

# (kind, channel, magnitude, description)
DEMO_SCENARIOS: List[Tuple[str, str, float, str]] = [
    ("shift",   "P-1", 1.0, "EPS bus voltage drop (internal_r degradation)"),
    ("shift",   "T-1", 1.0, "Thermal: payload heater stuck on"),
    ("dropout", "A-1", 1.0, "ADCS: star tracker lost (mask)"),
    ("dropout", "G-1", 1.0, "ADCS: wheel saturation (step)"),
    ("dropout", "D-1", 1.0, "Comms: ground station lost (mask)"),
    ("shift",   "T-2", 1.0, "Thermal: electronics heater stuck on"),
]


# --- server helpers ---------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_server(app: Any, host: str, port: int) -> threading.Thread:
    """Boot uvicorn in a daemon thread."""
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)

    def _serve() -> None:
        server.run()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return t
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server failed to start at {host}:{port}")


def _patch_lstm() -> None:
    """Replace live.model_runner.LSTMRunner with a no-op that always
    returns the last input value. Lets the ErrorStream flag injected
    shifts within ~100 ticks without a real model."""
    import live.model_runner as mr
    import live.ws_server as ws

    class MockLSTMRunner:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.n_predictions = kw.get("n_predictions", 10)

        def predict(self, window: np.ndarray) -> np.ndarray:
            last = float(window[-1]) if len(window) else 0.0
            return np.full(self.n_predictions, last, dtype=np.float32)

    mr.LSTMRunner = MockLSTMRunner
    ws.LSTMRunner = MockLSTMRunner


# --- pretty printing --------------------------------------------------


def _print_verdict_table(scenario: Tuple[str, str, float, str], body: Dict[str, Any]) -> None:
    kind, channel, magnitude, description = scenario
    print()
    print("=" * 72)
    print(f"  {description}")
    print("=" * 72)
    print(f"  Injected:    kind={kind}  channel={channel}  magnitude={magnitude}")
    print(f"  Fault ID:    {body.get('fault_id', '<none>')}")
    print(f"  Fault type:  {body.get('fault_type', '<none>')}")
    verdict = body.get("verdict")
    if not verdict:
        print("  Verdict:     <none>  (twin not initialized — CHESS venv missing?)")
        return
    print(f"  Cause:       {verdict.get('cause', '<none>')}")
    print(f"  Cause score: {verdict.get('cause_score', 0.0):.3f}")
    print(f"  Procedure:   {verdict.get('procedure', '<none>')}")
    print(f"  Risk score:  {verdict.get('risk_score', 0.0):.3f}")
    inner = verdict.get("verdict") or {}
    print(f"  Inner:       status={inner.get('status', '?')}")
    print(f"               reason={inner.get('reason', '')}")
    ranked = verdict.get("candidates_ranked") or []
    if ranked:
        print(f"  Ranking:     (best first)")
        for i, c in enumerate(ranked[:5]):
            print(f"               #{i+1}  {c.get('procedure'):30s}  risk={c.get('risk_score', 0.0):.3f}")


# --- stream consumer --------------------------------------------------


async def _consume_stream(host: str, port: int, n_ticks: int, timeout_s: float) -> List[Dict[str, Any]]:
    try:
        import websockets
    except ImportError:
        print("  (websockets module missing; skipping stream consumption)")
        return []
    uri = f"ws://{host}:{port}/stream"
    out: List[Dict[str, Any]] = []
    try:
        async with websockets.connect(uri) as ws:
            deadline = time.time() + timeout_s
            while len(out) < n_ticks and time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                out.append(msg)
    except Exception as e:
        print(f"  (stream read failed: {e})")
    return out


# --- main -------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    host = "127.0.0.1"
    port = args.port or _free_port()

    # Boot the live app in-process.
    _patch_lstm()
    import live.ws_server as ws_server
    app = ws_server.create_app(model_path="__unused__")
    # Speed up the producer + shrink the history floor for a snappy demo.
    state = app.state.live
    state.config.tick_hz = 25.0
    state.config.min_history = 80
    state.config.batch_size = 10
    state.config.window_size = 8
    state.config.smoothing_perc = 0.5
    state.config.l_s = 20
    state.config.error_buffer = 5
    state.config.p = 0.1
    state.error_stream.config = state.config

    if state.twin_scheduler is None:
        print("WARNING: twin FaultScheduler did not initialize. The HTTP")
        print("         /inject_twin_fault endpoint will return 503. The")
        print("         pipeline still runs Diagnose -> Propose -> Verdict")
        print("         via the in-process bridge if you call it directly.")

    _run_server(app, host, port)
    print(f"Live server up at http://{host}:{port}")

    import httpx
    async with httpx.AsyncClient() as client:
        r = await client.get(f"http://{host}:{port}/healthz")
        h = r.json()
        print(f"  /healthz: ok={h.get('ok')}, history={h.get('history')}, clients={h.get('clients')}")

    results: List[Dict[str, Any]] = []
    for scenario in DEMO_SCENARIOS:
        kind, channel, magnitude, description = scenario
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"http://{host}:{port}/inject_twin_fault",
                    params={
                        "type": kind,
                        "magnitude": str(magnitude),
                        "channel": channel,
                        "horizon_s": "600",
                        "dt_s": "60",
                    },
                    timeout=30.0,
                )
            if r.status_code != 200:
                print(f"\n  {description}: HTTP {r.status_code} {r.text}")
                continue
            body = r.json()
            _print_verdict_table(scenario, body)
            results.append({"scenario": scenario, "body": body})
        except Exception as e:
            print(f"\n  {description}: EXCEPTION {e!r}")
        await asyncio.sleep(args.pause)

    if args.read_ticks > 0:
        print()
        print("=" * 72)
        print(f"  Reading {args.read_ticks} stream tick(s) for the last injection")
        print("=" * 72)
        ticks = await _consume_stream(host, port, n_ticks=args.read_ticks, timeout_s=10.0)
        verdicts_seen = 0
        for t in ticks:
            v = t.get("twin_verdict")
            if v:
                verdicts_seen += 1
                print(
                    f"  t={t.get('t'):>5d}  "
                    f"value={t.get('value', 0.0):.3f}  "
                    f"pred={t.get('pred', 0.0):.3f}  "
                    f"err={t.get('error', 0.0):.3f}  "
                    f"verdict={v.get('cause')} -> {v.get('procedure')} "
                    f"[{v.get('verdict', {}).get('status', '?')}]"
                )
        if verdicts_seen == 0:
            print("  (no twin_verdict surfaced on the stream within the deadline;")
            print("   the ErrorStream may not have flagged enough anomaly to trigger")
            print("   the verdict surfacing. The HTTP responses above are the source of truth.)")

    # Roll-up
    print()
    print("=" * 72)
    print("  PHASE 1 ROLL-UP")
    print("=" * 72)
    print(f"  {'channel':<8s}  {'kind':<8s}  {'cause':<32s}  {'procedure':<32s}  {'verdict':<12s}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*32}  {'-'*32}  {'-'*12}")
    for r in results:
        sc = r["scenario"]
        b = r["body"]
        v = b.get("verdict") or {}
        cause = (v.get("cause") or "<none>")[:32]
        procedure = (v.get("procedure") or "<none>")[:32]
        inner_status = (v.get("verdict") or {}).get("status", "?")
        print(f"  {sc[1]:<8s}  {sc[0]:<8s}  {cause:<32s}  {procedure:<32s}  {inner_status:<12s}")

    print()
    print("Phase 1 demo complete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=0,
                        help="Port to bind the live server (default: pick a free one).")
    parser.add_argument("--pause", type=float, default=1.0,
                        help="Seconds to pause between scenarios (default: 1.0).")
    parser.add_argument("--read-ticks", type=int, default=0,
                        help="Read this many /stream ticks after the last injection (default: 0).")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
