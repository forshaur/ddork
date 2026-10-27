"""Adaptive concurrency limiter for the competitor-enumeration fan-out.

Problem: BFS competitor enumeration grows the frontier geometrically (11 -> 121 -> 1331 at
fan-out 11), so request *volume* scales with depth even though thread count is capped at
`-w` -- a worker count that's safe at depth 1 can still get you rate-limited or IP-blocked
once hundreds of domains are queued behind it, especially with the old delay mechanism
(which only slept between *collecting* results, not between requests, so it never actually
throttled anything).

Fix: AIMD, the same idea TCP congestion control uses. Start at the configured cap, add 1
concurrent permit per run of clean successes, halve permits the instant a request fails
(treat any failure as a possible block/rate-limit until proven otherwise). Enumeration keeps
one instance per provider, because a block on one upstream says nothing about the others.
"""
import threading
import time


class AdaptiveLimiter:
    def __init__(self, max_permits, min_permits=1, grow_after=5, min_interval=0.0):
        self.max_permits = max(min_permits, max_permits)
        self.min_permits = min_permits
        self.permits = self.max_permits  # start at the cap; AIMD halves on failure
        self.grow_after = grow_after
        self.min_interval = min_interval
        self._in_flight = 0
        self._successes = 0
        self._last_acquire = 0.0
        self._cv = threading.Condition()

    def acquire(self):
        with self._cv:
            while self._in_flight >= self.permits:
                self._cv.wait()
            self._in_flight += 1
            wait = self.min_interval - (time.monotonic() - self._last_acquire)
            self._last_acquire = time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def release(self, ok):
        with self._cv:
            self._in_flight -= 1
            if ok:
                self._successes += 1
                if self._successes >= self.grow_after and self.permits < self.max_permits:
                    self.permits += 1  # additive increase
                    self._successes = 0
            else:
                self.permits = max(self.min_permits, self.permits // 2)  # multiplicative decrease
                self._successes = 0
            self._cv.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release(ok=exc_type is None)
        return False  # never suppress the exception