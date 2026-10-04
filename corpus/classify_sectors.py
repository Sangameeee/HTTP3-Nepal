#!/usr/bin/env python3
"""Post-process domains.csv: reclassify "unclassified" rows using
config/sector_keywords.yaml.

Why this runs *after* build_corpus.py rather than being folded into it: the
1,200-domain corpus was already sampled down (stratified, fixed seed) from
3,434 validated candidates before it was clear how much of the
"unclassified" stratum (579/1200, 48%) would turn out to be personal/NGO
noise vs. mis-sectored businesses. Reclassifying *within* the already-drawn
sample keeps that sample a valid simple random draw of each newly-identified
sub-population (each unclassified row had equal probability of selection, so
partitioning it post hoc by true sector does not introduce selection bias
the way re-querying or re-sampling would). Re-running the whole build to
sample based on corrected labels would also have re-spent the DNS-validation
budget for no benefit -- the DNS-liveness result is already correct and
unaffected by sector labels.

Effect on RQ1: rows reclassified into "other_not_in_scope" should be
excluded from the RQ1 sector-adoption denominator (they are confirmed not
one of the six sectors), while the residual "unclassified" rows are neither
included nor silently dropped -- they are reported as their own count, per
the brief's "never silently bucket, report the count" principle (already
applied to ASN classification; applied here to sector classification too).

Outputs (in corpus/):
  domains.csv              -- overwritten in place, sector column updated
  classification_log.csv   -- every reclassification: domain, old_sector,
                               new_sector, matched_keyword
  corpus_summary.md        -- regenerated with updated sector counts
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"


def load_patterns() -> tuple[dict[str, list[str]], list[str]]:
    with open(REPO_ROOT / "config" / "sector_keywords.yaml") as f:
        spec = yaml.safe_load(f)
    return spec["sectors"], spec["other_not_in_scope"]


def classify(hostname: str, sector_patterns: dict[str, list[str]], other_patterns: list[str]) -> tuple[str, str]:
    """Returns (new_sector, matched_keyword). new_sector is 'unclassified'
    with matched_keyword '' if nothing matched."""
    h = hostname.lower()
    for sector, keywords in sector_patterns.items():
        for kw in keywords:
            if kw in h:
                return sector, kw
    for kw in other_patterns:
        if kw in h:
            return "other_not_in_scope", kw
    return "unclassified", ""


def main():
    domains_csv = CORPUS_DIR / "domains.csv"
    with open(domains_csv, newline="") as f:
        rows = list(csv.DictReader(f))

    sector_patterns, other_patterns = load_patterns()

    log_rows = []
    for row in rows:
        if row["sector"] != "unclassified":
            continue
        new_sector, kw = classify(row["domain"], sector_patterns, other_patterns)
        if new_sector != "unclassified":
            log_rows.append({
                "domain": row["domain"],
                "old_sector": "unclassified",
                "new_sector": new_sector,
                "matched_keyword": kw,
            })
            row["sector"] = new_sector

    with open(domains_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "sector", "source", "date_added"])
        w.writeheader()
        for row in sorted(rows, key=lambda r: (r["sector"], r["domain"])):
            w.writerow(row)

    log_csv = CORPUS_DIR / "classification_log.csv"
    with open(log_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "old_sector", "new_sector", "matched_keyword"])
        w.writeheader()
        for row in sorted(log_rows, key=lambda r: (r["new_sector"], r["domain"])):
            w.writerow(row)

    by_sector: dict[str, int] = {}
    for row in rows:
        by_sector[row["sector"]] = by_sector.get(row["sector"], 0) + 1

    reclassified_to_sector = sum(1 for r in log_rows if r["new_sector"] != "other_not_in_scope")
    reclassified_to_other = sum(1 for r in log_rows if r["new_sector"] == "other_not_in_scope")
    still_unclassified = by_sector.get("unclassified", 0)

    print(f"Reclassified {len(log_rows)} of {still_unclassified + len(log_rows)} previously-unclassified rows:")
    print(f"  -> moved into one of the 6 RQ1 sectors: {reclassified_to_sector}")
    print(f"  -> moved into other_not_in_scope:       {reclassified_to_other}")
    print(f"  -> remain unclassified (no keyword hit): {still_unclassified}")
    print()
    print("Sector counts after classification:")
    for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1]):
        print(f"  {k:20s} {v}")

    # Regenerate corpus_summary.md's sector/TLD/source tables against the
    # corrected domains.csv. Rejection/build-log sections are untouched
    # (this script never re-runs DNS validation or CT enumeration).
    regenerate_summary(rows, log_rows)


def regenerate_summary(rows: list[dict], log_rows: list[dict]):
    summary_path = CORPUS_DIR / "corpus_summary.md"
    existing = summary_path.read_text() if summary_path.exists() else ""

    by_sector: dict[str, int] = {}
    by_tld: dict[str, int] = {}
    for row in rows:
        by_sector[row["sector"]] = by_sector.get(row["sector"], 0) + 1
        tld = ".".join(row["domain"].split(".")[-2:]) if row["domain"].count(".") >= 2 and row["domain"].endswith(".np") else row["domain"].rsplit(".", 1)[-1]
        by_tld[tld] = by_tld.get(tld, 0) + 1

    lines = [
        f"\n## Post-processing: sector reclassification ({datetime.now(timezone.utc).isoformat()})\n",
        f"Ran `corpus/classify_sectors.py` against `config/sector_keywords.yaml` "
        f"to split the original \"unclassified\" bucket into confirmed RQ1 sectors, "
        f"`other_not_in_scope` (NGOs/personal sites/clubs -- confirmed not one of "
        f"the 6 sectors), and a smaller honest `unclassified` residual. "
        f"See `corpus/classification_log.csv` for every individual decision.\n",
        f"\nReclassified: **{len(log_rows)}** rows "
        f"({sum(1 for r in log_rows if r['new_sector'] != 'other_not_in_scope')} into an RQ1 sector, "
        f"{sum(1 for r in log_rows if r['new_sector'] == 'other_not_in_scope')} into other_not_in_scope).\n",
        "\n### Sector counts after reclassification\n",
        "| Sector | Count |\n|---|---|\n",
    ]
    for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |\n")

    with open(summary_path, "a") as f:
        f.writelines(lines)


if __name__ == "__main__":
    main()
