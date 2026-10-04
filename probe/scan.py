#!/usr/bin/env python3
"""Phase 2 orchestrator: runs dns_probe / h2_probe / quic_probe / tls_probe /
asn_probe over every host in the corpus and appends one JSON record per host
to `data/raw/<run_id>.jsonl`.

Timeout model, stated explicitly because the brief's "per-domain timeout
10 s" is ambiguous once a domain fans out into 6+ distinct network
operations (DNS, H2, QUIC direct/VN/0-RTT, ASN x{1,2 IPs}): **10 s is
applied per individual network operation**, not as a single deadline for
the whole per-host pipeline. A host where everything times out can
therefore take a low multiple of 10 s end to end, not 10 s flat -- over
Nepali links with occasional real packet loss (the brief's own stated
environment), a single 10 s ceiling for DNS+H2+3xQUIC+ASN combined would
misclassify slow-but-working origins as failures. Concurrency (20-50
hosts at once) is what keeps total wall-clock reasonable, not a tighter
per-op timeout.

Resumability: before probing, reads any existing `data/raw/<run_id>.jsonl`
and skips hosts already present in it. `data/raw/` is append-only (brief
Correctness requirement 6) -- this script only ever appends, never rewrites
a line, which is also why cross-validation sampling (see below) is decided
per-host *before* probing rather than as a second pass over already-written
records.

Apex vs www: every corpus domain becomes two hosts (`example.com` and
`www.example.com`), each an independent record -- see README's "apex vs
www" note for why this isn't collapsed at the corpus stage.

Scope: by default, only hosts whose corpus sector is one of the six RQ1
sectors are probed (see phases.md Phase 1 "decision" section) -- pass
--include-unclassified to widen this.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import logging
import platform
import random
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import asn_probe, dns_probe, h2_probe, quic_probe, schema
from .quic_probe import install_quiet_exception_handler

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW_DIR = REPO_ROOT / "data" / "raw"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("scan")

EXCLUDED_SECTORS_DEFAULT = {"unclassified", "other_not_in_scope"}


def load_settings() -> dict:
    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


def load_corpus(include_unclassified: bool) -> list[dict]:
    import csv
    rows = []
    with open(REPO_ROOT / "corpus" / "domains.csv", newline="") as f:
        for row in csv.DictReader(f):
            if not include_unclassified and row["sector"] in EXCLUDED_SECTORS_DEFAULT:
                continue
            rows.append(row)
    return rows


def expand_hosts(domain_rows: list[dict]) -> list[tuple[str, str, dict]]:
    """(host, host_type, domain_row) for apex + www of every domain."""
    out = []
    for row in domain_rows:
        out.append((row["domain"], "apex", row))
        out.append((f"www.{row['domain']}", "www", row))
    return out


def tool_versions() -> dict[str, str]:
    versions = {
        "python": platform.python_version(),
        "aioquic": importlib.metadata.version("aioquic"),
        "httpx": importlib.metadata.version("httpx"),
        "dnspython": importlib.metadata.version("dnspython"),
    }
    return versions


def curl_version(curl_path: str) -> str | None:
    try:
        out = subprocess.run([curl_path, "-V"], capture_output=True, text=True, timeout=5)
        return out.stdout.splitlines()[0] if out.returncode == 0 else None
    except Exception:
        return None


async def curl_http3_check(curl_path: str, host: str, timeout_s: float, user_agent: str) -> bool | None:
    """Runs Homebrew curl --http3 against the host, returns True if it
    completed over HTTP/3, False if it completed over something else or
    failed, None if curl itself couldn't be run."""
    try:
        proc = await asyncio.create_subprocess_exec(
            curl_path, "--http3", "-sS", "-o", "/dev/null", "-A", user_agent,
            "--max-time", str(timeout_s), "-w", "%{http_version}",
            f"https://{host}/",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 2)
        if proc.returncode != 0:
            return False
        return stdout.decode().strip() == "3"
    except Exception:
        return None


def already_done_hosts(run_id: str) -> set[str]:
    path = DATA_RAW_DIR / f"{run_id}.jsonl"
    done = set()
    if path.exists():
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    done.add(rec["host"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return done


def in_cross_validation_sample(host: str, seed: int, fraction: float) -> bool:
    """Deterministic per-host decision, made before probing (not as a
    second pass over written records) so data/raw/ stays append-only."""
    return random.Random(f"{seed}:{host}").random() < fraction


async def probe_one_host(
    host: str, host_type: str, domain_row: dict, *, cfg: dict, run_id: str, vantage_point: str,
    versions: dict, out_fh, write_lock: asyncio.Lock,
) -> None:
    probe_cfg = cfg["probe"]
    timeout_s = probe_cfg["per_domain_timeout_s"]
    user_agent = cfg["study"]["user_agent"]

    rec = schema.new_record(
        domain=domain_row["domain"], host=host, host_type=host_type, sector=domain_row["sector"],
        run_id=run_id, vantage_point=vantage_point, tool_versions=versions,
    )
    t0 = time.monotonic()
    durations = rec["probe_durations_ms"]

    # --- DNS ---
    t = time.monotonic()
    rec["dns"] = await dns_probe.probe_dns(host, probe_cfg.get("dns_timeout_s", timeout_s))
    durations["dns"] = (time.monotonic() - t) * 1000

    if rec["dns"]["failure_reason"] is not None:
        rec["failure_reason"] = rec["dns"]["failure_reason"]
        # h2/quic/tls are never attempted on a DNS failure -- mark them
        # explicitly rather than leaving failure_reason at the schema
        # default of None, which would otherwise be indistinguishable from
        # "attempted and succeeded" to anyone aggregating this field later.
        rec["h2"]["failure_reason"] = "not_attempted"
        rec["quic"]["failure_reason"] = "not_attempted"
        rec["quic"]["version_negotiation"]["failure_reason"] = "not_attempted"
        rec["h3_advertised"] = False
        rec["h3_supported"] = False
        durations["total"] = (time.monotonic() - t0) * 1000
        await _write(out_fh, write_lock, rec)
        return

    rec["ips"] = list(dict.fromkeys(rec["dns"]["a"] + rec["dns"]["aaaa"]))

    # --- H2 baseline ---
    t = time.monotonic()
    rec["h2"] = await h2_probe.probe_h2(host, user_agent, timeout_s)
    durations["h2"] = (time.monotonic() - t) * 1000

    # --- QUIC (direct attempt + version negotiation + 0-RTT) and TLS ---
    ip_for_quic = next((ip for ip in rec["dns"]["a"]), None) or next((ip for ip in rec["dns"]["aaaa"]), None)
    if ip_for_quic is not None:
        t = time.monotonic()
        rec["quic"], rec["tls"] = await quic_probe.probe_quic(ip_for_quic, 443, host, timeout_s, user_agent)
        durations["quic"] = (time.monotonic() - t) * 1000
    else:
        rec["quic"]["failure_reason"] = "not_attempted"

    # ECH: aioquic can't test it directly (see tls_probe.py docstring) --
    # use the DNS HTTPS-RR's published ech param as a documented proxy.
    rec["tls"]["ech_supported"] = any(r["ech_present"] for r in rec["dns"]["https_rr"]["records"]) or None
    if rec["dns"]["https_rr"]["present"] and not rec["tls"]["ech_supported"]:
        rec["tls"]["ech_supported"] = False

    # --- Derived: advertised vs supported (brief Phase 2 item 3) ---
    https_rr_advertises_h3 = any("h3" in r.get("alpn", []) for r in rec["dns"]["https_rr"]["records"])
    alt_svc_advertises_h3 = any(e["protocol"].startswith("h3") for e in rec["h2"]["alt_svc_parsed"])
    rec["h3_advertised"] = bool(https_rr_advertises_h3 or alt_svc_advertises_h3)
    rec["h3_supported"] = bool(rec["quic"]["direct_attempt_success"])

    # --- ASN / ownership ---
    if rec["ips"]:
        t = time.monotonic()
        asns, overall_class = await asn_probe.probe_asns(rec["ips"], probe_cfg.get("dns_timeout_s", timeout_s))
        rec["asns"] = asns
        rec["ownership_class"] = overall_class
        durations["asn"] = (time.monotonic() - t) * 1000

    # --- Cross-validation (10% sample, decided deterministically) ---
    if in_cross_validation_sample(host, cfg["study"]["random_seed"], probe_cfg["cross_validation_fraction"]):
        rec["cross_validation"]["sampled"] = True
        rec["cross_validation"]["curl_http3_attempted"] = True
        curl_result = await curl_http3_check(probe_cfg["curl_path"], host, timeout_s, user_agent)
        rec["cross_validation"]["curl_http3_success"] = curl_result
        if curl_result is not None:
            rec["cross_validation"]["agrees_with_primary"] = (curl_result == rec["h3_supported"])
            if not rec["cross_validation"]["agrees_with_primary"]:
                log.warning("cross-validation disagreement on %s: aioquic h3_supported=%s, curl --http3=%s",
                            host, rec["h3_supported"], curl_result)

    durations["total"] = (time.monotonic() - t0) * 1000
    await _write(out_fh, write_lock, rec)


async def _write(out_fh, lock: asyncio.Lock, rec: dict) -> None:
    line = json.dumps(rec, sort_keys=False) + "\n"
    async with lock:
        out_fh.write(line)
        out_fh.flush()


async def run_scan(args, cfg: dict) -> None:
    probe_cfg = cfg["probe"]
    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)

    domain_rows = load_corpus(args.include_unclassified)
    hosts = expand_hosts(domain_rows)
    log.info("Corpus: %d domains -> %d hosts (apex+www)%s", len(domain_rows), len(hosts),
              " [including unclassified/other_not_in_scope]" if args.include_unclassified else "")

    if args.limit:
        hosts = hosts[: args.limit]
        log.info("--limit applied: probing only the first %d hosts", len(hosts))

    skip = already_done_hosts(args.run_id)
    if skip:
        log.info("Resuming run %s: %d hosts already recorded, skipping them", args.run_id, len(skip))
    todo = [h for h in hosts if h[0] not in skip]
    log.info("Hosts to probe this invocation: %d", len(todo))

    if not todo:
        log.info("Nothing to do.")
        return

    versions = tool_versions()
    versions["curl"] = curl_version(probe_cfg["curl_path"]) or "NOT FOUND -- cross-validation will be skipped"
    log.info("Tool versions: %s", versions)

    install_quiet_exception_handler(asyncio.get_event_loop())

    out_path = DATA_RAW_DIR / f"{args.run_id}.jsonl"
    out_fh = open(out_path, "a", buffering=1)
    write_lock = asyncio.Lock()
    sem = asyncio.Semaphore(probe_cfg["concurrency"])
    delay = probe_cfg["inter_request_delay_s"]

    completed = 0

    async def _bounded(host, host_type, domain_row):
        nonlocal completed
        async with sem:
            try:
                await probe_one_host(
                    host, host_type, domain_row, cfg=cfg, run_id=args.run_id,
                    vantage_point=args.vantage_point, versions=versions,
                    out_fh=out_fh, write_lock=write_lock,
                )
            except Exception:
                log.exception("Unhandled error probing %s -- recording as failure and continuing", host)
                rec = schema.new_record(
                    domain=domain_row["domain"], host=host, host_type=host_type, sector=domain_row["sector"],
                    run_id=args.run_id, vantage_point=args.vantage_point, tool_versions=versions,
                )
                rec["failure_reason"] = "error:unhandled_exception"
                await _write(out_fh, write_lock, rec)
            finally:
                completed += 1
                if completed % 25 == 0:
                    log.info("  progress: %d/%d", completed, len(todo))
            await asyncio.sleep(delay)

    try:
        await asyncio.gather(*(_bounded(h, t, r) for h, t, r in todo))
    finally:
        out_fh.close()

    log.info("=== Scan complete: %d hosts probed this invocation, output: %s ===", completed, out_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True, help="e.g. run-2026-08-06. Determines data/raw/<run-id>.jsonl")
    ap.add_argument("--vantage-point", required=True, help="must match an id in config/settings.yaml vantage_points")
    ap.add_argument("--include-unclassified", action="store_true",
                     help="also probe corpus rows with sector unclassified/other_not_in_scope (widens scope beyond the default 659-domain confirmed-sector subset)")
    ap.add_argument("--limit", type=int, default=None, help="probe only the first N hosts (smoke testing)")
    args = ap.parse_args()

    cfg = load_settings()
    vp_ids = {vp["id"] for vp in cfg["vantage_points"]}
    if args.vantage_point not in vp_ids:
        log.warning("vantage_point %r is not in config/settings.yaml's vantage_points list (%s) -- recording it anyway",
                    args.vantage_point, sorted(vp_ids))

    asyncio.run(run_scan(args, cfg))


if __name__ == "__main__":
    sys.exit(main())
