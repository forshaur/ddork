<p align="center">
  <img src="img/55625.png" alt="ddork logo" width="350" />
</p>

# ddork

ddork is a security reconnaissance and bug-bounty program discovery tool designed to discover competitor domains and automatically classify their bug-bounty or Vulnerability Disclosure Program (VDP) policies.

---

## Overview

using google dorks is exhausting, not only that using dorks or platforms like bbradar.io lead to programs that are well known and thus increasing competition and so your reports get flagged as duplicate because someone else already reported the vuln. ddork automates the process of mapping out an organization's competitors and analyzing their external security posture. It crawls and queries policy locations—such as `security.txt` and search provider results—before feeding collected policy pages into an automated classifier to determine whether targets offer paid bug bounties (`PAID_BB`), vulnerability disclosure programs (`VDP`), or no program (`NOT_PROGRAM`).

since, you control the seed, you control the results you get and thus it solves the problem of - everyone getting well-known targets.

If your dork query missed a company's bug bounty program to get it indexed on the search engine - there is a chance you may find it using ddork.

## Demo

<p align="center">
  <img src="img/55329.jpg" alt="ddork demo" width="850" style="max-width: 100%; border-radius: 8px;" />
</p>

### Key Capabilities

- **Automated Competitor Expansion:** Discovers related domains from an initial seed domain or URL.
- **Security Policy Extraction:** Checks `security.txt` and search engine results for security and disclosure policies.
- **Smart Program Classification:** Integrates with `isbounty` to categorize policy text with confidence scoring and decision paths.
- **State Persistence:** Tracks analyzed targets in SQLite to allow incremental and new-only delta scans across runs.
- **Saved Program Lists:** Appends program links from every run to `~/.ddork/paid_programs.txt` and `~/.ddork/vdp.txt`, one dated block per run.
- **Flexible Exporting:** Saves structured results into TSV format with label and confidence filtering.

---

## Requirements

- Python >= 3.8
- Git (for installing package dependencies)

---

## Installation

Install `ddork` directly from GitHub using `pip`:

```bash
pip install git+https://github.com/for-shaur/ddork
```

### Optional / Direct Classifier Update

The classification engine relies on `isbounty`. You can install or update the classifier at any time by running:

```bash
ddork --update
```

Or manually:

```bash
pip install --upgrade git+https://github.com/forshaur/isbounty
```

---

## Usage

### 1. Seed Domain Enumeration and Classification

Discover competitors starting from a seed URL or domain, specifying the target number of domains to find:

```bash
ddork -u https://example.com -n 25
```

### 2. Analyze Domains from a File

Analyze a pre-existing list of target domains without running competitor enumeration:

```bash
ddork -f domains.txt
```

### 3. Competitor Enumeration Only

Enumerate competitor domains without performing policy fetching or classification:

```bash
ddork -u https://example.com -n 50 --enumerate-only discovered_targets.txt
```

### 4. Cross-Run State Tracking

Persist scan history into an SQLite database and filter results to newly discovered assets only:

```bash
# Record findings to SQLite database
ddork -u https://example.com -n 30 --store targets.sqlite

# Show only newly discovered assets in subsequent runs
ddork -u https://example.com -n 30 --store targets.sqlite --new-only
```

### 5. Where Results Go

The terminal shows only how many `PAID_BB`, `VDP` and `NOT_PROGRAM` findings a run produced. The program links are appended to text files in `~/.ddork/` (on Windows, `C:\Users\<you>\.ddork\`), with a dated separator before each run's block:

```text
# ===== 2026-10-28 14:05 · seed example.com · 2 link(s) =====
https://acme.com/security
https://hackerone.com/widgetco
```

| File | Contents |
| :--- | :--- |
| `paid_programs.txt` | `PAID_BB` links |
| `vdp.txt` | `VDP` links |
| `not_program.txt` | `NOT_PROGRAM` links (only with `--all`) |
| `scan.log` | Log of the latest run (replaced each run) |

### 6. Export Findings to TSV

Export results to a file with optional label and confidence filtering:

```bash
# Export all findings
ddork -f domains.txt -o findings.tsv

# Export only PAID_BB findings
ddork -f domains.txt -o paid_bounties.tsv PAID_BB
```

---

## Command-Line Options

| Argument | Description |
| :--- | :--- |
| `-u <URL/DOMAIN>` | Seed URL or domain to enumerate competitors from. |
| `-f <FILE>` | Path to a file containing domains to analyze (one per line). |
| `-n, --min-targets <INT>` | Minimum number of targets to find during enumeration (required with `-u`). |
| `-w <INT>` | Worker concurrency level (default: `5`). |
| `--delay <FLOAT>` | Minimum delay in seconds between provider requests (default: `0.1`). |
| `-o <FILE> [LABEL]` | Output findings to TSV file. Optional label filter (`PAID_BB`, `VDP`, `NOT_PROGRAM`). |
| `--min-conf <FLOAT>` | Minimum confidence threshold for file output. |
| `--store <PATH>` | Path to SQLite database for state and run tracking. |
| `--new-only` | Display only findings first seen in the current run (requires `--store`). |
| `--all` | Also save `NOT_PROGRAM` links to `~/.ddork/not_program.txt`. |
| `--enumerate-only [FILE]` | Stop after competitor enumeration and save domains to file. |
| `-v, --verbose <0-3>` | Verbosity level (`0`=quiet, `1`=normal, `2`=verbose, `3`=debug). |
| `--debug` | Alias for `-v 3`. |
| `--update` | Update or install the `isbounty` classifier package and exit. |
| `--no-banner` | Suppress startup banner. |

---

## Output Labels

| Label | Description |
| :--- | :--- |
| `PAID_BB` | Confirmed paid bug bounty program with monetary rewards or platform bounty links. |
| `VDP` | Vulnerability Disclosure Program offering acknowledgment, hall of fame, or safe harbor without monetary bounties. |
| `NOT_PROGRAM` | General security page or domain without an active vulnerability disclosure or bounty policy. |

---

## Credits:
My friend Sarthak unknowingly gave me the idea of this tool when he found out a target from LinkedIn, which was not indexed on google for the dorks we tried despite having all the keywords in its website.

## License

This project is distributed under the terms of the GNU General Public License v3.0. See the [LICENSE](LICENSE) file for details.

