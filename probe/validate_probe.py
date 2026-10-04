#!/usr/bin/env python3
"""Pre-flight validation: run the full probe pipeline (dns/h2/quic/tls/asn)
against a small fixed set of known-good and known-negative HTTP/3 endpoints,
and print the results for inspection.

Required by the brief before any full corpus scan runs. Deliberately a
separate script from scan.py rather than a flag on it: this writes to its
own file (data/raw/validation.jsonl), never touches run resumability state
or the real corpus, and is meant to be read by a human, not analyzed later.

Known-good hosts (expect h3_supported=true): cloudflare-quic.com,
quic.nginx.org -- both named in the brief.
Known-negative hosts (expect h3_supported=false, and for the Nepali .gov.np
ones specifically, quic.failure_reason="udp_no_reply"): example.com (no
QUIC), plus two real corpus government domains already observed not to
answer on UDP/443 during probe development, which doubles as an early look
at the exact failure mode RQ5 cares about.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from probe import schema
from probe.scan import (DATA_RAW_DIR, curl_version, load_settings, probe_one_host,
                          tool_versions)
from probe.quic_probe import install_quiet_exception_handler

VALIDATION_SET = [
    {"domain": "cloudflare-quic.com", "sector": "validation_known_good"},
    {"domain": "quic.nginx.org", "sector": "validation_known_good"},
    {"domain": "example.com", "sector": "validation_known_negative"},
    {"domain": "abpp.gov.np", "sector": "validation_known_negative"},
    {"domain": "admincourt.gov.np", "sector": "validation_known_negative"},
]


async def main():
    cfg = load_settings()
    probe_cfg = cfg["probe"]
    versions = tool_versions()
    versions["curl"] = curl_version(probe_cfg["curl_path"]) or "NOT FOUND"
    print(f"Tool versions: {versions}\n")

    install_quiet_exception_handler(asyncio.get_event_loop())

    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_RAW_DIR / "validation.jsonl"
    out_fh = open(out_path, "w", buffering=1)  # validation output is regenerated each run, not append-only
    write_lock = asyncio.Lock()

    for row in VALIDATION_SET:
        host = row["domain"]
        await probe_one_host(
            host, "apex", row, cfg=cfg, run_id="validation", vantage_point="dev-machine",
            versions=versions, out_fh=out_fh, write_lock=write_lock,
        )

    out_fh.close()

    records = {}
    with open(out_path) as f:
        for line in f:
            rec = json.loads(line)
            records[rec["host"]] = rec

    print(f"{'host':<24} {'expect':<10} {'h3_advertised':<14} {'h3_supported':<13} {'quic_failure_reason':<20} {'vn_versions'}")
    print("-" * 110)
    all_ok = True
    for row in VALIDATION_SET:
        host = row["domain"]
        rec = records.get(host)
        expect_good = row["sector"] == "validation_known_good"
        if rec is None:
            print(f"{host:<24} NO RECORD WRITTEN -- probe crashed silently")
            all_ok = False
            continue
        got_good = bool(rec["h3_supported"])
        status = "OK" if got_good == expect_good else "MISMATCH"
        if status == "MISMATCH":
            all_ok = False
        print(f"{host:<24} {'good' if expect_good else 'negative':<10} "
              f"{str(rec['h3_advertised']):<14} {str(rec['h3_supported']):<13} "
              f"{str(rec['quic']['failure_reason']):<20} {rec['quic']['version_negotiation']['server_versions']}  [{status}]")

    print()
    print("Full records:")
    print(json.dumps(records, indent=2))

    print()
    if all_ok:
        print("VALIDATION PASSED: every host's h3_supported matched its expected known-good/known-negative status.")
    else:
        print("VALIDATION FAILED: see MISMATCH rows above -- do not proceed to a full scan until this is fixed.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
