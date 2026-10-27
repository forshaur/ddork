"""Per-domain discovery + isBounty classification, batched across domains.

Pipeline per domain:
  1. security.txt — deterministic, runs first; if it advertises a fetchable
     policy URL, that URL is the only candidate.
  2. Otherwise a search engine. Search-needing domains are split ~50/50
     between Exa and Olostep by a round-robin cycle. Each domain gets one
     primary engine; the other is called only if the primary raises.

Candidates are canonicalized (www-stripped) and deduped before fetching.

Classification is loaded lazily through classifier.py, so a missing or
broken isbounty install does not prevent the module from importing.
"""
import asyncio as aio
import itertools
import time

from .classifier import ClassifierUnavailable, classifier
from .config import log
from .net import canonicalize_url, gated_sync, domain_limiter
from .progress import fmt_secs
from .scrape import fetch_and_clean
from .sources.security_txt import check_security_txt
from .sources.exa import search_exa
from .sources.olostep import search_olostep

# Round-robin primary assignment: half of search-needing domains go to Exa
# first, half to Olostep. The other engine is only invoked if the primary
# raises (see _search_with_fallback). asyncio is single-threaded, so
# advancing a bare cycle between coroutines is safe without a lock.
# Exa-only: olostep playground is capped at 50 req/24h per IP and 429s
# after a single run. Kept on disk for when a real API key is wired in.
_engine_cycle = itertools.cycle([
    (search_exa, "exa", search_exa, "exa")#search_olostep, "olostep"),
    #search_olostep, "olostep", search_exa, "exa"),
])


async def _classify(url, raw_text, domain, security_txt=None, headings=None):
    """Classify one page. Returns the pipeline result or None.

    Returns None (rather than raising) if the classifier is unavailable or
    classification fails for any reason — a single unclassifiable page must
    never abort the run.
    """
    if not raw_text:
        return None
    if not classifier.available:
        return None
    try:
        def _run():
            return classifier.classify_page(
                url=url,
                raw_text=raw_text,
                headings=headings,
                domain=domain,
                security_txt=security_txt,
            )

        return await aio.to_thread(_run)
    except ClassifierUnavailable:
        # Classifier disappeared between the availability check and the
        # call. Log once; the CLI preflight normally prevents this.
        log.error("[CLS] classifier unavailable mid-run; skipping page")
        return None
    except Exception as e:
        log.error(f"[CLS] {url} err: {e.__class__.__name__}: {e}")
        return None


def _finding(domain, url, source, result):
    return {
        "domain": domain,
        "url": url,
        "source": source,
        "label": getattr(result, "label", "NOT_PROGRAM"),
        "confidence": float(getattr(result, "confidence", 0.0)),
        "decision_path": getattr(result, "decision_path", "") or "",
        "reasons": list(getattr(result, "reasons", []) or []),
    }


MAX_URLS_PER_DOMAIN = 5


async def _search_with_fallback(domain, primary_fn, primary_src,
                                fallback_fn, fallback_src):
    """Run the assigned primary engine; fall back only on error.

    An empty list is a valid answer ("no bounty page for this domain") and
    is NOT retried against the other engine — that would double request
    volume across the run for no gain. Only an exception triggers the
    handoff. Returns (hits, source_label).
    """
    try:
        hits = await primary_fn(domain)
        for h in hits or []:
            log.debug(f"[{primary_src.upper()}] {domain} → {h['url']}")
        log.info(f"[SEARCH] {domain}: {primary_src} → {len(hits or [])} hit(s)")
        return hits or [], primary_src
    except Exception as e:
        log.warning(f"[SEARCH] {domain}: {primary_src} failed "
                    f"({e.__class__.__name__}: {e}); retrying with {fallback_src}")

    try:
        hits = await fallback_fn(domain)
        for h in hits or []:
            log.debug(f"[{fallback_src.upper()}] {domain} → {h['url']}")
        log.info(f"[SEARCH] {domain}: {fallback_src} (fallback) → "
                 f"{len(hits or [])} hit(s)")
        return hits or [], fallback_src
    except Exception as e:
        log.warning(f"[SEARCH] {domain}: {fallback_src} also failed "
                    f"({e.__class__.__name__}: {e}); giving up")
        return [], fallback_src


async def analyze_domain(domain, semaphore, limiter, ui=None):
    async with semaphore:
        log.info(f"[ANZ] start {domain}")

        sec = await check_security_txt(domain, limiter)
        sec_body = sec.get("body") if sec.get("found") else None

        candidates = []
        if sec.get("policy_url"):
            candidates.append((sec["policy_url"], "sec.txt"))
        else:
            primary_fn, primary_src, fallback_fn, fallback_src = next(_engine_cycle)
            log.debug(f"[SEARCH] {domain}: primary={primary_src.upper()}, "
                      f"fallback={fallback_src.upper()} (on error only)")
            hits, hits_src = await _search_with_fallback(
                domain, primary_fn, primary_src, fallback_fn, fallback_src
            )
            candidates.extend((r["url"], hits_src) for r in hits)

        seen_canon = set()
        unique = []
        for url, src in candidates:
            c = canonicalize_url(url)
            if c and c not in seen_canon:
                seen_canon.add(c)
                unique.append((url, src))
        unique = unique[:MAX_URLS_PER_DOMAIN]
        log.debug(f"[ANZ] {domain}: {len(unique)} unique candidate(s) after dedup: "
                  + ", ".join(f"{src}:{url}" for url, src in unique))

        async def process_url(url, source):
            text, headings = await gated_sync(
                limiter, fetch_and_clean, url, domain=domain
            )
            if not text:
                return None
            res = await _classify(url, text, domain,
                                  security_txt=sec_body, headings=headings)
            return _finding(domain, url, source, res) if res else None

        results = await aio.gather(*[process_url(u, s) for u, s in unique])
        findings = [r for r in results if r is not None]

        log.info(f"[ANZ] {domain} FIN: {len(findings)} findings "
                 f"(candidates: {len(unique)})")
        if findings:
            for f in findings:
                log.debug(f"[ANZ] {domain} finding via [{f['source'].upper()}]: "
                          f"{f['url']} → {f['label']} ({f['confidence']:.2f})")
        if ui:
            ui.v(2, f"{domain}: {len(findings)} findings "
                    f"from {len(unique)} candidate(s)")
        return {"domain": domain, "findings": findings}


async def run_workflow(domains, workers=8, limiter=None, ui=None, results=None):
    """Analyze `domains`; per-domain results are appended to `results` as they
    finish, so a caller holding the list can save partial progress."""
    if limiter is None:
        limiter = domain_limiter
    sem = aio.Semaphore(max(1, workers))
    total = len(domains)
    results = [] if results is None else results
    start = time.monotonic()

    if ui:
        ui.phase("probing", total)
        ui.tally([])  # show the live PAID_BB / VDP counts from the start

    tasks = [aio.create_task(analyze_domain(d, sem, limiter, ui)) for d in domains]
    for t in aio.as_completed(tasks):
        r = await t
        results.append(r)
        if ui:
            ui.tally(r["findings"])
            ui.advance()

    if ui:
        resolved = sum(1 for r in results if r["findings"])
        ui.checkpoint(
            f"probing complete: {total} domain(s) in "
            f"{fmt_secs(time.monotonic() - start)} — "
            f"{resolved} with findings, {total - resolved} empty"
        )
    return results