"""Thread-safe token bucket rate limiter."""

import threading
import time
from typing import Callable

_EPS = 1e-9


class TokenBucket:
    """Allows `rate` acquisitions per second with bursts up to `capacity`.

    capacity=1 spaces calls evenly (1/rate apart), which is the safest way to
    stay under Kite's per-second limits.
    """

    def __init__(self, rate: float, capacity: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def set_rate(self, rate: float) -> None:
        with self._lock:
            self.rate = float(rate)

    def acquire(self) -> float:
        """Block until a token is available; returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0 - _EPS:  # epsilon: float drift must not spin forever
                    self._tokens = max(0.0, self._tokens - 1.0)
                    return waited
                wait = (1.0 - self._tokens) / self.rate
            self._sleep(wait)
            waited += wait
