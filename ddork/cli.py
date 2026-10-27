"""Command-line entry point."""
import argparse
import logging
import os
import signal
import sys

from .colors import palette
from .config import configure_logging, display_path, log
from .report import LABELS


def build_parser():
    p = argparse.ArgumentParser(
        description="Discover competitor domains and classify their bug-bounty/VDP policy."
    )
    p.add_argument("-u", help="seed URL/domain to enumerate competitors from")
    p.add_argument("-f", help="file of domains to analyze (one per line), skips enumeration")
    p.add_argument(
        "-o", nargs="+", metavar=("FILE", "LABEL"),
        help="save findings to FILE as TSV. Optional LABEL filters to one of "
             f"({'|'.join(LABELS)}).",
    )
    p.add_argument("-n", "--min-targets", type=int, default=None,
                   help="stop enumeration once this many total targets are found")
    p.add_argument("-w", type=int, default=5, help="worker concurrency (default: 5)")
    p.add_argument("--delay", type=float, default=0.1,
                   help="min seconds between requests for provider calls (default: 0.1)")
    p.add_argument("--min-conf", type=float, default=None,
                   help="minimum confidence for -o output (within-label; do not use as "
                        "a global filter — NOT_PROGRAM always scores 0.9)")
    p.add_argument("--store", default=None,
                   help="SQLite path for cross-run seen/new tracking")
    p.add_argument("--new-only", action="store_true",
                   help="show only findings first seen this run (requires --store)")
    p.add_argument("--all", action="store_true",
                   help="also save NOT_PROGRAM links to ~/.ddork/not_program.txt")
    p.add_argument("-v", "--verbose", type=int, default=1, choices=(0, 1, 2, 3),
                   metavar="LEVEL",
                   help="0=quiet · 1=normal (default) · 2=verbose · 3=debug "
                        "(level 3 disables the live UI, streams log to stdout)")
    p.add_argument("--debug", action="store_true",
                   help="alias for -v 3")
    p.add_argument("--update", action="store_true",
                   help="update ddork and the isbounty classifier via pip and exit")
    p.add_argument("--no-banner", action="store_true",
                   help="skip the startup banner (logo + version lines)")
    p.add_argument("--no-tips", action="store_true",
                   help="skip the helpful tip shown at the end of a run")
    p.add_argument(
        "--enumerate-only", nargs="?", const="targets.txt", default=None, metavar="FILE",
        help="stop after enumeration, save discovered domains to FILE and exit. "
             "Does not require the classifier.",
    )
    return p


def _fail(ui, msg, code=1):
    """Show an error the user can see (not just in scan.log) and return `code`."""
    log.error(msg)
    if ui:
        ui.error(msg)
    else:
        mark = f"{palette.neutral('[')}{palette.error('!')}{palette.neutral(']')}"
        print(f"  {mark} {msg}", file=sys.stderr, flush=True)
    return code


def _say(msg):
    log.info(msg)
    mark = f"{palette.neutral('[')}{palette.success('+')}{palette.neutral(']')}"
    print(f"  {mark} {msg}", flush=True)


class _Progress:
    """What a run has collected so far, so Ctrl-C can save it."""
    args = None
    label_filter = None
    ui = None
    src = None
    enumerator = None  # live during enumeration
    domains = None     # final target list once known
    results = None     # per-domain results, appended as each domain finishes


_progress = _Progress()


def _save_results(results, src, args, label_filter):
    """Print the counts and write program lists (+ -o TSV). Returns an exit code."""
    from .report import print_summary, save_programs, save_urls
    print_summary(results)

    try:
        written = save_programs(results, src, include_not_program=args.all)
    except OSError as e:
        return _fail(None, f"cannot save program lists: {e.strerror or e}")
    for label, path, count in written:
        _say(f"{count} {label} link(s) appended to {display_path(path)}")
    if not written:
        print(f"  {palette.muted('no program links to save this run')}")

    if args.o:
        try:
            count = save_urls(
                results, args.o[0],
                label_filter=label_filter, min_conf=args.min_conf,
            )
        except OSError as e:
            return _fail(None, f"cannot write {args.o[0]}: {e.strerror or e}")
        extras = []
        if label_filter:
            extras.append(f"label={label_filter}")
        if args.min_conf is not None:
            extras.append(f"min_conf={args.min_conf}")
        _say(
            f"saved {count} row(s) to {args.o[0]}"
            + (f" ({', '.join(extras)})" if extras else "")
        )
    return 0


def _save_progress():
    """On Ctrl-C: save finished results, and the domains still to do for -f resume."""
    p = _progress
    if p.args is None:
        return  # interrupted before the run started; nothing collected
    results = list(p.results or [])
    if p.domains is not None:
        domains = list(p.domains)
    elif p.enumerator is not None:
        domains = list(p.enumerator.domains)
    else:
        domains = []
    done = {r["domain"] for r in results}
    remaining = [d for d in domains if d and d not in done]

    if results:
        _save_results(results, f"{p.src} (interrupted)", p.args, p.label_filter)
    if not remaining:
        return
    if p.args.enumerate_only:
        path = p.args.enumerate_only
    else:
        from .config import ensure_home
        path = os.path.join(ensure_home(), "resume.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(remaining) + "\n")
    except OSError as e:
        _fail(None, f"cannot write {path}: {e.strerror or e}")
        return
    if p.args.enumerate_only:
        _say(f"saved {len(remaining)} domain(s) found so far to {display_path(path)}")
    else:
        shown = display_path(path)
        _say(f"{len(remaining)} unprocessed domain(s) saved to {shown}")
        print(f"      resume with: {palette.bold(f'ddork -f {shown}')}", flush=True)


async def main(logo_already_printed=False):
    parser = build_parser()
    args = parser.parse_args()
    verbose = 3 if args.debug else args.verbose
    configure_logging(verbose)

    # --update short-circuits everything else — updates ddork & isbounty via pip.
    if args.update:
        from .banner import update_packages
        return 0 if update_packages() else 1

    # Validate every argument before the banner, update check and classifier
    # preflight, so a typo fails instantly with the real reason.
    label_filter = None
    if args.o and len(args.o) > 2:
        parser.error(f"-o takes at most FILE and LABEL, got: {' '.join(args.o)}")
    if args.o and len(args.o) == 2:
        label_filter = args.o[1].upper()
        if label_filter not in LABELS:
            parser.error(
                f"invalid -o label '{args.o[1]}', expected one of: {', '.join(LABELS)}"
            )
    if args.new_only and not args.store:
        parser.error("--new-only requires --store")
    if not args.u and not args.f:
        parser.error("need -u (seed domain) or -f (file of domains)")
    if args.u and not args.f and not args.min_targets:
        parser.error("-u needs -n/--min-targets (pool expansion has no depth cap)")

    # Expand ~ ourselves: the resume hint prints ~/.ddork/..., and cmd.exe
    # passes a literal ~ through.
    for name in ("f", "enumerate_only", "store"):
        if getattr(args, name):
            setattr(args, name, os.path.expanduser(getattr(args, name)))
    if args.o:
        args.o[0] = os.path.expanduser(args.o[0])

    from .net import normalize_domain
    domains = None
    if args.f:
        try:
            with open(args.f, encoding="utf-8") as fh:
                domains = [normalize_domain(l.strip()) for l in fh if l.strip()]
        except OSError as e:
            return _fail(None, f"cannot read {args.f}: {e.strerror or e}")

    # Banner first so the user always sees version info, including the
    # "(not installed)" tag on isbounty when it's missing — it sets up the
    # error that follows.
    if verbose >= 1 and not args.no_banner:
        from .banner import print_banner
        print_banner(verbose, show_tip=not args.no_tips, skip_logo=logo_already_printed)

    # Classifier preflight. Skip it in --enumerate-only mode: enumeration
    # does not need isbounty. Everything else does, so fail here with an
    # actionable message instead of deep inside the async workflow.
    if not args.enumerate_only:
        from .classifier import classifier, print_preflight_error
        ok, reason, _ = classifier.preflight()
        if not ok:
            print_preflight_error(reason)
            return 2

    from .ui import ScanUI
    ui = ScanUI(verbose) if 1 <= verbose <= 2 else None
    if ui:
        ui.__enter__()
    # From here on Ctrl-C has something to save.
    _progress.args, _progress.label_filter, _progress.ui = args, label_filter, ui

    store = None
    try:
        if domains is not None:
            src = f"file {args.f}"
        else:
            src = f"seed {args.u}"
            _progress.src = src
            if ui:
                ui.checkpoint(f"seeding from {args.u}, target {args.min_targets}")
            from .competitors.enumerate import Enumerator
            _progress.enumerator = Enumerator(
                args.u, args.min_targets, args.w, args.delay, ui=ui
            )
            domains = _progress.enumerator.run()

        domains = [d for d in domains if d]
        _progress.src, _progress.domains = src, domains
        if not domains:
            return _fail(ui, f"no valid domains from {src}")

        if ui and args.f:
            ui.checkpoint(f"loaded {len(domains)} domain(s) from {src}")

        if args.enumerate_only:
            try:
                with open(args.enumerate_only, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(domains) + "\n")
            except OSError as e:
                return _fail(ui, f"cannot write {args.enumerate_only}: {e.strerror or e}")
        else:
            from .analyzer import run_workflow
            _progress.results = []
            results = await run_workflow(
                domains, args.w, ui=ui, results=_progress.results
            )

            if args.store:
                from .store import Store
                store = Store(args.store)
                run_id = store.record(results, seed=args.u)
                if args.new_only:
                    results = store.filter_new(results, run_id)
    finally:
        if ui:
            ui.__exit__(None, None, None)
        if store:
            store.close()

    # The run is complete; the final save takes milliseconds, so don't let a
    # Ctrl-C cut a file off halfway through it.
    _progress.args = None
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    if args.enumerate_only:
        _say(f"saved {len(domains)} domain(s) to {args.enumerate_only}")
        return 0

    code = _save_results(results, src, args, label_filter)
    print()
    return code


def run():
    import asyncio as aio

    # Check early CLI args to print the logo instantly (sub-millisecond)
    argv = sys.argv[1:]
    wants_help = any(a in ("-h", "--help") for a in argv)
    wants_quiet = any(a in ("-v0", "--no-banner") or a == "0" for a in argv)
    
    logo_already_printed = False
    if not wants_help and not wants_quiet:
        from .banner import print_logo
        print_logo()
        logo_already_printed = True

    # Installed before aio.run so asyncio keeps it instead of its own handler.
    signal.signal(signal.SIGINT, _on_interrupt)
    code = aio.run(main(logo_already_printed=logo_already_printed))
    sys.exit(code or 0)


def _on_interrupt(signum, frame):
    """Ctrl-C: save what the run has collected, then exit now.

    asyncio's own handler only cancels the main task once the event loop runs,
    and enumeration is blocking code, so Ctrl-C did nothing until it finished.
    A clean exit would then still join every in-flight provider/fetch thread
    (up to 15s per request), so after saving we exit without joining them.
    A second Ctrl-C while saving exits immediately.
    """
    signal.signal(signal.SIGINT, lambda *_: os._exit(130))
    try:
        if _progress.ui:
            _progress.ui.abort()
        elif sys.stdout.isatty():
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        mark = f"{palette.neutral('[')}{palette.error('!')}{palette.neutral(']')}"
        print(f"\n  {mark} interrupted, saving progress", file=sys.stderr, flush=True)
        log.warning("interrupted by user (Ctrl-C)")
        _save_progress()
        print(flush=True)
    except Exception as e:  # never let a save error turn Ctrl-C into a traceback
        log.error(f"saving progress failed: {e}")
        print(f"  could not save progress: {e}", file=sys.stderr, flush=True)
    finally:
        logging.shutdown()
        os._exit(130)
