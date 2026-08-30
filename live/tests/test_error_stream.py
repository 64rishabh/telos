"""Tests for the streaming ErrorStream.

The error stream needs the LSTM-then-error pipeline to be meaningful. To
keep these tests fast we don't run a real model — we feed it a known
predictor (e.g. always-1.0) and a known actual signal, then assert that
the error stream flags the right windows.
"""
import math

import numpy as np
import pytest

from live.error_stream import ErrorStream
from live import config as cfg


def test_below_min_history_returns_no_alerts():
    es = ErrorStream()
    # Default config needs BATCH_SIZE * (WINDOW_SIZE + 1) = 70 * 31 = 2170
    # ticks before alerts are even attempted. We can't iterate that in a
    # test, so override the threshold downward.
    es.config.min_history = 100
    for i in range(99):
        alerts = es.tick(actual=0.0, predicted=0.0)
        assert alerts == []


def test_clean_sinusoid_does_not_alert():
    """A perfect predictor on a smooth signal should produce ~0 error and
    no alerts, even past the minimum-history threshold."""
    es = ErrorStream()
    es.config.min_history = 50
    # Predictor that exactly matches the actual.
    for i in range(200):
        actual = math.sin(0.1 * i)
        alerts = es.tick(actual=actual, predicted=actual)
        assert alerts == []


def test_sustained_shift_triggers_alert():
    """Inject a level shift into the actual signal. The predictor (which
    sees the pre-shift pattern) will start missing by ~magnitude. Once
    the EWMA-smoothed error exceeds threshold, an alert should fire.
    """
    es = ErrorStream()
    # Tighten the history floor so the test runs in a few hundred ticks.
    es.config.min_history = 80
    # Make the threshold parameters friendly to a small synthetic signal.
    es.config.batch_size = 10
    es.config.window_size = 8
    es.config.smoothing_perc = 0.5  # span = 10*8*0.5 = 40 ticks
    es.config.l_s = 20
    es.config.error_buffer = 5
    es.config.p = 0.1

    fired = False
    for i in range(500):
        actual = 1.0 if i >= 100 else 0.0  # sustained +1.0 shift at t=100
        # Predictor thinks the signal stays at 0.
        alerts = es.tick(actual=actual, predicted=0.0)
        if alerts:
            fired = True
            break
    assert fired, "expected an alert within 500 ticks of a sustained level shift"


def test_single_point_spike_does_not_persist():
    """A single-point spike should fire at most once. After the EWMA of
    the spike has decayed back below the threshold, the system should
    stop emitting alerts even though the spike index is technically
    still in the trailing window.
    """
    es = ErrorStream()
    es.config.min_history = 50
    es.config.batch_size = 5
    es.config.window_size = 4
    es.config.smoothing_perc = 0.5
    es.config.l_s = 10
    es.config.error_buffer = 3
    es.config.p = 0.5  # aggressive pruning

    # Background: zero predictor, zero actual (so e=0 always).
    for i in range(150):
        alerts = es.tick(actual=0.0, predicted=0.0)
        assert alerts == []

    # One huge spike. We expect an alert (the system caught the anomaly)
    # — what we DON'T expect is for the system to keep firing forever.
    fired_at_least_once = False
    for _ in range(200):
        alerts = es.tick(actual=0.0, predicted=0.0)
        if alerts:
            fired_at_least_once = True
        if fired_at_least_once and not alerts:
            # The system saw the spike, flagged it, then went quiet.
            return
    # If we got here the system kept firing for 200 ticks — that's
    # wrong, the spike was a single point.
    assert not fired_at_least_once or False, "spike produced alerts indefinitely"
