"""CLI entry point: load the trained model and start the live server.

Usage:
    python -m live.train_baseline   # one-time, before the demo
    python -m live.sim              # starts the server
"""
from __future__ import annotations

import argparse
import os

import uvicorn

from . import config as cfg


def main():
    parser = argparse.ArgumentParser(description="Live Telemanom streaming demo")
    parser.add_argument("--host", default=cfg.HOST)
    parser.add_argument("--port", type=int, default=cfg.PORT)
    parser.add_argument("--model", default=cfg.MODEL_PATH, help="path to trained .h5")
    args = parser.parse_args()

    # Propagate overrides via env so ws_server picks them up at import.
    os.environ["LIVE_MODEL_PATH"] = args.model

    print(f"[sim] model = {args.model}")
    print(f"[sim] dashboard: http://{args.host}:{args.port}/")
    print(f"[sim] websocket: ws://{args.host}:{args.port}/stream")
    print(f"[sim] inject an anomaly: POST /inject?type=spike&magnitude=1")
    uvicorn.run(
        "live.ws_server:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
