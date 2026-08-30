"""Tests for the synthetic generator."""
import math

import pytest

from live.generator import SyntheticGenerator, VALID_KINDS


def test_prefill_returns_n_values():
    gen = SyntheticGenerator(seed=0)
    vals = gen.prefill(50)
    assert len(vals) == 50
    assert all(isinstance(v, float) for v in vals)


def test_prefill_is_pure_sinusoid_shape():
    # Without noise we should get exactly sin(0.05 * i).
    gen = SyntheticGenerator(amp=1.0, freq=0.05, noise_std=0.0, seed=0)
    vals = gen.prefill(20)
    for i, v in enumerate(vals, start=1):
        assert v == pytest.approx(math.sin(0.05 * i), abs=1e-9)


def test_next_increments_t():
    gen = SyntheticGenerator(seed=0)
    t1 = gen.next()
    t2 = gen.next()
    assert t2.t == t1.t + 1


def test_inject_spike_changes_value():
    gen = SyntheticGenerator(amp=0.0, freq=0.0, noise_std=0.0, seed=0)
    # Without injection, value is always 0.
    assert gen.next().value == 0.0
    gen.inject("spike", 1.0)
    spiked = gen.next()
    assert spiked.value == pytest.approx(3.0)  # spike magnitude is scaled by 3
    assert spiked.injected is True
    # Spike is one-shot, next tick is back to 0.
    clean = gen.next()
    assert clean.value == 0.0
    assert clean.injected is False


def test_inject_dropout_zeros_value():
    gen = SyntheticGenerator(amp=1.0, freq=0.05, noise_std=0.0, seed=0)
    gen.inject("dropout", 1.0)
    t = gen.next()
    assert t.value == 0.0


def test_inject_shift_persists_for_duration():
    gen = SyntheticGenerator(
        amp=0.0, freq=0.0, noise_std=0.0, seed=0, shift_duration_ticks=5
    )
    gen.inject("shift", 1.5)
    ticks = [gen.next() for _ in range(7)]
    # First 5 should be shifted, last 2 should be back to 0.
    assert all(t.injected for t in ticks[:5])
    assert all(not t.injected for t in ticks[5:])
    assert all(t.value == pytest.approx(1.5) for t in ticks[:5])
    assert all(t.value == pytest.approx(0.0) for t in ticks[5:])


def test_inject_rejects_unknown_kind():
    gen = SyntheticGenerator(seed=0)
    with pytest.raises(ValueError):
        gen.inject("flood", 1.0)


def test_valid_kinds_complete():
    assert set(VALID_KINDS) == {"spike", "shift", "dropout"}


def test_pending_count():
    gen = SyntheticGenerator(seed=0)
    assert gen.pending() == 0
    gen.inject("spike", 1.0)
    gen.inject("spike", 1.0)
    assert gen.pending() == 2
    gen.next()
    assert gen.pending() == 1
