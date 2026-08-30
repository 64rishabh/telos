"""Synthetic telemetry generator for the live demo.

Produces a sinusoid + Gaussian noise, with a thread-safe injection queue so
the HTTP /inject endpoint can drop in anomalies (spike, level shift, dropout)
at any tick.
"""
from __future__ import annotations

import math
import random
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


VALID_KINDS = ("spike", "shift", "dropout")


@dataclass
class Tick:
    t: int
    value: float
    injected: bool = False


def _apply_injection(value: float, kind: str, magnitude: float) -> float:
    """Mutate a single value according to the injection kind.

    spike:   add `magnitude` once (scaled so a magnitude of 1.0 ≈ +3σ on a
             unit-amplitude signal — chosen because raw σ=0.1 with a unit
             amplitude is too small to feel like a "spike" in the demo).
    shift:   add `magnitude` as a persistent level shift. The caller is
             responsible for clearing it; for the demo, the simulator
             converts a single shift request into a finite-duration shift
             (see SyntheticGenerator.inject) so the user always sees the
             recovery.
    dropout: set the value to 0 (simulates a sensor going silent).
    """
    if kind == "spike":
        return value + magnitude * 3.0
    if kind == "shift":
        return value + magnitude
    if kind == "dropout":
        return 0.0
    raise ValueError(f"unknown injection kind: {kind!r}")


class SyntheticGenerator:
    """Yields Tick(t, value) one at a time. Thread-safe injection queue."""

    def __init__(
        self,
        amp: float = 1.0,
        freq: float = 0.05,
        noise_std: float = 0.1,
        seed: Optional[int] = None,
        shift_duration_ticks: int = 30,
    ) -> None:
        self.amp = amp
        self.freq = freq
        self.noise_std = noise_std
        self.shift_duration_ticks = shift_duration_ticks
        self._t = 0
        self._lock = threading.Lock()
        self._queue: List[Tuple[str, float, int]] = []  # (kind, mag, expires_at_t)
        if seed is not None:
            random.seed(seed)

    # -- injection API ------------------------------------------------------

    def inject(self, kind: str, magnitude: float = 1.0) -> None:
        """Queue an anomaly. `shift` lasts for `shift_duration_ticks` then
        auto-expires so the demo shows both the onset and the recovery.
        """
        if kind not in VALID_KINDS:
            raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
        with self._lock:
            expires = self._t + (self.shift_duration_ticks if kind == "shift" else 1)
            self._queue.append((kind, float(magnitude), expires))

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- tick production ----------------------------------------------------

    def next(self) -> Tick:
        with self._lock:
            self._t += 1
            base = self.amp * math.sin(self.freq * self._t) + random.gauss(
                0.0, self.noise_std
            )
            injected = False
            # Apply any non-expired queued injection. Queue entries are
            # single-use; the shift auto-clears by virtue of its expiry.
            if self._queue:
                kind, mag, expires = self._queue[0]
                if self._t > expires:
                    self._queue.pop(0)
                else:
                    base = _apply_injection(base, kind, mag)
                    injected = True
                    if kind != "shift":
                        # spike / dropout fire once
                        self._queue.pop(0)
            return Tick(t=self._t, value=base, injected=injected)

    def prefill(self, n: int) -> List[float]:
        """Generate `n` warmup ticks and return the values. Does not advance
        the public tick counter into the live region — the live consumer
        will see t=0 as its first received tick, and the values produced
        here become the LSTM's input window.
        """
        # We use a private counter so the prefilled ticks don't pollute the
        # live sequence numbering. The shape of the sinusoid is the same
        # regardless of starting t, so this is fine.
        return [
            self.amp * math.sin(self.freq * i) + random.gauss(0.0, self.noise_std)
            for i in range(1, n + 1)
        ]

    @property
    def t(self) -> int:
        with self._lock:
            return self._t
