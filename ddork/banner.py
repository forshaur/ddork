"""Startup banner: logo, version lines, update hint.

Every bracket and paren is drawn in the `neutral` role (bright white); only
the content between them takes a semantic color:

    (latest)              success — green
    (outdated → x.y.z)    warn    — yellow
    (not installed)       error   — red
    <empty>               no tag when the remote check could not be reached
"""
import subprocess
import sys

from .colors import palette
from .config import (
    CLASSIFIER_GIT_URL,
    CLASSIFIER_PACKAGE,
    DDORK_GIT_URL,
    DDORK_PACKAGE,
    LOG_FILE,
    display_path,
    log,
)
from .version import check_updates, is_outdated

LOGO = r"""
   ┓ ┓    ┓ 
  ┏┫┏┫┏┓┏┓┃┏
  ┗┻┗┻┗┛┛ ┛┗
"""


def _parens(inner):
    """`(inner)` with white parens and the caller-provided colored inner."""
    return f"{palette.neutral('(')}{inner}{palette.neutral(')')}"


def _tag(installed, latest, error):
    """Return a colored suffix for one version line."""
    if error:
        return _parens(palette.error("not installed"))
    if not installed or not latest:
        return ""   # no source of truth; don't claim anything
    if is_outdated(installed, latest):
        return _parens(palette.warn(f"outdated → {latest}"))
    return _parens(palette.success("latest"))


def print_logo():
    """Print just the ASCII logo immediately, without any network or version checking."""
    print(palette.brand(LOGO.rstrip("\n")), flush=True)
    print(flush=True)


def print_banner(verbose=1, show_tip=True, skip_logo=False):
    """Print the logo, version lines, and tip. Silenced by -v 0."""
    if verbose < 1:
        return

    # Print logo immediately so the user doesn't face a blank frozen terminal
    if not skip_logo:
        print_logo()

    # Give immediate feedback that ddork is active and checking for updates
    is_tty = False
    try:
        is_tty = bool(sys.stdout.isatty())
    except Exception:
        pass

    if is_tty:
        status_msg = f"  {palette.muted('checking for updates...')}"
        sys.stdout.write(status_msg)
        sys.stdout.flush()

    from . import __version__ as ddork_ver
    d_info, c_info = check_updates()

    if is_tty:
        # Clear status message line cleanly on interactive terminals
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    d_tag = _tag(ddork_ver, d_info.get("latest"), d_info.get("error"))
    d_line = f"  {palette.bold('ddork')}    {palette.success(ddork_ver)}"
    if d_tag:
        d_line += f" {d_tag}"
    print(d_line)

    c_tag = _tag(c_info.get("version"), c_info.get("latest"), c_info.get("error"))
    c_ver = (palette.success(c_info["version"])
             if c_info.get("version") else palette.muted("?"))
    c_line = f"  {palette.bold('isbounty')} {c_ver}"
    if c_tag:
        c_line += f" {c_tag}"
    print(c_line)

    if show_tip:
        from .tips import print_tip
        print_tip()

    if d_info.get("outdated") or c_info.get("outdated"):
        print()
        print(palette.muted("  run with --update to refresh ddork and isbounty"))

    print(flush=True)


def _run_pip_upgrade(pkg_name, target_spec):
    """Run pip install --upgrade for a target with interactive console feedback."""
    prefix = f"  {palette.neutral('[')}{palette.brand('*')}{palette.neutral(']')}"
    print(f"{prefix} Updating {palette.bold(pkg_name)} ({target_spec})...", flush=True)
    log.info(f"[UPD] upgrading {pkg_name} via {target_spec}")

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", target_spec],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        err_prefix = f"  {palette.neutral('[')}{palette.error('!')}{palette.neutral(']')}"
        print(f"{err_prefix} {palette.error(f'Failed to update {pkg_name}')}: pip install timed out after 180s", flush=True)
        log.error(f"[UPD] pip install {pkg_name} timed out after 180s")
        return False
    except Exception as e:
        err_prefix = f"  {palette.neutral('[')}{palette.error('!')}{palette.neutral(']')}"
        print(f"{err_prefix} {palette.error(f'Failed to update {pkg_name}')}: {e}", flush=True)
        log.error(f"[UPD] pip install {pkg_name} failed: {e}")
        return False

    if result.returncode != 0:
        err_prefix = f"  {palette.neutral('[')}{palette.error('!')}{palette.neutral(']')}"
        print(f"{err_prefix} {palette.error(f'Failed to update {pkg_name}')} (exit code {result.returncode})", flush=True)
        tail = (result.stderr or result.stdout or "").strip()
        if tail:
            tail_lines = tail.splitlines()[-10:]
            for line in tail_lines:
                print(f"      {palette.muted(line)}", flush=True)
        log.error(f"[UPD] pip install {pkg_name} failed:\n{tail}")
        return False

    ok_prefix = f"  {palette.neutral('[')}{palette.success('+')}{palette.neutral(']')}"
    print(f"{ok_prefix} {palette.success(f'{pkg_name} updated successfully')}", flush=True)
    log.info(f"[UPD] {pkg_name} updated successfully")
    return True


def update_packages():
    """Update ddork and the isbounty classifier via pip."""
    targets = [
        ("ddork", f"git+{DDORK_GIT_URL}" if DDORK_GIT_URL else DDORK_PACKAGE),
        ("isbounty", f"git+{CLASSIFIER_GIT_URL}" if CLASSIFIER_GIT_URL else CLASSIFIER_PACKAGE),
    ]

    print(f"\n{palette.bold('Updating ddork and isbounty classifier...')}\n", flush=True)

    success_all = True
    for name, target in targets:
        if not _run_pip_upgrade(name, target):
            success_all = False

    print()
    if success_all:
        print(f"  {palette.neutral('[')}{palette.success('+')}{palette.neutral(']')} {palette.bold('All packages are up to date.')}\n", flush=True)
    else:
        print(f"  {palette.neutral('[')}{palette.error('!')}{palette.neutral(']')} {palette.bold(f'One or more updates failed. Check logs in {display_path(LOG_FILE)}.')}\n", flush=True)

    return success_all


# Backwards compatibility alias
update_classifier = update_packages