"""Exa (hermes.exa.ai) search — primary discovery source for a domain's
bug-bounty/disclosure page.

Exa is SEMANTIC: queries are natural-language sentences, not `site:` operators.
We issue four phrasings per domain and merge results, keeping only URLs whose
host is the target domain or a subdomain of it (same-site filter). Off-domain
hits (Bugcrowd listings, bbscope, news articles) are dropped.
"""
import asyncio as aio
import json
import time

from ..config import log
from ..net import (
    GlobalRateLimiter,
    RateLimited,
    canonicalize_url,
    get_session,
    get_user_agent,
    host_matches,
)

_limiter = GlobalRateLimiter(requests_per_second=2)
_cooldown_until = 0.0

QUERIES = (
    "{c} bug bounty program page",
    "{c} vulnerability disclosure policy",
    "{c} responsible disclosure report a security vulnerability",
    "{c} security.txt security policy",
)


def _resolve_ref(value, array):
    if isinstance(value, int) and 0 <= value < len(array):
        return array[value]
    if isinstance(value, list):
        return [_resolve_ref(x, array) for x in value]
    return value


def parse_exa_response(text):
    results = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if payload.get("type") != "data":
            continue
        for node in payload.get("nodes", []):
            if node.get("type") != "data":
                continue
            arr = node.get("data", [])
            if not arr or not isinstance(arr, list):
                continue
            meta = arr[0] if arr else {}
            if not isinstance(meta, dict):
                continue
            results_idx = meta.get("results")
            if results_idx is None or results_idx >= len(arr):
                continue
            result_indices = arr[results_idx]
            if not isinstance(result_indices, list):
                continue
            for r_ix in result_indices:
                if not isinstance(r_ix, int) or r_ix >= len(arr):
                    continue
                o = arr[r_ix]
                if not isinstance(o, dict):
                    continue
                title, url, highlights, domain = (
                    _resolve_ref(o.get(k), arr)
                    for k in ("title", "url", "highlights", "domain")
                )
                if isinstance(highlights, list):
                    highlights = " ".join(str(h) for h in highlights if h)
                if url and isinstance(url, str):
                    results.append({
                        "title": str(title) if title else "",
                        "url": url,
                        "snippet": str(highlights) if highlights else "",
                        "domain": str(domain) if domain else "",
                    })
    return results


async def _query(domain, query, max_retries, initial_wait):
    global _cooldown_until
    wait = initial_wait
    for _ in range(max_retries):
        remaining = _cooldown_until - time.monotonic()
        if remaining > 0:
            await aio.sleep(remaining)
        try:
            await _limiter.wait_for_token()
            r = await aio.to_thread(
                get_session().get,
                "https://hermes.exa.ai/search/__data.json",
                params={"q": query, "type": "auto",
                        "x-sveltekit-invalidated": "01"},
                headers={"referer": "https://hermes.exa.ai/",
                         "user-agent": get_user_agent()},
                timeout=10,
            )
            if r.status_code == 429:
                _cooldown_until = time.monotonic() + wait
                raise RateLimited(f"exa 429 for {domain}", retry_after=wait)
            if r.status_code != 200:
                return []
            return parse_exa_response(r.text)
        except RateLimited:
            wait = min(wait * 2, 20)
        except Exception as e:
            log.error(f"[EXA] {domain} err: {e}")
            return []
    return []


async def search_exa(domain, max_retries=3, initial_wait=2):
    company = domain.split(".")[0]
    seen = set()
    out = []
    for tmpl in QUERIES:
        hits = await _query(domain, tmpl.format(c=company),
                            max_retries, initial_wait)
        for h in hits:
            url = h["url"]
            if not host_matches(domain, url):
                continue
            c = canonicalize_url(url)
            if c and c not in seen:
                seen.add(c)
                out.append(h)
    return out