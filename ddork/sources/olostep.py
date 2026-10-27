"""Olostep search source.

Primary engine for ~50% of search-needing domains (assigned by
analyzer._engine_cycle). Returns a possibly-empty list of relevant
{url,title,snippet} dicts; empty means "no results", not a failure. Raises
on failure so the caller can hand the domain to Exa.

The public playground endpoint (`api.olostep.com/playground/...`) has a very
small unauthenticated quota and 429s aggressively. We do a single attempt
with a slow limiter and hand off to Exa on any failure — retrying inside the
same run does not help, since the quota won't recover on that timescale.

Error responses are logged with the body (truncated) and any rate-limit
headers the endpoint sends, so a 429's actual reason is visible in scan.log
rather than just the status code.
"""
import asyncio as aio
import time

from ..config import log
from ..net import (
    GlobalRateLimiter,
    RateLimited,
    get_session,
    get_user_agent,
    is_relevant_result,
)

_URL = "https://api.olostep.com/playground/v1/searches"
_limiter = GlobalRateLimiter(requests_per_second=0.2)   # 1 req / 5s
_cooldown_until = 0.0

_BODY_LOG_LIMIT = 800


def _parse_response(data):
    """Extract a flat list of {url, title, snippet} from the decoded JSON.

    Expected shape: {"result": {"links": [{url, title, description, ...}, ...]}}
    """
    results = []
    if not isinstance(data, dict):
        return results
    links = (data.get("result") or {}).get("links") or []
    if not isinstance(links, list):
        return results
    for item in links:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or ""
        if not url or not isinstance(url, str):
            continue
        title = item.get("title") or item.get("name") or ""
        snippet = item.get("description") or item.get("snippet") or ""
        results.append({
            "url": url,
            "title": str(title),
            "snippet": str(snippet),
        })
    return results


def _rate_limit_headers(r):
    """Return a compact string of rate-limit-related headers, if any."""
    interesting = {
        k: v for k, v in r.headers.items()
        if k.lower().startswith("x-ratelimit")
        or k.lower() in ("retry-after", "x-quota-remaining", "x-quota-reset")
    }
    if not interesting:
        return ""
    return " headers={" + ", ".join(f"{k}={v!r}" for k, v in interesting.items()) + "}"


def _body_snippet(r):
    """Best-effort readable body for logging. Never raises."""
    try:
        text = r.text
    except Exception:
        return "<no body>"
    if not text:
        return "<empty body>"
    text = text.strip()
    if len(text) > _BODY_LOG_LIMIT:
        text = text[:_BODY_LOG_LIMIT] + f"... <truncated, {len(text)} bytes total>"
    return text


async def search_olostep(domain):
    """Search Olostep for `{domain} bug bounty`.

    Returns relevant hits (possibly empty). Raises on any failure so the
    caller can hand the domain to Exa. Single attempt — see module docstring.
    """
    global _cooldown_until
    remaining = _cooldown_until - time.monotonic()
    if remaining > 0:
        await aio.sleep(remaining)
    await _limiter.wait_for_token()
    r = await aio.to_thread(
        get_session().post,
        _URL,
        json={"query": f'{domain} bug bounty'},
        headers={
            "content-type": "application/json",
            "origin": "https://www.olostep.com",
            "referer": "https://www.olostep.com/",
            "user-agent": get_user_agent(),
        },
        timeout=30,
    )

    if r.status_code == 429:
        body = _body_snippet(r)
        headers = _rate_limit_headers(r)
        log.warning(f"[OLOSTEP] {domain}: 429 body={body}{headers}")
        _cooldown_until = time.monotonic() + 30
        raise RateLimited(f"olostep 429 for {domain}", retry_after=30)

    if r.status_code != 200:
        body = _body_snippet(r)
        headers = _rate_limit_headers(r)
        log.warning(f"[OLOSTEP] {domain}: HTTP {r.status_code} "
                    f"body={body}{headers}")
        raise RuntimeError(f"olostep {domain}: HTTP {r.status_code}")

    try:
        data = r.json()
    except Exception as e:
        body = _body_snippet(r)
        log.warning(f"[OLOSTEP] {domain}: invalid JSON body={body}")
        raise RuntimeError(f"olostep {domain}: invalid JSON") from e

    _cooldown_until = 0.0
    hits = _parse_response(data)
    relevant = [
        h for h in hits
        if is_relevant_result(domain, h["url"], f"{h['title']} {h['snippet']}")
    ]
    if hits and not relevant:
        log.info(f"[OLOSTEP] {domain}: {len(hits)} raw hits, "
                 f"0 passed relevance filter")
    return relevant