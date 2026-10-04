#!/usr/bin/env python3
"""Phase 4a -- handshake timing, H3 (QUIC) vs H2 (TCP+TLS), same origin.

Brief requirements this implements:
  - Only domains confirmed to support BOTH H2 and H3 (same-origin
    comparison, never CDN-H3 vs. origin-H2) -- sourced from Phase 2's own
    output via analysis.load, not re-probed.
  - >=30 trials per domain, alternating protocols, **interleaved within
    each trial block** (H3, H2, H3, H2, ...) so diurnal network variation
    can't correlate with protocol -- a block of N trials always alternates,
    never "all H3 trials, then all H2 trials."
  - Baseline RTT (ICMP ping, `tcping` not available on this machine) recorded
    alongside every trial, not just once per domain -- RTT is a covariate,
    and this vantage point's path to a given IP can shift between trials.
  - Measures handshake time specifically, not full page load (that's 4b,
    perf/pageload_bench.py, which needs Playwright/Chromium and hasn't been
    built yet in this pass).

H2 timing is a raw TCP-connect + TLS-handshake measurement via asyncio's
SSL transport, not an httpx request -- deliberately excludes HTTP
request/response time so this is a clean protocol-handshake comparison, not
a full-transaction one. H3 timing reuses quic_probe.direct_attempt's
handshake_duration_ms, same definition (first packet sent to handshake
complete) for a fair comparison.

Design matrix the brief asks for (protocol x ISP x access-technology x
time-of-day): **this pass only has protocol and time-of-day** (single
vantage point, wired, whatever ISP this machine is on -- see phases.md
Phase 2/3 for the vantage-point limitation already documented). ISP and
access-technology are recorded as constants (vantage_point label), not
varied -- filling those in needs the operator's own additional network
access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW_DIR = REPO_ROOT / "data" / "raw"
sys.path.insert(0, str(REPO_ROOT))

from analysis.load import load_probe_runs  # noqa: E402
from probe.quic_probe import direct_attempt, install_quiet_exception_handler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("handshake_bench")


def load_settings() -> dict:
    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def qualifying_domains() -> list[dict]:
    """Same-origin H2+H3 candidates: **one host per domain** (prefers apex,
    falls back to www if only www qualifies).

    Unlike Phase 2 (rq3_config), which deliberately keeps apex and www as
    separate records because *configuration* frequently differs between
    them, Phase 4 is a performance comparison -- apex and www of the same
    domain are, in the overwhelming majority of cases, the same backend
    infrastructure, so probing both here would double-count a domain's
    "typical" performance as two samples that aren't really independent,
    not add a second real finding. This also brings the qualifying set back
    to ~186 domains, matching the brief's own stated expectation of
    "50-150 qualifying domains" -- the first version of this function
    returned 354 (apex+www treated separately), which was double what the
    brief anticipated and, in an earlier run of this script, the actual
    source of a scope/runtime estimate that turned out wrong. See
    phases.md Phase 4 for the full account.
    """
    df = load_probe_runs()
    q = df[(df["h2_success"] == True) & (df["h3_supported"] == True)]
    q = q.sort_values("host_type", ascending=True)  # "apex" < "www" alphabetically -- apex sorts first
    deduped = q.drop_duplicates(subset="domain", keep="first")
    return deduped[["domain", "host", "host_type"]].to_dict("records")


_PING_RE = re.compile(r"time=([\d.]+)\s*ms")


def ping_rtt_ms(host_or_ip: str, timeout_s: float = 2.0) -> float | None:
    try:
        out = subprocess.run(["ping", "-c", "1", "-t", str(int(timeout_s)), host_or_ip],
                              capture_output=True, text=True, timeout=timeout_s + 1)
        m = _PING_RE.search(out.stdout)
        return float(m.group(1)) if m else None
    except Exception:
        return None


async def h2_handshake_trial(host: str, ip: str, timeout_s: float) -> dict:
    """Raw TCP-connect + TLS-handshake timing, no HTTP semantics."""
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    result = {"success": False, "handshake_duration_ms": None, "failure_reason": None, "alpn_negotiated": None}
    start = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            transport, protocol = await asyncio.get_event_loop().create_connection(
                asyncio.Protocol, host=ip, port=443, ssl=ctx, server_hostname=host,
            )
        result["handshake_duration_ms"] = (time.monotonic() - start) * 1000
        result["success"] = True
        ssl_obj = transport.get_extra_info("ssl_object")
        result["alpn_negotiated"] = ssl_obj.selected_alpn_protocol() if ssl_obj else None
        transport.close()
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "timeout" in msg or isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            result["failure_reason"] = "timeout"
        elif "refused" in msg:
            result["failure_reason"] = "connection_refused"
        else:
            result["failure_reason"] = f"error:{type(e).__name__}"
    return result


async def h3_handshake_trial(host: str, ip: str, timeout_s: float) -> dict:
    quic_result, _, _ = await direct_attempt(ip, 443, host, timeout_s)
    return {
        "success": quic_result["direct_attempt_success"],
        "handshake_duration_ms": quic_result["handshake_duration_ms"],
        "failure_reason": quic_result["failure_reason"],
    }


async def run_domain_trials(host: str, ip: str, n_trials: int, timeout_s: float, vantage_point: str) -> list[dict]:
    """n_trials total, alternating H3/H2/H3/H2..., not n_trials of each."""
    records = []
    for i in range(n_trials):
        protocol = "h3" if i % 2 == 0 else "h2"
        rtt = ping_rtt_ms(ip)
        if protocol == "h3":
            trial = await h3_handshake_trial(host, ip, timeout_s)
        else:
            trial = await h2_handshake_trial(host, ip, timeout_s)
        records.append({
            "host": host, "ip": ip, "trial_index": i, "protocol": protocol,
            "vantage_point": vantage_point, "baseline_rtt_ms": rtt,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **trial,
        })
    return records


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--vantage-point", required=True)
    ap.add_argument("--trials", type=int, default=30, help="total trials per host (split across protocols, not per-protocol)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N qualifying hosts (smoke testing)")
    args = ap.parse_args()

    cfg = load_settings()
    timeout_s = cfg["probe"]["per_domain_timeout_s"]
    install_quiet_exception_handler(asyncio.get_event_loop())

    hosts = qualifying_domains()
    if args.limit:
        hosts = hosts[: args.limit]
    log.info("Handshake benchmark: %d qualifying hosts, %d trials each (%d total connections)",
              len(hosts), args.trials, len(hosts) * args.trials)

    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_RAW_DIR / f"handshake_bench_{args.run_id}.jsonl"

    with open(out_path, "a", buffering=1) as f:
        for i, row in enumerate(hosts):
            host = row["host"]
            try:
                ip = socket.getaddrinfo(host, 443, socket.AF_INET)[0][4][0]
            except Exception as e:
                log.warning("[%d/%d] %s: DNS failed (%s), skipping", i + 1, len(hosts), host, e)
                continue
            log.info("[%d/%d] %s (%s)", i + 1, len(hosts), host, ip)
            records = await run_domain_trials(host, ip, args.trials, timeout_s, args.vantage_point)
            for rec in records:
                rec["run_id"] = args.run_id
                rec["domain"] = row["domain"]
                rec["host_type"] = row["host_type"]
                f.write(json.dumps(rec) + "\n")
                f.flush()

    log.info("=== Handshake benchmark complete: output %s ===", out_path)


if __name__ == "__main__":
    asyncio.run(main())
