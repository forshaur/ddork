"""Fetch + clean a page into (text, headings), mirroring isBounty's _clean_html.

Uses the shared per-thread curl_cffi session so we don't pay TCP+TLS setup
per page.
"""
from bs4 import BeautifulSoup as bs

from .config import log
from .net import get_session, get_user_agent

_STRIP = ["script", "style", "nav", "noscript", "svg"]


def _decode_cf_email(cf_hex):
    try:
        k = int(cf_hex[:2], 16)
        return "".join(chr(int(cf_hex[i:i+2], 16) ^ k) for i in range(2, len(cf_hex), 2))
    except Exception:
        return ""


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

        # De-obfuscate Cloudflare protected emails across DOM
        for cf in soup.find_all(attrs={"data-cfemail": True}):
            decoded = _decode_cf_email(cf.get("data-cfemail", ""))
            if decoded:
                cf.replace_with(decoded)
        for a in soup.find_all("a", href=True):
            if "/cdn-cgi/l/email-protection#" in a["href"]:
                cf_hex = a["href"].split("/email-protection#")[-1]
                decoded = _decode_cf_email(cf_hex)
                if decoded:
                    a.replace_with(decoded)

        # Harvest security reporting channels (mailtos and platforms) before tag removal
        extra_channels = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                clean_email = href[7:].split("?")[0].strip()
                if clean_email:
                    extra_channels.append(clean_email)
            elif any(p in href.lower() for p in ("hackerone.com", "bugcrowd.com", "intigriti.com", "yeswehack.com")):
                extra_channels.append(href)

        for t in soup(_STRIP):
            t.decompose()
        headings = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])]
        headings = [h for h in headings if h]
        candidates = soup.find_all(["main", "article", "div", "section", "body"])
        best = max(candidates, key=lambda t: len(t.get_text(strip=True)), default=soup)
        text = best.get_text("\n", strip=True) if best else soup.get_text("\n", strip=True)

        if extra_channels:
            text += "\n\nReporting channels:\n" + "\n".join(sorted(set(extra_channels)))

        log.info(f"[SCRAPE] {url} extracted len: {len(text)} headings: {len(headings)}")
        return text, headings
    except Exception as e:
        log.error(f"[SCRAPE] {url} err: {e}")
        return None, None