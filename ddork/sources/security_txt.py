"""Look up a domain's security.txt (RFC 9116). Returns dict with:
found, source_url, policy_url, snippet (short), body (full text).

Parsing notes:
  - The `policy:` value is extracted with a line-anchored regex (tolerant of
    arbitrary whitespace after the colon) rather than `split(":", 1)`, which
    mangles any value containing a colon of its own.
  - The extracted value is validated with urlparse: scheme must be http/https
    and netloc must be non-empty. mailto:, relative paths, and garbage are
    rejected so the analyzer never tries to fetch them.
  - When the policy value is missing or invalid we still return found=True
    with policy_url=None. This is deliberate: the analyzer passes
    `body` to the classifier as security_txt context for whatever URL it ends
    up classifying, and returning found=False would strip that context.
"""
import re
from urllib.parse import urlparse as up

from ..config import log
from ..net import get_session, gated_sync

CANDIDATE_PATHS = (
    "https://{d}/.well-known/security.txt",
    "https://www.{d}/.well-known/security.txt",
    "https://{d}/security.txt",
)

PGP_BLOCK_RE = re.compile(
    r'-----BEGIN PGP SIGNED MESSAGE-----.*?-----END PGP SIGNATURE-----', re.S
)

# Anchored at line start (MULTILINE) with optional leading whitespace; matches
# the first `policy:` field. Value captured until end of line.
_POLICY_RE = re.compile(r"^\s*policy\s*:\s*(.+?)\s*$", re.I | re.M)

_EMPTY = {"found": False, "source_url": None, "policy_url": None,
          "snippet": None, "body": None}


def _fetch(u):
    try:
        r = get_session().get(u, timeout=8)
    except Exception:
        return None
    if r.status_code != 200 or "contact:" not in r.text.lower():
        return None
    return r.text


def _valid_policy_url(value):
    """Return `value` if it looks like a fetchable http(s) URL, else None."""
    if not value:
        return None
    try:
        p = up(value)
    except Exception:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return value


async def check_security_txt(domain, limiter):
    for u in (p.format(d=domain) for p in CANDIDATE_PATHS):
        text = await gated_sync(limiter, _fetch, u, domain=domain)
        if not text:
            continue
        body = PGP_BLOCK_RE.sub("", text).strip()
        m = _POLICY_RE.search(body)
        raw_policy = m.group(1).strip() if m else None
        policy = _valid_policy_url(raw_policy)
        if policy is None:
            log.info(f"[SEC] {domain} found at {u} but policy value "
                     f"missing or unparseable: {raw_policy!r}")
        else:
            log.info(f"[SEC] {domain} policy: {policy}")
        return {
            "found": True,
            "source_url": u,
            "policy_url": policy,
            "snippet": body[:400],
            "body": body,
        }
    log.info(f"[SEC] {domain} not found")
    return dict(_EMPTY)