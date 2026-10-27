"""Logging setup and shared constants."""
import logging as lg
import os
import sys

log = lg.getLogger("scan")

# Per-user state: scan.log, saved program lists, update-check cache.
DDORK_HOME = os.path.join(os.path.expanduser("~"), ".ddork")
LOG_FILE = os.path.join(DDORK_HOME, "scan.log")


def ensure_home():
    """Create DDORK_HOME if needed and return it."""
    os.makedirs(DDORK_HOME, exist_ok=True)
    return DDORK_HOME


def display_path(path):
    """`path` with the home directory shown as ~, for terminal output."""
    home = os.path.expanduser("~")
    path = str(path)
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


# 0 quiet · 1 normal · 2 verbose · 3 debug
_LOG_LEVELS = {0: lg.WARNING, 1: lg.INFO, 2: lg.INFO, 3: lg.DEBUG}


def configure_logging(verbose):
    """Call once, early, from cli.main().

    Level 3 (debug): stream + file logging, no live UI (cli disables it).
    Levels 0–2:     file-only logging; stdout is owned by ui.ScanUI instead.

    All handlers write UTF-8. Windows' default cp1252 crashes on any non-Latin-1
    character in a logged URL (seen in the wild: an emoji inside a DDG result
    URL), which takes down the whole process because logging's default error
    path is to print a traceback and keep going — but the traceback itself
    prints to the same broken stream, so the run dies.

    The log lives at LOG_FILE and is replaced each run, so it never grows
    without bound or litters the working directory. Timestamps go to the file
    only; the terminal never shows dates.
    """
    _reconfigure_std_streams()

    handlers = []
    try:
        ensure_home()
        fh = lg.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
        fh.setFormatter(lg.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        handlers.append(fh)
    except OSError:
        pass  # unwritable home: run without a log file rather than crash
    if verbose >= 3:
        sh = lg.StreamHandler(sys.stdout)
        sh.setFormatter(lg.Formatter("[%(levelname)s] %(message)s"))
        handlers.append(sh)
    if not handlers:
        handlers.append(lg.NullHandler())  # keep logging's stderr fallback off the UI

    lg.basicConfig(
        level=_LOG_LEVELS.get(verbose, lg.INFO),
        handlers=handlers,
        force=True,
    )


def _reconfigure_std_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --- Fallback user agents (used if the remote UA endpoint is unavailable) ---
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

DEFAULT_TIMEOUT = 15
IMPERSONATE = "chrome110"

# --- Package metadata ---------------------------------------------------
# ddork's own GitHub repo. Set to "user/repo" if/when it has a public repo;
# None disables the GitHub check and falls back to PyPI only.
DDORK_PACKAGE = "ddork"
DDORK_GITHUB = "forshaur/ddork"
DDORK_GIT_URL = "https://github.com/forshaur/ddork"

# Classifier. Installed from git, so the "latest" check reads the version
# field from the repo's pyproject.toml rather than PyPI.
CLASSIFIER_PACKAGE = "isbounty"
CLASSIFIER_GITHUB = "forshaur/isbounty"
CLASSIFIER_GIT_URL = "https://github.com/forshaur/isbounty"