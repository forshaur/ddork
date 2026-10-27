"""Rate/ETA helpers and a sqlmap-style progress bar. Stdlib only."""
import shutil
import sys
import threading
import time
from collections import deque

from .colors import palette, visible_len

# --- ETA tuning ---------------------------------------------------------
# Windowed rate, not cumulative: cumulative averages lag badly after a fast
# start and whip the estimate around when throughput changes.
ETA_WINDOW = 20.0
ETA_MIN_SAMPLES = 5
ETA_MIN_SPAN = 2.0
ETA_SMOOTHING = 0.6

_MIN_REDRAW_INTERVAL = 0.08
_TICK_INTERVAL = 0.5


def fmt_secs(secs):
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def mmss(secs):
    """MM:SS, or HH:MM:SS past an hour (sqlmap's ETA format)."""
    secs = max(0, int(secs))
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def rate_per_min(start, done):
    elapsed = time.monotonic() - start
    return done / elapsed * 60 if elapsed > 0 else 0.0


def eta_str(start, done, total):
    if not total or done <= 0:
        return "?"
    elapsed = time.monotonic() - start
    rate = done / elapsed if elapsed > 0 else 0
    if rate <= 0:
        return "?"
    return fmt_secs((total - done) / rate)


class ProgressBar:
    """In-place bar in sqlmap's style:

          [probing]  93% [===================>] 28/30 · PAID_BB 4 · VDP 7 (ETA 00:05)

    Phases are tagged like [probing], [enumerating]; brackets are neutral
    (bright white), the label is brand (cyan), the bar fill is green, the
    percentage is bold white, and the trailing count and ETA recede in grey.

    An optional one-line status (e.g. a blocked provider) sits directly above
    the bar; both lines are redrawn together in place. Every line is fitted to
    the terminal width, since a wrapped line breaks in-place redraws.
    Suppressed when the stream is not a TTY.
    """

    # Bar budget. Bar width is computed from MAX_LABEL_WIDTH (not the current
    # label) so the bar's right edge stays in the same place across phases,
    # while the label itself is never padded — proximity beats alignment.
    MAX_LABEL_WIDTH = 13   # len("[enumerating]")
    MAX_WIDTH = 100        # don't stretch across very wide terminals
    MIN_SLOTS = 6          # shrink the bar this far before dropping the tally
    INDENT = "  "
    ETA_WIDTH = len("(ETA 00:00:00)")

    def __init__(self, label, total, stream=None):
        self.label = label
        self.total = max(int(total), 1)
        self.stream = stream or sys.stdout
        self.is_tty = _isatty(self.stream)
        self._amount = 0
        self._start = time.monotonic()
        self._eta = None
        self._eta_at = None
        self._samples = deque(maxlen=128)   # (monotonic_time, amount)
        self._last_draw = 0.0
        self._lines = 0                     # lines currently on screen (0-2)
        self._status = ""
        self._suffix = []                   # [(text, palette role)]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ticker = None
        self._aborted = False
        self._draw(force=True)

    # -- public ----------------------------------------------------------

    def update(self, amount):
        with self._lock:
            self._amount = max(0, min(int(amount), self.total))
            self._estimate()
            self._draw(force=self._amount >= self.total)

    def advance(self, n=1):
        with self._lock:
            self._amount = max(0, min(self._amount + n, self.total))
            self._estimate()
            self._draw(force=self._amount >= self.total)

    def set_status(self, text):
        """One plain-text line above the bar; empty removes it."""
        with self._lock:
            if text == self._status:
                return
            self._status = text or ""
            self._draw(force=True)

    def set_suffix(self, segments):
        """Extra [(text, palette role)] shown after the count, e.g. live tallies."""
        with self._lock:
            self._suffix = list(segments)
            self._draw(force=True)

    def tick(self):
        with self._lock:
            self._draw()

    def start_ticker(self):
        if not self.is_tty or self._ticker:
            return

        def _loop():
            while not self._stop.wait(_TICK_INTERVAL):
                self.tick()

        self._ticker = threading.Thread(target=_loop, daemon=True)
        self._ticker.start()

    def pause(self):
        self._stop.set()
        if self._ticker:
            self._ticker.join(timeout=1)
            self._ticker = None
        with self._lock:
            self._clear()

    def resume(self):
        with self._lock:
            self._draw(force=True)
        self._stop.clear()
        self.start_ticker()

    def finish(self):
        self.pause()

    def abort(self):
        """Stop drawing and wipe the bar without taking the lock.

        Safe from a signal handler, which may interrupt a thread holding it.
        """
        self._aborted = True
        self._stop.set()
        if self.is_tty and self._lines:
            self.stream.write(self._wipe())
            self.stream.flush()
            self._lines = 0

    # -- internals -------------------------------------------------------

    def _estimate(self):
        now = time.monotonic()
        done = self._amount
        if done <= 0:
            return

        self._samples.append((now, done))

        cutoff = now - ETA_WINDOW
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

        if len(self._samples) < ETA_MIN_SAMPLES:
            return
        span = self._samples[-1][0] - self._samples[0][0]
        if span < ETA_MIN_SPAN:
            return

        dn = self._samples[-1][1] - self._samples[0][1]
        if span <= 0 or dn <= 0:
            return

        rate = dn / span
        raw = (self.total - done) / rate

        if self._eta is None:
            self._eta = raw
        else:
            held = max(0.0, self._eta - (now - self._eta_at))
            self._eta = ETA_SMOOTHING * held + (1 - ETA_SMOOTHING) * raw
        self._eta_at = now

    def _live_eta(self):
        if self._eta is None:
            return None
        if self._eta_at is None:
            return self._eta
        return max(0.0, self._eta - (time.monotonic() - self._eta_at))

    def _width(self):
        cols = shutil.get_terminal_size(fallback=(80, 24)).columns
        return max(20, min(self.MAX_WIDTH, cols - 1))

    def _render(self, width):
        pct = min(100, round(self._amount / self.total * 100))
        count = f"{self._amount}/{self.total}"
        label_disp = (f"{palette.neutral('[')}"
                      f"{palette.brand(self.label)}"
                      f"{palette.neutral(']')}")

        suffix = ""
        if self._suffix:
            suffix = f" {palette.muted('·')} ".join(
                getattr(palette, role)(text) for text, role in self._suffix
            )
            suffix = f" {palette.muted('·')} {suffix}"

        fixed = (len(self.INDENT) + self.MAX_LABEL_WIDTH + 1 + 4 + 1 + 2 + 1
                 + len(count) + 1 + self.ETA_WIDTH)
        if width < fixed + 3:
            # Too narrow for a bar: keep the essentials on one line.
            compact = (f"{self.INDENT}{label_disp} "
                       f"{palette.neutral(f'{pct}%')} {palette.muted(count)}")
            if visible_len(compact + suffix) <= width:
                compact += suffix
            return compact
        slots = width - fixed - visible_len(suffix)
        if slots < self.MIN_SLOTS and suffix:
            suffix, slots = "", width - fixed  # the bar matters more than the tally
        slots = max(3, slots)

        filled = int(round(pct / 100 * slots))
        if filled <= 0:
            inner = palette.progress(">") + " " * (slots - 1)
        elif filled >= slots:
            inner = palette.progress("=" * slots)
        else:
            inner = (palette.progress("=" * (filled - 1) + ">")
                     + " " * (slots - filled))

        eta = self._live_eta()
        eta_s = mmss(eta) if eta is not None else "??:??"

        return (f"{self.INDENT}{label_disp} "
                f"{palette.neutral(f'{pct:>3}%')} "
                f"[{inner}] "
                f"{palette.muted(count)}"
                f"{suffix} "
                f"{palette.muted(f'(ETA {eta_s})')}")

    def _render_status(self, width):
        mark = f"{palette.neutral('[')}{palette.warn('!')}{palette.neutral(']')}"
        room = width - len(self.INDENT) - 4
        text = self._status
        if len(text) > room:
            text = text[:max(0, room - 1)] + "…"
        return f"{self.INDENT}{mark} {palette.warn(text)}"

    def _wipe(self):
        """Clear the lines drawn last time; leaves the cursor at the top one."""
        return "\r\x1b[2K" + "\x1b[1A\r\x1b[2K" * max(0, self._lines - 1)

    def _draw(self, force=False):
        if not self.is_tty or self._aborted:
            return
        now = time.monotonic()
        if not force and now - self._last_draw < _MIN_REDRAW_INTERVAL:
            return
        self._last_draw = now
        width = self._width()
        lines = [self._render(width)]
        if self._status:
            lines.insert(0, self._render_status(width))
        self.stream.write(self._wipe() + "\n".join(lines))
        self.stream.flush()
        self._lines = len(lines)

    def _clear(self):
        if not self.is_tty or not self._lines:
            return
        self.stream.write(self._wipe())
        self.stream.flush()
        self._lines = 0


def _isatty(stream):
    try:
        return bool(stream.isatty())
    except Exception:
        return False