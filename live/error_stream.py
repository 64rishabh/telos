"""Streaming EWMA error buffer + ported ErrorWindow pipeline.

The telemanom/errors.py code is fundamentally batch:
  1. run the whole LSTM over the whole test set,
  2. compute e = |y_hat - y|,
  3. EWMA-smooth the error,
  4. walk sliding windows of length `window_size * batch_size` over the
     smoothed error and for each window pick a z-score threshold that
     maximizes (mean_decrease + sd_decrease) / (n_sequences^2 + n_anom),
  5. prune + score anomalies.

For live streaming, we keep the *exact* thresholding / pruning / scoring
math but call it on the trailing window of the EWMA buffer each time the
buffer advances by one tick. The anomaly indices we return are positions
in the **live** stream (not the y_test array), so the dashboard can plot
them directly.

See:
  telemanom/telemanom/errors.py:269  find_epsilon
  telemanom/telemanom/errors.py:324  compare_to_epsilon
  telemanom/telemanom/errors.py:386  prune_anoms
  telemanom/telemanom/errors.py:437  score_anomalies
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

import more_itertools as mit
import numpy as np
import pandas as pd

from . import config as cfg


# -- alert event ---------------------------------------------------------


@dataclass
class AlertEvent:
    t: int                       # live tick index (0-based, the first tick emitted on /stream)
    score: float                 # severity score (see score_anomalies below)
    seq: tuple                   # (start_t, end_t) in live stream indices
    kind: str = "anomaly"        # future-proof: "anomaly" | "shift" | etc.


# -- ErrorWindow: same math as telemanom/errors.py, callable as a pure fn -
# Adapted from telemanom/errors.py:171. The only meaningful change is that
# `prior_idx` and the indices in `i_anom` are interpreted as live-stream
# indices (not y_test indices), and the +l_s shift in
# Errors.process_batches (telemanom/errors.py:165) is no longer applied.


class ErrorWindow:
    """Mirrors telemanom/errors.py:171. Pure function of its inputs."""

    def __init__(
        self,
        e_s: np.ndarray,
        y_test: np.ndarray,
        config,
        prior_idx: int,
        window_num: int,
        already_known: np.ndarray,
    ):
        self.e_s = e_s
        self.config = config
        self.anom_scores: List[dict] = []
        self.window_num = window_num

        self.sd_lim = 12.0
        self.sd_threshold = self.sd_lim
        self.sd_threshold_inv = self.sd_lim

        self.i_anom = np.array([])
        self.i_anom_inv = np.array([])
        self.non_anom_max = -1_000_000.0
        self.non_anom_max_inv = -1_000_000.0
        self.E_seq: list = []
        self.E_seq_inv: list = []

        self.mean_e_s = float(np.mean(e_s))
        self.sd_e_s = float(np.std(e_s))
        self.e_s_inv = np.array(
            [self.mean_e_s + (self.mean_e_s - e) for e in e_s]
        )
        self.epsilon = self.mean_e_s + self.sd_lim * self.sd_e_s
        self.epsilon_inv = self.mean_e_s + self.sd_lim * self.sd_e_s

        self.y_test = y_test
        self.sd_values = float(np.std(y_test))
        self.perc_high, self.perc_low = np.percentile(y_test, [95, 5])
        self.inter_range = float(self.perc_high - self.perc_low)

        # Skip leading values until we have enough history; the original
        # uses l_s*2 (telemanom/errors.py:262).
        self.num_to_ignore = config.l_s * 2
        if len(y_test) < 2500:
            self.num_to_ignore = config.l_s
        if len(y_test) < 1800:
            self.num_to_ignore = 0

        # Carry the running set of already-known anomaly indices so we
        # don't double-count the same point across windows.
        self._already_known = already_known

    # -- the four pipeline stages, ported verbatim -------------------------

    def find_epsilon(self, inverse: bool = False) -> None:
        e_s = self.e_s if not inverse else self.e_s_inv
        max_score = -1e7
        for z in np.arange(2.5, self.sd_lim, 0.5):
            epsilon = self.mean_e_s + (self.sd_e_s * z)
            pruned_e_s = e_s[e_s < epsilon]
            i_anom = np.argwhere(e_s >= epsilon).reshape(-1)
            buffer = np.arange(1, self.config.error_buffer)
            i_anom = np.sort(
                np.concatenate(
                    (
                        i_anom,
                        np.array([i + buffer for i in i_anom]).flatten(),
                        np.array([i - buffer for i in i_anom]).flatten(),
                    )
                )
            )
            i_anom = i_anom[(i_anom < len(e_s)) & (i_anom >= 0)]
            i_anom = np.sort(np.unique(i_anom))

            if len(i_anom) > 0:
                groups = [list(g) for g in mit.consecutive_groups(i_anom)]
                E_seq = [(g[0], g[-1]) for g in groups if not g[0] == g[-1]]
                mean_perc_decrease = (self.mean_e_s - np.mean(pruned_e_s)) / max(
                    self.mean_e_s, 1e-12
                )
                sd_perc_decrease = (self.sd_e_s - np.std(pruned_e_s)) / max(
                    self.sd_e_s, 1e-12
                )
                score = (mean_perc_decrease + sd_perc_decrease) / (
                    len(E_seq) ** 2 + len(i_anom)
                )
                if score >= max_score and len(E_seq) <= 5 and len(i_anom) < (
                    len(e_s) * 0.5
                ):
                    max_score = score
                    if not inverse:
                        self.sd_threshold = z
                        self.epsilon = self.mean_e_s + z * self.sd_e_s
                    else:
                        self.sd_threshold_inv = z
                        self.epsilon_inv = self.mean_e_s + z * self.sd_e_s

    def compare_to_epsilon(self, inverse: bool = False) -> None:
        e_s = self.e_s if not inverse else self.e_s_inv
        epsilon = self.epsilon if not inverse else self.epsilon_inv

        # Original sanity check: errors are tiny compared to values, or
        # the signal is flat, skip. (telemanom/errors.py:338)
        if not (
            self.sd_e_s > (0.05 * self.sd_values)
            or max(self.e_s) > (0.05 * self.inter_range)
        ) or not max(self.e_s) > 0.05:
            return

        i_anom = np.argwhere(
            (e_s >= epsilon) & (e_s > 0.05 * self.inter_range)
        ).reshape(-1)
        if len(i_anom) == 0:
            return

        buffer = np.arange(1, self.config.error_buffer + 1)
        i_anom = np.sort(
            np.concatenate(
                (
                    i_anom,
                    np.array([i + buffer for i in i_anom]).flatten(),
                    np.array([i - buffer for i in i_anom]).flatten(),
                )
            )
        )
        i_anom = i_anom[(i_anom < len(e_s)) & (i_anom >= 0)]

        if self.window_num == 0:
            i_anom = i_anom[i_anom >= self.num_to_ignore]
        else:
            i_anom = i_anom[i_anom >= len(e_s) - self.config.batch_size]
        i_anom = np.sort(np.unique(i_anom))

        # non_anom_max: the largest smoothed error value in the window that
        # is *not* currently flagged, used by prune_anoms as a baseline.
        batch_position = self.window_num * self.config.batch_size
        window_indices = np.arange(0, len(e_s)) + batch_position
        adj_i_anom = i_anom + batch_position
        all_known = np.append(self._already_known, adj_i_anom)
        window_indices = np.setdiff1d(window_indices, all_known)
        candidate_indices = np.unique(window_indices - batch_position)
        non_anom_max = float(np.max(np.take(e_s, candidate_indices))) if len(
            candidate_indices
        ) else 0.0

        groups = [list(g) for g in mit.consecutive_groups(i_anom)]
        E_seq = [(g[0], g[-1]) for g in groups if not g[0] == g[-1]]

        if inverse:
            self.i_anom_inv = i_anom
            self.E_seq_inv = E_seq
            self.non_anom_max_inv = non_anom_max
        else:
            self.i_anom = i_anom
            self.E_seq = E_seq
            self.non_anom_max = non_anom_max

    def prune_anoms(self, inverse: bool = False) -> None:
        E_seq = self.E_seq if not inverse else self.E_seq_inv
        e_s = self.e_s if not inverse else self.e_s_inv
        non_anom_max = self.non_anom_max if not inverse else self.non_anom_max_inv

        if len(E_seq) == 0:
            return
        E_seq_max = np.array([max(e_s[e[0] : e[1] + 1]) for e in E_seq])
        E_seq_max_sorted = np.sort(E_seq_max)[::-1]
        E_seq_max_sorted = np.append(E_seq_max_sorted, [non_anom_max])
        i_to_remove = np.array([])
        for i in range(0, len(E_seq_max_sorted) - 1):
            if (
                E_seq_max_sorted[i] - E_seq_max_sorted[i + 1]
            ) / E_seq_max_sorted[i] < self.config.p:
                i_to_remove = np.append(
                    i_to_remove,
                    np.argwhere(E_seq_max == E_seq_max_sorted[i]),
                )
            else:
                i_to_remove = np.array([])
        i_to_remove[::-1].sort()
        if len(i_to_remove) > 0:
            E_seq = np.delete(E_seq, i_to_remove, axis=0)
        if len(E_seq) == 0 and inverse:
            self.i_anom_inv = np.array([])
            return
        if len(E_seq) == 0 and not inverse:
            self.i_anom = np.array([])
            return
        indices_to_keep = np.concatenate(
            [range(e_seq[0], e_seq[-1] + 1) for e_seq in E_seq]
        )
        if not inverse:
            mask = np.isin(self.i_anom, indices_to_keep)
            self.i_anom = self.i_anom[mask]
        else:
            mask_inv = np.isin(self.i_anom_inv, indices_to_keep)
            self.i_anom_inv = self.i_anom_inv[mask_inv]

    def score_anomalies(self, prior_idx: int) -> None:
        groups = [list(g) for g in mit.consecutive_groups(self.i_anom)]
        for e_seq in groups:
            score = max(
                [
                    abs(self.e_s[i] - self.epsilon) / (self.mean_e_s + self.sd_e_s)
                    for i in range(e_seq[0], e_seq[-1] + 1)
                ]
            )
            inv_score = max(
                [
                    abs(self.e_s_inv[i] - self.epsilon_inv)
                    / (self.mean_e_s + self.sd_e_s)
                    for i in range(e_seq[0], e_seq[-1] + 1)
                ]
            )
            self.anom_scores.append(
                {
                    "start_idx": int(e_seq[0] + prior_idx),
                    "end_idx": int(e_seq[-1] + prior_idx),
                    "score": float(max(score, inv_score)),
                }
            )


# -- streaming wrapper ---------------------------------------------------


class ErrorStream:
    """Maintains EWMA-smoothed error buffer and runs the ErrorWindow pipeline
    on each new tick once enough history is available.

    tick(actual, predicted) -> list[AlertEvent]
    """

    def __init__(self, config: Optional[cfg.LiveConfig] = None):
        self.config = config or cfg.load()
        self.e: deque[float] = deque(maxlen=self.config.l_s + self.config.n_predictions)
        self.e_s: deque[float] = deque(maxlen=10_000)  # unbounded in practice
        # Running accumulation of all flagged indices (in live stream
        # coordinates). Mirrors Errors.i_anom (telemanom/errors.py:43).
        self.i_anom: np.ndarray = np.array([], dtype=int)
        # Live tick counter — increments per tick() call.
        self.t = 0
        # Tracks which smoothed-error indices we've already shown the user.
        # In the batch code, the Errors class scans all windows in y_test,
        # so each index gets re-evaluated many times. In streaming we scan
        # the trailing window once, so we don't need this — but we keep it
        # as a hook for future de-dup.
        self._last_alert_t = -10_000
        # Sanity: how many ticks have we received?
        self.n_ticks = 0

    @property
    def history(self) -> int:
        return len(self.e_s)

    def tick(self, actual: float, predicted: float) -> List[AlertEvent]:
        self.t += 1
        self.n_ticks += 1
        self.actuals.append(float(actual))
        e = abs(float(predicted) - float(actual))
        self.e.append(e)
        # EWMA update: we have to recompute the full EWMA every time the
        # buffer changes because pandas.ewm().mean() is not a true
        # streaming update. For demo volumes (~5 Hz × minutes) this is
        # trivially fast. Same formula as telemanom/errors.py:58.
        span = max(
            1,
            int(
                self.config.batch_size
                * self.config.window_size
                * self.config.smoothing_perc
            ),
        )
        e_arr = np.array(self.e, dtype=float)
        if len(e_arr) >= span:
            e_s_full = pd.DataFrame(e_arr).ewm(span=span).mean().values.flatten()
            new_e_s = float(e_s_full[-1])
        else:
            # Not enough data for a full EWMA span — fall back to the
            # running mean of what we have. (Matches the early-window
            # behavior in telemanom/errors.py:62-64.)
            new_e_s = float(np.mean(e_arr))
        self.e_s.append(new_e_s)

        # Below the minimum-history threshold we return no alerts. The
        # original code raises ValueError; the streaming version is
        # permissive and just waits.
        if len(self.e_s) < self.config.min_history:
            return []

        # Run the ErrorWindow pipeline on the trailing window. This is the
        # streaming analogue of Errors.process_batches (telemanom/errors.py:111).
        return self._run_pipeline()

    # -- internal ----------------------------------------------------------

    def _run_pipeline(self) -> List[AlertEvent]:
        config = self.config
        # The trailing window covers the same length as one full window of
        # batches in the original code: batch_size * window_size, plus
        # one extra batch to give the threshold sweep something to chew on.
        window_len = config.batch_size * (config.window_size + 1)
        if len(self.e_s) < window_len:
            return []
        e_s_window = np.array(list(self.e_s)[-window_len:], dtype=float)
        y_window = np.array(list(self.actuals)[-window_len:], dtype=float)

        # prior_idx is the live-stream index of the first point in the
        # window. Subtract window_len from self.t to get the start tick.
        prior_idx = self.t - window_len

        # Subset self.i_anom to indices that fall inside the current
        # window. The ErrorWindow class uses already_known to exclude
        # these from the non-anom candidate set, so an anomaly flagged
        # in a prior window won't be counted twice in the threshold math.
        already_in_window = self.i_anom[
            (self.i_anom >= prior_idx) & (self.i_anom < self.t + 1)
        ] - prior_idx

        win = ErrorWindow(
            e_s=e_s_window,
            y_test=y_window,
            config=config,
            prior_idx=prior_idx,
            window_num=0,
            already_known=already_in_window,
        )
        win.find_epsilon()
        win.find_epsilon(inverse=True)
        win.compare_to_epsilon()
        win.compare_to_epsilon(inverse=True)
        if len(win.i_anom) == 0 and len(win.i_anom_inv) == 0:
            return []
        win.prune_anoms()
        win.prune_anoms(inverse=True)
        if len(win.i_anom) == 0 and len(win.i_anom_inv) == 0:
            return []
        win.i_anom = np.sort(
            np.unique(np.append(win.i_anom, win.i_anom_inv))
        ).astype(int)
        win.score_anomalies(prior_idx=0)

        # Translate window-relative indices back to live stream indices,
        # then dedup against self.i_anom so we don't re-emit an alert
        # for an anomaly we've already announced.
        new_alerts: List[AlertEvent] = []
        for s in win.anom_scores:
            live_start = s["start_idx"] + prior_idx
            live_end = s["end_idx"] + prior_idx
            seq_range = np.arange(live_start, live_end + 1)
            new_points = np.setdiff1d(seq_range, self.i_anom)
            if len(new_points) == 0:
                continue
            self.i_anom = np.append(self.i_anom, seq_range)
            self._last_alert_t = max(self._last_alert_t, live_end)
            # Anchor the alert at the first newly-flagged point.
            new_alerts.append(
                AlertEvent(
                    t=int(new_points[0]),
                    score=s["score"],
                    seq=(live_start, live_end),
                )
            )
        return new_alerts

    # -- actuals buffer (parallel to e_s) ---------------------------------

    @property
    def actuals(self) -> deque[float]:
        if not hasattr(self, "_actuals"):
            self._actuals = deque(maxlen=10_000)
        return self._actuals

    def record_actual(self, actual: float) -> None:
        """Record the actual value alongside tick(). Called by the consumer
        before invoking tick(actual, predicted) so the EWMA window has
        matching y values for the sanity check.
        """
        self.actuals.append(float(actual))

    # -- accessors for the dashboard --------------------------------------

    def latest_error(self) -> float:
        return self.e_s[-1] if self.e_s else 0.0

    def latest_actual(self) -> Optional[float]:
        return self.actuals[-1] if self.actuals else None

    def record_actual(self, actual: float) -> None:  # backward-compat shim
        """Deprecated: tick() now records the actual automatically."""
        self.actuals.append(float(actual))
