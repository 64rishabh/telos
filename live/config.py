"""Live demo configuration.

Reuses the same hyperparameters as telemanom/config.yaml where they apply,
but adds a few demo-specific knobs (tick rate, server host/port, model path).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

# Defaults match telemanom/config.yaml so the ported ErrorWindow pipeline
# behaves identically to the batch version.
L_S: int = 250              # input window length (timesteps fed to LSTM)
N_PREDICTIONS: int = 10     # how many steps ahead the LSTM predicts

# Thresholding parameters. The default BATCH_SIZE/WINDOW_SIZE values from
# telemanom/config.yaml are tuned for batch processing of long SMAP/MSL
# streams — for a live demo they produce a min_history of ~2200 ticks,
# which is an 18-minute warmup. The values below give a 600-tick warmup
# (~5 minutes at 2 Hz, or ~12s at 50 Hz), which is a much better demo UX
# while still leaving enough data for the threshold math to work.
BATCH_SIZE: int = 20
WINDOW_SIZE: int = 20
SMOOTHING_PERC: float = 0.10
ERROR_BUFFER: int = 30
P: float = 0.13

# LSTM architecture — must match train_baseline.py and the .h5 file.
LAYERS: List[int] = [80, 80]
DROPOUT: float = 0.3
LOSS_METRIC: str = "mse"
OPTIMIZER: str = "adam"
EPOCHS: int = 20
PATIENCE: int = 5
MIN_DELTA: float = 0.0003
VALIDATION_SPLIT: float = 0.2
LSTM_BATCH_SIZE: int = 64

# Synthetic data
GEN_AMP: float = 1.0
GEN_FREQ: float = 0.05
GEN_NOISE_STD: float = 0.1
TICK_HZ: float = 5.0        # ticks per second emitted on /stream

# Server
HOST: str = "0.0.0.0"
PORT: int = 8000

# Paths
MODEL_PATH: str = os.environ.get(
    "LIVE_MODEL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "models", "baseline.h5"),
)
OUT_DIR: str = os.path.join(os.path.dirname(__file__), "out")
ALERTS_JSONL: str = os.path.join(OUT_DIR, "alerts.jsonl")

# Number of EWMA-smoothed-error points we need before ErrorWindow becomes valid.
# Below this, tick() returns no alerts (instead of raising like the batch code).
MIN_HISTORY: int = BATCH_SIZE * (WINDOW_SIZE + 1)


@dataclass
class LiveConfig:
    l_s: int = L_S
    n_predictions: int = N_PREDICTIONS
    batch_size: int = BATCH_SIZE
    window_size: int = WINDOW_SIZE
    smoothing_perc: float = SMOOTHING_PERC
    error_buffer: int = ERROR_BUFFER
    p: float = P
    layers: List[int] = field(default_factory=lambda: list(LAYERS))
    dropout: float = DROPOUT
    loss_metric: str = LOSS_METRIC
    optimizer: str = OPTIMIZER
    epochs: int = EPOCHS
    patience: int = PATIENCE
    min_delta: float = MIN_DELTA
    validation_split: float = VALIDATION_SPLIT
    lstm_batch_size: int = LSTM_BATCH_SIZE
    gen_amp: float = GEN_AMP
    gen_freq: float = GEN_FREQ
    gen_noise_std: float = GEN_NOISE_STD
    tick_hz: float = TICK_HZ
    host: str = HOST
    port: int = PORT
    model_path: str = MODEL_PATH
    out_dir: str = OUT_DIR
    alerts_jsonl: str = ALERTS_JSONL
    min_history: int = MIN_HISTORY


def load() -> LiveConfig:
    """Return the default config. Hook left for env-var overrides later."""
    return LiveConfig()
