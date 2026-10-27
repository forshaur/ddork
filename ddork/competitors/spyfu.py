"""SpyFu 'top organic competitors' lookup. Returns ordered list of domains."""
import re

from curl_cffi import requests as rq
from bs4 import BeautifulSoup as bs

from ..net import NonRetryable, RateLimited, normalize_domain

CSRF_META_RE = re.compile(r"csrf-token", re.I)
CSRF_INLINE_RE = re.compile(
    r'csrf(?:Token)?["\']?\s*:\s*["\']([^"\']+)["\']', re.I
)


def get_spyfu_competitors(domain):
    with rq.Session(impersonate="chrome110") as s:
        r = s.get(
            f"https://www.spyfu.com/overview/domain?query={domain}", timeout=15
        )

        token = (bs(r.text, "html.parser").find(
            "meta", {"name": CSRF_META_RE}) or {}).get("content")
        token = token or (CSRF_INLINE_RE.search(r.text) or [None, None])[1]
        token = token or s.cookies.get("XSRF-TOKEN") or s.cookies.get("csrf-token")

        headers = {
            "accept": "application/json, text/plain, */*",
            "referer": f"https://www.spyfu.com/overview/domain?query={domain}",
        }
        if token:
            headers["x-csrf-token"] = token

        r = s.get(
            "https://www-in.spyfu.com/NsaApi/Competitors/GetTopOrganicCompetitors",
            params={"domain": domain, "countryCode": "US"},
            headers=headers,
            timeout=15,
        )
        if r.status_code == 429:
            ra = r.headers.get("Retry-After")
            ra = int(ra) if ra and ra.isdigit() else None
            raise RateLimited(
                f"spyfu {domain}: rate limited (429)" +
                (f", retry-after={ra}s" if ra else ""),
                retry_after=ra,
            )
        if 400 <= r.status_code < 500:
            raise NonRetryable(
                f"spyfu {domain}: HTTP {r.status_code}: {r.text[:200]!r}",
                status=r.status_code,
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"spyfu {domain}: HTTP {r.status_code}: {r.text[:200]!r}"
            )
        try:
            j = r.json()
        except Exception as e:
            raise RuntimeError(
                f"spyfu {domain}: non-JSON 200 response: {r.text[:200]!r}"
            ) from e

        out, seen = [], set()
        if isinstance(j, list):
            for x in j:
                if isinstance(x, dict):
                    d = normalize_domain(x.get("domain"))
                    if d and d not in seen:
                        seen.add(d)
                        out.append(d)
        return out