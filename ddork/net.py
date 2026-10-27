"""Small, dependency-free networking/domain helpers shared across the package."""
import asyncio as aio
import random
import re
import time
from functools import lru_cache
from threading import local
from urllib.parse import parse_qsl, urlencode, urlparse as up, urlunparse
import tldextract
from curl_cffi import requests as rq

from .config import USER_AGENTS, IMPERSONATE


@lru_cache(maxsize=1)
def _remote_user_agents():
    try:
        r = rq.get("https://headers.scrapeops.io/v1/user-agents", timeout=3)
        return r.json().get("result", []) if r.status_code == 200 else []
    except Exception:
        return []


def get_user_agent():
    return random.choice(_remote_user_agents() or USER_AGENTS)


# --- Shared per-thread session -------------------------------------------
_thread_local = local()


def get_session():
    """Return a curl_cffi session for the current thread (cached per-thread)."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = rq.Session(impersonate=IMPERSONATE)
        _thread_local.session = s
    return s


def normalize_domain(d):
    if not d:
        return None
    p = up(d if "://" in d else "https://" + d).netloc.lower().strip()
    return p[4:] if p.startswith("www.") else p


def resolve_ddg_redirect(u):
    if not u:
        return None
    u = f"https:{u}" if u.startswith("//") else u
    p = up(u)
    if "duckduckgo.com" in p.netloc and "uddg" in p.query:
        for pt in p.query.split("&"):
            if pt.startswith("uddg="):
                from urllib.parse import unquote as uq
                return uq(pt[5:])
    return u


def to_registrable_domain(hostname):
    if not hostname:
        return None
    ext = tldextract.extract(hostname)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


# --- Relevance checks (ddg.py / exa.py / searx.py) -----------------------

# Registrable domains of known bug-bounty platforms. A result hosted on one of
# these counts as relevant if the target's brand slug appears in the URL or
# snippet — that's how we keep hackerone.com/<target> and reject
# hackerone.com/some-other-company.
BOUNTY_PLATFORMS = {
    "hackerone.com", "bugcrowd.com", "yeswehack.com", "intigriti.com",
    "openbugbounty.org", "hackenproof.com", "synack.com", "cobalt.io",
    "immunefi.com", "bugbounty.jp", "zerocopter.com", "safehats.com",
    "federacy.com", "detectify.com", "hackerbay.com", "bugbase.in",
    "redstorm.io", "vulncheck.com",
}


def host_matches(domain, url):
    """True if url's host is `domain` itself or a subdomain of it, ignoring www."""
    if not domain or not url:
        return False
    target = normalize_domain(domain)
    host = normalize_domain(url)
    if not target or not host:
        return False
    return host == target or host.endswith("." + target)


def is_relevant_result(domain, url, text=""):
    """Relevant if hosted on the target's own domain, or on a known bounty
    platform and mentioning the target's brand slug."""
    if host_matches(domain, url):
        return True
    host = normalize_domain(url)
    slug = domain.split(".")[0] if domain else None
    if not host or host not in BOUNTY_PLATFORMS or not slug:
        return False
    return bool(re.search(rf"\b{re.escape(slug)}\b", f"{url} {text}".lower()))


# --- URL canonicalization ------------------------------------------------

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "_ga",
}


def canonicalize_url(url):
    """Normalize a URL for dedup. Lowercases scheme+host, strips default
    ports, leading www., tracking params, and a trailing slash on the path."""
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    try:
        p = up(url)
    except Exception:
        return None
    if not p.netloc:
        return None
    scheme = (p.scheme or "https").lower()
    host = p.netloc.lower()
    if scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    elif scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    pairs = sorted(
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    )
    query = urlencode(pairs)
    return urlunparse((scheme, host, path, "", query, ""))


# --- Per-Domain Token Bucket Rate Limiter ---------------------------------

class DomainRateLimiter:
    """Fair per-domain rate limiting using token buckets.

    Each domain gets its own bucket (default: 2 req/s, burst 5) AND its own
    lock, so:
      - a slow/blocked domain never throttles an unrelated one (per-bucket)
      - same-domain callers can't outrun the bucket (per-bucket lock held
        across the read-compute-sleep-consume sequence)

    The previous version released the shared lock before the token math and
    the `await aio.sleep()`; two same-domain coroutines would then both read a
    stale token balance and both consume a token the bucket only held once,
    silently exceeding the configured rate under concurrent load.
    """
    def __init__(self, rate=2.0, capacity=5):
        self.rate = rate
        self.capacity = capacity
        self._buckets = {}
        self._map_lock = aio.Lock()

    async def _get_bucket(self, domain):
        async with self._map_lock:
            b = self._buckets.get(domain)
            if b is None:
                b = {"tokens": self.capacity,
                     "last": time.monotonic(),
                     "lock": aio.Lock()}
                self._buckets[domain] = b
            return b

    async def acquire(self, domain):
        bucket = await self._get_bucket(domain)
        async with bucket["lock"]:
            now = time.monotonic()
            elapsed = now - bucket["last"]
            bucket["tokens"] = min(self.capacity,
                                   bucket["tokens"] + elapsed * self.rate)
            bucket["last"] = now
            if bucket["tokens"] < 1:
                sleep_time = (1 - bucket["tokens"]) / self.rate
                await aio.sleep(sleep_time)
                bucket["tokens"] = 0
            else:
                bucket["tokens"] -= 1


# Global instance used by analyzer
domain_limiter = DomainRateLimiter(rate=2.0, capacity=5)


async def gated_sync(limiter, fn, *args, **kwargs):
    """Run sync fn(*args) in a thread under the per-domain limiter.

    `domain` kwarg is required for rate limiting and is popped before the call.
    Returns the fn result, or None on failure. Never raises.
    """
    domain = kwargs.pop("domain", None)
    if limiter and domain:
        await limiter.acquire(domain)
    try:
        return await aio.to_thread(fn, *args)
    except Exception:
        return None


class RateLimited(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class NonRetryable(RuntimeError):
    """A deterministic failure (HTTP 4xx). Retrying cannot fix it."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def retry(fn, tries=3, base=1.5):
    for a in range(tries):
        try:
            return fn()
        except (RateLimited, NonRetryable):
            raise
        except Exception:
            if a == tries - 1:
                raise
            time.sleep(base * (2 ** a))


async def retry_async(fn, tries=3, base=1.0):
    for a in range(tries):
        try:
            return await fn()
        except (RateLimited, NonRetryable):
            raise
        except Exception:
            if a == tries - 1:
                raise
            await aio.sleep(base * (2 ** a))


class GlobalRateLimiter:
    """Paces a single upstream to a fixed req/s. Used by ddg.py / exa.py."""
    def __init__(self, requests_per_second):
        self.interval = 1.0 / requests_per_second
        self._last_scheduled_time = 0.0
        self._lock = aio.Lock()

    async def wait_for_token(self):
        async with self._lock:
            now = time.monotonic()
            if self._last_scheduled_time < now:
                self._last_scheduled_time = now
            target_time = self._last_scheduled_time
            self._last_scheduled_time = target_time + self.interval
        sleep_time = target_time - time.monotonic()
        if sleep_time > 0:
            await aio.sleep(sleep_time)