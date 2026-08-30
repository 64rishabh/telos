"""Tests for LSTMRunner.

The model runner wraps a pre-trained .h5. To exercise the load+predict
path without making the test suite depend on `train_baseline.py` having
been run, we build a tiny model in-process, fit it for 2 epochs on
synthetic sinusoid data, save it to a temp file, and then load via
LSTMRunner.
"""
import math
import os
import tempfile

import numpy as np
import pytest

from live.model_runner import LSTMRunner, build_baseline_arch


@pytest.fixture(scope="module")
def tiny_trained_model():
    """Train a tiny LSTM for 2 epochs and return its path."""
    n = 600
    freq = 0.05
    amp = 1.0
    raw = np.array(
        [amp * math.sin(freq * i) for i in range(1, n + 1)], dtype=np.float32
    )
    l_s = 50
    n_pred = 5
    X, y = [], []
    for i in range(len(raw) - l_s - n_pred):
        X.append(raw[i : i + l_s].reshape(-1, 1))
        y.append(raw[i + l_s : i + l_s + n_pred])
    X = np.array(X)
    y = np.array(y)

    model = build_baseline_arch(n_predictions=n_pred, layers=(8, 8), dropout=0.0)
    # build_baseline_arch already compiles; this is redundant but safe.
    model.fit(X, y, epochs=2, batch_size=32, verbose=0)

    with tempfile.NamedTemporaryFile(suffix=".h5", delete=False) as f:
        path = f.name
    model.save(path)
    yield path
    os.unlink(path)


def test_load_predict(tiny_trained_model):
    runner = LSTMRunner(
        model_path=tiny_trained_model,
        n_predictions=5,
        layers=(8, 8),
        dropout=0.0,
    )
    # Build a clean sinusoid window.
    window = np.array(
        [math.sin(0.05 * i) for i in range(1, 51)], dtype=np.float32
    )
    pred = runner.predict(window)
    assert pred.shape == (5,)
    assert np.isfinite(pred).all()


def test_predict_accepts_1d_and_2d(tiny_trained_model):
    runner = LSTMRunner(
        model_path=tiny_trained_model, n_predictions=5, layers=(8, 8), dropout=0.0
    )
    w1 = np.arange(50, dtype=np.float32)
    w2 = w1.reshape(-1, 1)
    p1 = runner.predict(w1)
    p2 = runner.predict(w2)
    np.testing.assert_allclose(p1, p2, rtol=1e-6)


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        LSTMRunner(
            model_path=str(tmp_path / "nope.h5"),
            n_predictions=5,
            layers=(8, 8),
            dropout=0.0,
        )
