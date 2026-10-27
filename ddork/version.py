"""Version detection and update checking.

Latest versions come from GitHub raw: the `version` field of each repo's
pyproject.toml (both packages install from git). Results are cached in
~/.ddork for CACHE_TTL so most runs start without touching the network.

Every network call is best-effort: a failure returns None, never raises.
Stdlib only (urllib + json), so no new dependency.
"""
import json
import os
import threading
import time
import urllib.request
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .config import (
    CLASSIFIER_GITHUB,
    CLASSIFIER_PACKAGE,
    DDORK_GITHUB,
    DDORK_HOME,
    DDORK_PACKAGE,
    ensure_home,
    log,
)

_TIMEOUT = 3
CHECK_BUDGET = _TIMEOUT + 1  # wall-clock cap for the whole startup check
CACHE_TTL = 24 * 3600
_CACHE_FILE = os.path.join(DDORK_HOME, "update_check.json")


# --- installed versions --------------------------------------------------

def get_installed_version(package):
    """Return the installed distribution version, or None if not installed."""
    try:
        return _pkg_version(package)
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def get_ddork_version():
    """ddork's own version. Falls back to the package __version__."""
    try:
        from . import __version__
        if __version__:
            return __version__
    except Exception:
        pass
    return get_installed_version(DDORK_PACKAGE)


def get_classifier_version():
    """isbounty's installed version, however it was installed.

    Import is best-effort — a missing or broken isbounty returns None and
    the banner renders "(not installed)". The classifier wrapper in
    classifier.py performs the real availability check; this is only for
    display.
    """
    try:
        import isbounty
        v = getattr(isbounty, "__version__", None)
        if v:
            return str(v)
    except Exception:
        pass
    return get_installed_version(CLASSIFIER_PACKAGE)


# --- version comparison --------------------------------------------------

def _version_tuple(v):
    """Parse '1.2.3' into (1, 2, 3). Non-numeric segments become 0."""
    parts = []
    for seg in (v or "").split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_outdated(current, latest):
    """True if `latest` is strictly newer than `current`."""
    if not current or not latest:
        return False
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return False


# --- latest versions -----------------------------------------------------

def fetch_github_version(repo, path="pyproject.toml", timeout=_TIMEOUT):
    """Version declared in `path` on the repo's default branch, or None."""
    if not repo:
        return None
    url = f"https://raw.githubusercontent.com/{repo}/main/{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ddork"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        return _parse_version_from_toml(text)
    except Exception:
        return None


def _parse_version_from_toml(text):
    """Extract `version = "x.y.z"` from a pyproject.toml snippet."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("version") and "=" in line:
            _, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if val:
                return val
    return None


# --- cache ---------------------------------------------------------------

def _read_cache():
    """(ddork_latest, classifier_latest) if checked within CACHE_TTL, else None."""
    try:
        with open(_CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if time.time() - data["checked_at"] < CACHE_TTL:
            return data["ddork"], data["isbounty"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_cache(d_latest, c_latest):
    # Only cache a complete answer, so a network blip doesn't hide updates for a day.
    if not (d_latest and c_latest):
        return
    try:
        ensure_home()
        with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"checked_at": time.time(),
                       "ddork": d_latest, "isbounty": c_latest}, fh)
    except OSError as e:
        log.debug(f"[UPD] cannot write update cache: {e}")


# --- combined check ------------------------------------------------------

def _fetch_latest(*repos, budget=CHECK_BUDGET):
    """Latest version per repo, fetched in parallel, within `budget` seconds total.

    urlopen's timeout does not cover DNS resolution, so a hung resolver can
    block it indefinitely. Daemon threads plus a bounded join keep startup
    and process exit from waiting on that; a repo that runs out of time is None.
    """
    out = [None] * len(repos)

    def work(i, repo):
        out[i] = fetch_github_version(repo)

    threads = [threading.Thread(target=work, args=(i, r), daemon=True)
               for i, r in enumerate(repos)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + budget
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return out

def check_updates():
    """Return (ddork_info, classifier_info). Never raises.

    Each info is a dict::
        {name, version, latest, outdated, error}
    """
    d_installed = get_ddork_version()
    c_installed = get_classifier_version()

    cached = _read_cache()
    if cached:
        d_latest, c_latest = cached
    else:
        # GitHub only: both packages install from git, and a similarly-named
        # PyPI package would produce a false "outdated" warning (this already
        # bit us once).
        d_latest, c_latest = _fetch_latest(DDORK_GITHUB, CLASSIFIER_GITHUB)
        _write_cache(d_latest, c_latest)

    d_info = {
        "name": DDORK_PACKAGE,
        "version": d_installed,
        "latest": d_latest,
        "outdated": is_outdated(d_installed, d_latest),
        "error": None if d_installed else "not installed",
    }
    c_info = {
        "name": CLASSIFIER_PACKAGE,
        "version": c_installed,
        "latest": c_latest,
        "outdated": is_outdated(c_installed, c_latest),
        "error": None if c_installed else "not installed",
    }
    return d_info, c_info