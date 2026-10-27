"""Helpful usage tips for ddork users."""
import random

from .colors import palette

TIPS = [
    (
        "Encountered a false positive or missed a policy? Report it to help improve accuracy:\n"
        f"       {palette.link('https://github.com/for-shaur/ddork/issues')}"
    ),
    (
        "Export confirmed paid bounty programs directly to TSV using "
        f"{palette.bold('-o bounties.tsv PAID_BB')}."
    ),
    (
        "Need to discover competitor domains without policy classification? Use "
        f"{palette.bold('--enumerate-only targets.txt')}."
    ),
    (
        "Domains without policy signals (NOT_PROGRAM) are not saved by default. Use "
        f"{palette.bold('--all')} to also save them to ~/.ddork/not_program.txt."
    ),
    (
        "Program links from every run are appended to "
        f"{palette.bold('~/.ddork/paid_programs.txt')} and {palette.bold('~/.ddork/vdp.txt')}, "
        "one dated block per run."
    ),
]


def get_random_tip():
    """Return a randomly selected formatted tip string."""
    return random.choice(TIPS)


def print_tip():
    """Print a random tip formatted nicely with palette colors."""
    tip = get_random_tip()
    print()
    print(f"  {palette.bold('tip:')} {tip}")
