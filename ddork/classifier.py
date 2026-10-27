"""Lazy, defensive loader for the isbounty classifier.

isbounty is a separate package (git-only). Historically it was imported at
module load in analyzer.py, so a missing or broken install crashed the whole
process with a bare ModuleNotFoundError before argparse could even handle
--help or --update.

This module centralises all access to isbounty:

  * imports happen lazily, on first use;
  * the result is cached so repeated calls are cheap;
  * a missing, broken, or API-incompatible install produces a
    ClassifierUnavailable with a formatted, actionable message;
  * `preflight()` reports availability without raising, so the CLI can
    decide what to do before the workflow starts.

The wrapper deliberately exposes a narrow API so nothing outside this module
needs to know about isbounty's internal layout.
"""
import re
import textwrap

from .colors import palette
from .config import CLASSIFIER_GIT_URL, log


# --- public exception ----------------------------------------------------

class ClassifierUnavailable(RuntimeError):
    """Raised when the isbounty classifier cannot be used.

    `reason` is a short human string ("isbounty is not installed"). `cause`
    is the underlying exception if any. `str(exc)` returns the multi-line
    install hint (plain, uncolored) suitable for logs.
    """

    def __init__(self, reason, cause=None):
        super().__init__(reason)
        self.reason = reason
        self.cause = cause

    def __str__(self):
        return _install_hint(self.reason)


# --- install hint (plain text, used in exception + log) ------------------

def _install_hint(reason):
    return textwrap.dedent(f"""\
        classifier unavailable: {reason}

        The classification stage needs the isbounty package, which performs
        the actual bug-bounty / VDP policy analysis.

        Install or repair it with:

            ddork --update
            # or, directly:
            pip install --upgrade git+{CLASSIFIER_GIT_URL}

        Then re-run this command. To skip classification and only enumerate
        competitors, re-run with --enumerate-only.
        """).rstrip()


# --- fallback sentence splitter ------------------------------------------
# Used only if isbounty is present but its text_utils module has moved
# (defensive against API churn). Quality is slightly lower than isbounty's
# own splitter, but keeps the pipeline working.

_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _fallback_split_sentences(text):
    if not text:
        return []
    return [s.strip() for s in _SENT_RE.split(text) if s.strip()]


# --- loader --------------------------------------------------------------

class _Classifier:
    """Lazy singleton wrapper around isbounty."""

    def __init__(self):
        self._attempted = False
        self._ok = False
        self._error = None            # the exception that blocked loading
        self._reason = None           # human string, e.g. "not installed"
        self._pipeline = None
        self._PageContent = None
        self._split_sentences = None

    # -- loading ---------------------------------------------------------

    def _load(self):
        """Try to import isbounty. Cached after first call. Never raises."""
        if self._attempted:
            return self._ok
        self._attempted = True

        # Step 1: top-level package.
        try:
            import isbounty  # noqa: F401
        except ModuleNotFoundError as e:
            # Distinguish "isbounty itself is missing" from "isbounty is
            # present but something it depends on is missing".
            missing = getattr(e, "name", "") or ""
            if missing == "isbounty" or missing.startswith("isbounty."):
                self._reason = "isbounty is not installed"
            else:
                self._reason = (f"isbounty is installed but its dependency "
                                f"'{missing}' is missing")
            self._error = e
            log.warning(f"[CLS] {self._reason}")
            return False
        except Exception as e:
            self._reason = f"isbounty failed to import: {e.__class__.__name__}: {e}"
            self._error = e
            log.warning(f"[CLS] {self._reason}")
            return False

        # Step 2: the public API surface we rely on.
        try:
            from isbounty import Pipeline, PageContent
        except ImportError as e:
            self._reason = ("isbounty is installed but does not export the "
                            "expected API (Pipeline / PageContent)")
            self._error = e
            log.warning(f"[CLS] {self._reason}")
            return False
        except Exception as e:
            self._reason = f"isbounty import failed: {e.__class__.__name__}: {e}"
            self._error = e
            log.warning(f"[CLS] {self._reason}")
            return False

        # Step 3: internal helper. Fallback if missing — this is the piece
        # most likely to move between isbounty versions.
        try:
            from isbounty.core.text_utils import split_sentences
        except Exception:
            split_sentences = _fallback_split_sentences
            log.info("[CLS] isbounty.core.text_utils.split_sentences not "
                     "importable; using fallback regex splitter")

        # Step 4: pipeline construction. Anything raised here means the
        # install is present but broken — same treatment as a missing module.
        try:
            pipeline = Pipeline()
        except Exception as e:
            self._reason = (f"isbounty.Pipeline() failed to initialise: "
                            f"{e.__class__.__name__}: {e}")
            self._error = e
            log.warning(f"[CLS] {self._reason}")
            return False

        self._pipeline = pipeline
        self._PageContent = PageContent
        self._split_sentences = split_sentences
        self._ok = True
        log.info("[CLS] isbounty loaded")
        return True

    # -- public API ------------------------------------------------------

    @property
    def available(self):
        """True if isbounty can be used. Triggers (cached) load on first call."""
        return self._load()

    @property
    def reason(self):
        """Human-readable reason if unavailable, else None."""
        self._load()
        return self._reason if not self._ok else None

    def require(self):
        """Raise ClassifierUnavailable if isbounty is not usable."""
        if not self._load():
            raise ClassifierUnavailable(self._reason, cause=self._error)

    def preflight(self):
        """Return (ok, reason, hint). Does not raise, does not print."""
        ok = self._load()
        if ok:
            return True, None, None
        return False, self._reason, _install_hint(self._reason)

    def classify_page(self, *, url, raw_text, headings, domain, security_txt):
        """Run the pipeline on a page. Raises ClassifierUnavailable if not loaded."""
        self.require()
        sentences = self._split_sentences(raw_text)
        page = self._PageContent(
            url=url,
            raw_text=raw_text,
            sentences=sentences,
            headings=headings or [],
            domain=domain,
            security_txt=security_txt,
        )
        return self._pipeline.run_from_page(page)


classifier = _Classifier()


# --- CLI-facing rendering ------------------------------------------------

def print_preflight_error(reason=None):
    """Colored, structured rendering of the preflight failure.

    Follows the bracket convention: brackets and parens are neutral (white),
    only the content between them takes a semantic color.
    """
    reason = reason or classifier.reason or "unavailable"

    mark = palette.error("X")
    tag = f"{palette.neutral('[')}{mark}{palette.neutral(']')}"

    print()
    print(f"  {tag} {palette.bold('classifier unavailable')}")
    print()
    _paragraph("The classification stage needs the isbounty package, "
               "which performs the actual bug-bounty / VDP policy analysis.")
    print()
    print(f"  {palette.warn('Reason:')} {palette.muted(reason)}")
    print()
    print(f"  {palette.muted('Install or repair it with:')}")
    print()
    print(f"      {palette.brand('ddork --update')}")
    print(f"      {palette.muted('# or, directly:')}")
    print(f"      {palette.brand(f'pip install --upgrade git+{CLASSIFIER_GIT_URL}')}")
    print()
    print(f"  {palette.muted('Then re-run this command.')}")
    print()
    print(f"  {palette.muted('To skip classification and only enumerate competitors,')}")
    print(f"  {palette.muted('re-run with')} {palette.brand('--enumerate-only')}"
          f"{palette.muted('.')}")
    print()


def _paragraph(text, indent=2, width=76):
    for line in textwrap.wrap(text, width=width - indent):
        print(" " * indent + palette.muted(line))