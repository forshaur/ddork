"""Terminal output in sqlmap's style: permanent lines plus one in-place
progress bar (with an optional status line above it). Colors auto-disable
on non-TTY.

Roles (see colors.Palette): brand for phases, success/error for [+]/[!],
warn for the status line, neutral for brackets, muted for secondary text.

Verbosity:
  0  quiet     — no UI, only the final summary
  1  normal    — permanent lines + bar (default)
  2  verbose   — adds per-source and per-domain checkpoints
  3  debug     — UI disabled; raw logger takes over
"""
import threading

from .colors import palette
from .progress import ProgressBar

# Labels tallied live on the probing bar (NOT_PROGRAM would only add noise).
_TALLY = (("PAID_BB", "success"), ("VDP", "brand"))


class ScanUI:
    def __init__(self, verbose=1):
        self.verbose = verbose
        self._bar = None
        self._hinted = False
        self._seen = set()                 # (label, link) already tallied
        self._tally = dict.fromkeys((label for label, _ in _TALLY), 0)
        self._tally_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self._bar:
            self._bar.finish()
            self._bar = None

    def abort(self):
        """Signal-handler-safe close: stop the bar without waiting on its lock."""
        if self._bar:
            self._bar.abort()
            self._bar = None

    # -- permanent lines -------------------------------------------------

    def line(self, text):
        if self._bar:
            self._bar.pause()
        print(text, flush=True)
        if self._bar:
            self._bar.resume()

    def checkpoint(self, text, ok=True):
        mark = palette.success("+") if ok else palette.error("!")
        tag = f"{palette.neutral('[')}{mark}{palette.neutral(']')}"
        self.line(f"  {tag} {text}")

    def error(self, text):
        self.checkpoint(text, ok=False)

    def v(self, level, text, ok=True):
        """Emit only when verbosity >= level."""
        if self.verbose >= level:
            self.checkpoint(text, ok=ok)

    # -- live bar --------------------------------------------------------

    def phase(self, label, total):
        """Start (or restart) the single live bar for a phase."""
        if self._bar:
            self._bar.finish()
        if not self._hinted:
            self._hinted = True
            self.line(palette.muted("  ctrl-c stops and saves progress"))
        self._bar = ProgressBar(label, total)
        self._bar.start_ticker()

    def advance(self, n=1):
        if self._bar:
            self._bar.advance(n)

    def status(self, text):
        """One line above the bar (e.g. a blocked provider); empty clears it."""
        if self._bar:
            self._bar.set_status(text)

    def tally(self, findings):
        """Add one domain's findings to the live PAID_BB / VDP counts on the bar.

        Counts unique links per label, the same way the saved lists do.
        """
        with self._tally_lock:
            for f in findings:
                if f["label"] not in self._tally:
                    continue
                key = (f["label"], f.get("url") or f["domain"])
                if key not in self._seen:
                    self._seen.add(key)
                    self._tally[f["label"]] += 1
            segments = [(f"{label} {self._tally[label]}", role)
                        for label, role in _TALLY]
        if self._bar:
            self._bar.set_suffix(segments)
