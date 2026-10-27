"""Fetch + clean a page into (text, headings), mirroring isBounty's _clean_html.

Uses the shared per-thread curl_cffi session so we don't pay TCP+TLS setup
per page.
"""
from bs4 import BeautifulSoup as bs

from .config import log
from .net import get_session, get_user_agent

_STRIP = ["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]


def fetch_and_clean(url, timeout=12):
    """Return (text, headings) or (None, None)."""
    if not url:
        return None, None
    try:
        log.info(f"[SCRAPE] {url}")
        r = get_session().get(url, headers={"User-Agent": get_user_agent()}, timeout=timeout)
        log.info(f"[SCRAPE] {url} stat: {r.status_code} len: {len(r.text)}")
        if r.status_code != 200:
            return None, None
        soup = bs(r.text, "html.parser")
        for t in soup(_STRIP):
            t.decompose()
        headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])]
        headings = [h for h in headings if h]
        candidates = soup.find_all(["main", "article", "div", "section", "body"])
        best = max(candidates, key=lambda t: len(t.get_text(strip=True)), default=soup)
        text = best.get_text("\n", strip=True) if best else soup.get_text("\n", strip=True)
        log.info(f"[SCRAPE] {url} extracted len: {len(text)} headings: {len(headings)}")
        return text, headings
    except Exception as e:
        log.error(f"[SCRAPE] {url} err: {e}")
        return None, None