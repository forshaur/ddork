"""Lazy pool-based competitor expansion.

Each provider returns an ordered list. Take up to PER_PROVIDER from each into `domains`;
everything else goes into `extra`. To reach the target count, draw from `extra` first; when
`extra` runs dry, expand unexpanded domains in parallel. Expansion is roughly
O(target / (PER_PROVIDER * num_providers)) rounds, not exponential in depth.

The three providers are called in parallel per domain, each behind its own AdaptiveLimiter
(a block on one upstream must not throttle the other two). Domains are expanded up to
`workers` at a time, and each expansion is time-boxed so one slow provider cannot stall it.
"""
import threading as th
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from ..config import log
from ..net import NonRetryable, normalize_domain, retry
from ..concurrency import AdaptiveLimiter
from ..progress import fmt_secs
from .distill import get_distill_competitors
from .spyfu import get_spyfu_competitors
from .owler import get_owler_competitors

PROVIDERS = [
    (get_spyfu_competitors, "S"),
    (get_distill_competitors, "D"),
    (get_owler_competitors, "O"),
]

PER_PROVIDER = 5

# Hard ceiling for one domain's provider fan-out. Socket timeouts are shorter; this
# catches anything that still hangs (DNS, redirects, a wedged TLS session) and lets
# enumeration move on.
PROVIDER_TIMEOUT = 35.0


class Enumerator:
    def __init__(self, seed, target, workers=8, delay=0.5, ui=None):
        self.seed = normalize_domain(seed)
        self.target = target
        self.ui = ui
        self.workers = max(1, workers)
        self.domains = []
        self.extra = []
        self.unexpanded = []
        self.seen = {self.seed} if self.seed else set()
        # One limiter per provider: 403s/timeouts from spyfu say nothing about owler.
        self.limiters = {
            label: AdaptiveLimiter(max_permits=self.workers, min_interval=delay)
            for _, label in PROVIDERS
        }
        self.provider_timeout = PROVIDER_TIMEOUT
        self._lock = th.Lock()
        self._ui_lock = th.Lock()

    def _call(self, fn, domain, label):
        limiter = self.limiters[label]
        limiter.acquire()
        try:
            result = fn(domain)
        except NonRetryable:
            # Deterministic failure: do not shrink the concurrency window.
            limiter.release(ok=True)
            raise
        except BaseException:
            limiter.release(ok=False)
            raise
        else:
            limiter.release(ok=True)
            return result

    def _expand(self, domain):
        log.info(f"[ENUM] expand {domain}")

        pending = {}
        pool = ThreadPoolExecutor(max_workers=len(PROVIDERS))
        try:
            for fn, label in PROVIDERS:
                fut = pool.submit(
                    lambda f=fn, l=label: (
                        l,
                        retry(lambda: self._call(f, domain, l), tries=2) or [],
                    )
                )
                pending[fut] = label

            # Consume providers as they finish instead of waiting for the slowest one.
            deadline = time.monotonic() + self.provider_timeout
            while pending:
                if len(self.domains) >= self.target:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Short slices so another expansion reaching the target stops this one.
                done, _ = wait(
                    pending, timeout=min(0.5, remaining),
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue

                for fut in done:
                    label = pending.pop(fut)
                    try:
                        _, results = fut.result()
                    except NonRetryable as e:
                        log.error(f"[{label}] {domain} err: {e}")
                        if self.ui:
                            with self._ui_lock:
                                self.ui.v(2, f"{label} lookup failed for {domain}: {e}", ok=False)
                        continue
                    except Exception as e:
                        log.error(f"[{label}] {domain} err: {e}")
                        if self.ui:
                            with self._ui_lock:
                                self.ui.v(2, f"{label} lookup failed for {domain}: {e}", ok=False)
                        continue

                    with self._lock:
                        fresh = []
                        for r in results:
                            d = normalize_domain(r)
                            if d and d not in self.seen:
                                self.seen.add(d)
                                fresh.append(d)

                        added = 0
                        top, rest = fresh[:PER_PROVIDER], fresh[PER_PROVIDER:]
                        for d in top:
                            if len(self.domains) < self.target:
                                self.domains.append(d)
                                added += 1
                            else:
                                self.extra.append(d)
                        self.extra.extend(rest)
                        self.unexpanded.extend(fresh)

                    log.info(f"[{label}] {domain} -> {len(fresh)} fresh")
                    if self.ui:
                        with self._ui_lock:
                            self.ui.v(2, f"{label} {domain}: {len(fresh)} fresh")
                            if added:
                                self.ui.advance(added)

                if len(self.domains) >= self.target:
                    break

            if pending and len(self.domains) >= self.target:
                for fut in pending:
                    fut.cancel()
                log.info(
                    f"[ENUM] {domain}: target reached; "
                    f"{len(pending)} provider result(s) dropped"
                )
            elif pending:
                for fut in list(pending):
                    label = pending[fut]
                    fut.cancel()
                    log.warning(
                        f"[{label}] {domain} timed out after "
                        f"{self.provider_timeout:.0f}s; result dropped"
                    )
        finally:
            # Do not join here: a wedged provider must not hold up the expansion.
            pool.shutdown(wait=False)

        with self._lock:
            self.unexpanded = [d for d in self.unexpanded if d != domain]

    def run(self):
        if not self.seed:
            log.error("enumerate_domains: empty seed domain")
            return []

        if self.ui:
            self.ui.phase("enumerating", self.target)
        start = time.monotonic()

        self._expand(self.seed)

        pool = ThreadPoolExecutor(max_workers=self.workers)
        inflight = {}
        try:
            while len(self.domains) < self.target:
                with self._lock:
                    drained = 0
                    while self.extra and len(self.domains) < self.target:
                        self.domains.append(self.extra.pop(0))
                        drained += 1
                if drained and self.ui:
                    with self._ui_lock:
                        self.ui.advance(drained)

                if len(self.domains) >= self.target:
                    break

                with self._lock:
                    while self.unexpanded and len(inflight) < self.workers:
                        d = self.unexpanded.pop(0)
                        inflight[pool.submit(self._expand, d)] = d

                if not inflight:
                    break

                done, _ = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    domain = inflight.pop(fut)
                    exc = fut.exception()
                    if exc:
                        log.error(f"[ENUM] expand {domain} err: {exc}")
        finally:
            for fut in inflight:
                fut.cancel()
            pool.shutdown(wait=True)

        log.info(
            f"[ENUM] complete: {len(self.domains)}/{self.target} domains "
            f"(pool leftover: {len(self.extra)})"
        )
        if self.ui:
            with self._ui_lock:
                self.ui.checkpoint(
                    f"enumerated {len(self.domains)}/{self.target} target(s) in "
                    f"{fmt_secs(time.monotonic() - start)} — "
                    f"{len(self.extra)} still in pool"
                )
        return self.domains


def enumerate_domains(seed, target, workers=8, delay=0.5, ui=None):
    return Enumerator(seed, target, workers, delay, ui).run()
