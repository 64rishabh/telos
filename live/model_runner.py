"""LSTM model runner for live inference.

Builds the same architecture as telemanom/telemanom/modeling.py (2x LSTM
with 80 units, dropout 0.3, dense to n_predictions, linear activation)
but with input dim = 1 (synthetic sinusoid), then loads weights from a
pre-trained .h5 file.
"""
from __future__ import annotations

import os
from typing import Optional

import numpy as np

# Suppress TF CPU-speedup warnings (same as telemanom).
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

from keras.models import Sequential, load_model
from keras.layers import LSTM, Dense, Activation, Dropout
from keras.losses import MeanSquaredError
from keras.optimizers import Adam


def build_baseline_arch(n_predictions: int, layers=(80, 80), dropout: float = 0.3):
    """Replicate telemanom/telemanom/modeling.py:77-92, with input dim = 1."""
    model = Sequential()
    model.add(
        LSTM(
            layers[0],
            input_shape=(None, 1),
            return_sequences=True,
        )
    )
    model.add(Dropout(dropout))
    model.add(LSTM(layers[1], return_sequences=False))
    model.add(Dropout(dropout))
    model.add(Dense(n_predictions))
    model.add(Activation("linear"))
    # Compile with explicit loss/optimizer classes (not string names) so
    # the saved .h5 round-trips through Keras 3's deserializer. Passing
    # loss="mse" makes Keras 3 store the string, and load_model then
    # fails with "Could not deserialize 'keras.metrics.mse'" because
    # keras.metrics.mse doesn't exist as a class.
    model.compile(loss=MeanSquaredError(), optimizer=Adam())
    return model


class LSTMRunner:
    """Loads a pre-trained .h5 and predicts the next n_predictions steps from
    a single window of l_s values.
    """

    def __init__(self, model_path: str, n_predictions: int, layers=(80, 80), dropout: float = 0.3):
        self.n_predictions = n_predictions
        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"No model at {model_path}. Run `python -m live.train_baseline` first."
            )
        # load_model restores the full architecture, so we don't need to
        # call build_baseline_arch. We do still reference it for tests that
        # want to build a fresh model before training.
        self.model = load_model(model_path)

    def predict(self, window: np.ndarray) -> np.ndarray:
        """Predict the next n_predictions values from a single window.

        Args:
            window: shape (l_s,) or (l_s, 1) — a 1-D or 2-D array of the
                last l_s values.

        Returns:
            np.ndarray of shape (n_predictions,).
        """
        w = np.asarray(window, dtype=np.float32)
        if w.ndim == 1:
            w = w.reshape(-1, 1)
        # model expects (batch, timesteps, features)
        w = w.reshape(1, w.shape[0], w.shape[1])
        y_hat = self.model.predict(w, verbose=0)
        # y_hat shape is (1, n_predictions)
        return np.asarray(y_hat).reshape(-1)
