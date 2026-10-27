"""Run summary, saved program lists, and TSV output.

The terminal shows counts only. Program links are appended to one text file
per label under ~/.ddork, each run starting with a dated separator:

    # ===== 2026-10-28 14:05 · seed example.com · 2 link(s) =====
    https://acme.com/security
    https://hackerone.com/widgetco

Color roles: labels use success/brand/muted, paths are links.
"""
import os
import time

from .colors import palette
from .config import ensure_home

LABEL_ORDER = {"PAID_BB": 0, "VDP": 1, "NOT_PROGRAM": 2}
LABELS = tuple(LABEL_ORDER.keys())

_LABEL_COLOR = {
    "PAID_BB":     "success",
    "VDP":         "brand",
    "NOT_PROGRAM": "muted",
}

# Label -> saved-list filename. NOT_PROGRAM is only written with --all.
PROGRAM_FILES = {
    "PAID_BB":     "paid_programs.txt",
    "VDP":         "vdp.txt",
    "NOT_PROGRAM": "not_program.txt",
}


def _flatten(results):
    rows = []
    for r in results:
        rows.extend(r.get("findings", []))
    return rows


def _sort_key(f):
    return (LABEL_ORDER.get(f["label"], 9), -f["confidence"], f["domain"])


def _links(results):
    """{label: [unique links]}, best first. A finding without a URL uses its domain."""
    links = {label: [] for label in LABELS}
    for f in sorted(_flatten(results), key=_sort_key):
        link = f.get("url") or f["domain"]
        if f["label"] in links and link not in links[f["label"]]:
            links[f["label"]].append(link)
    return links


def print_summary(results):
    """One line of per-label counts, matching what save_programs writes."""
    links = _links(results)
    parts = [
        f"{getattr(palette, _LABEL_COLOR[label])(label)} {len(links[label])}"
        for label in LABELS
    ]
    print()
    print("  " + f"   {palette.muted('·')}   ".join(parts))


def save_programs(results, source, include_not_program=False):
    """Append this run's program links to the per-label files under DDORK_HOME.

    Returns [(label, path, count)] for each file written. Labels with no
    links this run are skipped, so files only gain a separator when they
    also gain links.
    """
    links = _links(results)
    if not include_not_program:
        links.pop("NOT_PROGRAM")

    stamp = time.strftime("%Y-%m-%d %H:%M")
    written = []
    for label, items in links.items():
        if not items:
            continue
        path = os.path.join(ensure_home(), PROGRAM_FILES[label])
        with open(path, "a", encoding="utf-8") as fh:
            if fh.tell():
                fh.write("\n")
            fh.write(f"# ===== {stamp} · {source} · {len(items)} link(s) =====\n")
            fh.write("\n".join(items) + "\n")
        written.append((label, path, len(items)))
    return written


def save_urls(results, path, label_filter=None, min_conf=None):
    """Write findings as TSV: domain\turl\tlabel\tconfidence\tsource\tdecision_path."""
    n = 0
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("domain\turl\tlabel\tconfidence\tsource\tdecision_path\n")
        for f in sorted(_flatten(results), key=_sort_key):
            if label_filter and f["label"] != label_filter:
                continue
            if min_conf is not None and f["confidence"] < min_conf:
                continue
            fh.write(
                f"{f['domain']}\t{f.get('url', '')}\t{f['label']}\t"
                f"{f['confidence']:.2f}\t{f.get('source', '')}\t"
                f"{(f.get('decision_path') or '').replace(chr(9), ' ')}\n"
            )
            n += 1
    return n