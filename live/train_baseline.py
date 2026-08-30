"""Train the baseline LSTM on synthetic sinusoid data and save to disk.

Run once before the demo:
    python -m live.train_baseline

Output:
    models/baseline.h5
"""
from __future__ import annotations

import os
import random

import numpy as np

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

from keras.callbacks import EarlyStopping, History
from keras.layers import LSTM, Dense, Activation, Dropout
from keras.models import Sequential

from . import config as cfg
from .generator import SyntheticGenerator
from .model_runner import build_baseline_arch


def _shape_for_lstm(values: np.ndarray, l_s: int, n_pred: int) -> tuple:
    """Convert a flat (T,) array into (X, y) windows for the LSTM.

    X[i] = values[i : i + l_s]            (shape (l_s, 1))
    y[i] = values[i + l_s : i + l_s + n_pred]   (shape (n_pred,))

    Mirrors the slice in telemanom/channel.py:55-67.
    """
    X, y = [], []
    for i in range(len(values) - l_s - n_pred):
        X.append(values[i : i + l_s])
        y.append(values[i + l_s : i + l_s + n_pred])
    X = np.array(X).reshape(-1, l_s, 1)
    y = np.array(y)
    return X, y


def main(model_path: str | None = None, n_ticks: int = 5000, seed: int = 0) -> str:
    config = cfg.load()
    if model_path is None:
        model_path = config.model_path
    os.makedirs(os.path.dirname(model_path), exist_ok=True)

    # Reproducible synthetic data.
    random.seed(seed)
    np.random.seed(seed)
    gen = SyntheticGenerator(
        amp=config.gen_amp,
        freq=config.gen_freq,
        noise_std=config.gen_noise_std,
        seed=seed,
    )
    # Don't use the queue; pure baseline.
    raw = np.array(
        [gen.amp * np.sin(gen.freq * i) + np.random.normal(0, gen.noise_std) for i in range(1, n_ticks + 1)],
        dtype=np.float32,
    )
    X, y = _shape_for_lstm(raw, config.l_s, config.n_predictions)
    print(f"[train_baseline] X shape={X.shape}  y shape={y.shape}")

    model = build_baseline_arch(
        n_predictions=config.n_predictions,
        layers=tuple(config.layers),
        dropout=config.dropout,
    )
    # build_baseline_arch already compiles; train_baseline re-compiles
    # with the configured metric/optimizer for the long training run.
    from keras.losses import MeanSquaredError
    from keras.optimizers import Adam
    model.compile(loss=MeanSquaredError(), optimizer=Adam())
    model.fit(
        X,
        y,
        batch_size=config.lstm_batch_size,
        epochs=config.epochs,
        validation_split=config.validation_split,
        callbacks=[
            History(),
            EarlyStopping(
                monitor="val_loss",
                patience=config.patience,
                min_delta=config.min_delta,
                verbose=1,
            ),
        ],
        verbose=1,
    )
    model.save(model_path)
    print(f"[train_baseline] saved {model_path}")
    return model_path


if __name__ == "__main__":
    main()
