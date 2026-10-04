"""JSONL -> tidy pandas DataFrame loaders, shared by every rq*_*.py script.

Four record shapes exist in data/raw/, loaded by four different functions:
  - probe/scan.py output (one row per host: domain x {apex,www}) -> load_probe_runs()
  - viability/udp_path_test.py output (one row per reference server) -> load_viability_runs()
  - perf/handshake_bench.py output (one row per domain x trial) -> load_handshake_bench_runs()
  - perf/pageload_bench.py output (one row per domain x trial) -> load_pageload_bench_runs()

Both loaders flatten nested fields into columns rather than leaving them as
dicts, so downstream rq*_*.py scripts can use plain pandas filtering instead
of re-parsing JSON structure each time.

`ownership_class` is deliberately RE-DERIVED here from each row's raw ASN
numbers against the *current* config/asn_classification.yaml, not read
from the stored field -- config/asn_classification.yaml has already been
corrected and extended after data collection once (see phases.md Phase 2
and its post-Phase-3 addendum), and will likely be again. Trusting the
stored value would silently ignore every such improvement for data already
collected. This is the one non-obvious design decision in this module;
everything else is a straightforward flatten.

Filenames matching "*-pilot-buggy-*" are skipped automatically -- these are
archived runs from bugs caught during development (see phases.md), kept for
the record but never meant to feed analysis.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW_DIR = REPO_ROOT / "data" / "raw"

# Corpus rows with these sectors are excluded from RQ1-RQ5 analysis by
# default, matching probe/scan.py's own default scope -- see phases.md
# Phase 1's "Decision" section for why.
EXCLUDED_SECTORS = {"unclassified", "other_not_in_scope"}


def _rederive_ownership_class(asns: list[dict]) -> str:
    from probe.asn_probe import classify_asn

    classes = set()
    for a in asns:
        cls = classify_asn(a.get("asn"), a.get("as_name"))
        if cls != "unknown":
            classes.add(cls)
    if not classes:
        return "unknown"
    if len(classes) == 1:
        return next(iter(classes))
    return "mixed"


# The run every rq*.py script and figures.py regenerates its default
# (no --run-id override) output from. Pinned explicitly to a specific run
# rather than "whichever is newest" or "all of them concatenated" --
# results.md's prose was written against this run's exact numbers (e.g.
# "Education 53.5%", "Nepali hosting 10.9%"), and those numbers should
# only change when someone deliberately updates both the code and the
# prose together, not silently because a later run's file appeared in
# data/raw/. Update this constant (and results.md's cited numbers in the
# same commit) once a real cross-run union/aggregate is computed after
# all 3 required runs exist -- see phases.md Phase 2's second-run section
# for why a naive multi-file concat is NOT that union (it silently
# doubles every host's row instead of computing a real per-field
# OR/aggregate across runs).
CANONICAL_PROBE_RUN_ID = "run-2026-08-05b"


def _probe_run_files(run_ids: list[str] | None) -> list[Path]:
    if run_ids:
        return [DATA_RAW_DIR / f"{rid}.jsonl" for rid in run_ids]
    # Default (no run_ids given): ONLY the canonical run, not every
    # run-*.jsonl concatenated. With only one run on disk these were
    # equivalent, which is how a latent bug here stayed unnoticed through
    # the first run -- but the moment a second run (run-2026-08-07)
    # landed, "all files" started silently double-counting every host
    # (2,636 rows for 1,318 unique hosts) into every rq*.py script's
    # default-args table. Caught because output/tables/rq1_adoption.csv's
    # `total` column jumped from 250 to 500 for Government after a
    # routine re-run -- see phases.md.
    return [DATA_RAW_DIR / f"{CANONICAL_PROBE_RUN_ID}.jsonl"]


def load_probe_runs(run_ids: list[str] | None = None, exclude_out_of_scope: bool = True) -> pd.DataFrame:
    """One row per (run_id, host). Pass run_ids explicitly to pin analysis
    to specific runs, or to combine multiple runs for your own union/
    variance computation; omitted, loads only the single most recent
    clean run (see _probe_run_files for why "all files" is not the
    default)."""
    rows = []
    for path in _probe_run_files(run_ids):
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                rows.append(_flatten_probe_record(rec))

    df = pd.DataFrame(rows)
    if not df.empty:
        # A handful of hosts fail at the DNS stage before h3_advertised/
        # h3_supported are ever computed (schema default: None/null). A few
        # early records (data/raw/run-2026-08-05b.jsonl specifically, before
        # probe/scan.py was fixed to set these explicitly on a DNS failure --
        # see phases.md Phase 2) still have raw `null` here. Treat as False
        # rather than NaN so every rq*_*.py script can do plain boolean
        # indexing (`df[df["h3_supported"]]`) without every script needing
        # its own NaN-handling boilerplate.
        for col in ("h3_advertised", "h3_supported", "https_rr_present", "https_rr_alpn_h3", "alt_svc_h3"):
            df[col] = df[col].fillna(False).astype(bool)
    if exclude_out_of_scope and not df.empty:
        df = df[~df["sector"].isin(EXCLUDED_SECTORS)].reset_index(drop=True)
    return df


def _flatten_probe_record(rec: dict) -> dict:
    quic = rec.get("quic", {})
    vn = quic.get("version_negotiation", {})
    zrtt = quic.get("zero_rtt", {})
    tls = rec.get("tls", {})
    h2 = rec.get("h2", {})
    dns = rec.get("dns", {})
    https_rr = dns.get("https_rr", {})
    xval = rec.get("cross_validation", {})

    return {
        "run_id": rec.get("run_id"),
        "domain": rec.get("domain"),
        "host": rec.get("host"),
        "host_type": rec.get("host_type"),
        "sector": rec.get("sector"),
        "vantage_point": rec.get("vantage_point"),
        "timestamp_utc": rec.get("timestamp_utc"),
        "failure_reason": rec.get("failure_reason"),

        "dns_a_count": len(dns.get("a", [])),
        "dns_aaaa_count": len(dns.get("aaaa", [])),
        "https_rr_present": https_rr.get("present", False),
        "https_rr_alpn_h3": any("h3" in r.get("alpn", []) for r in https_rr.get("records", [])),

        "h2_success": h2.get("success"),
        "h2_http_version": h2.get("http_version"),
        "alt_svc_h3": any(e.get("protocol", "").startswith("h3") for e in h2.get("alt_svc_parsed", [])),

        "h3_advertised": rec.get("h3_advertised"),
        "h3_supported": rec.get("h3_supported"),

        "quic_failure_reason": quic.get("failure_reason"),
        "quic_handshake_ms": quic.get("handshake_duration_ms"),
        "quic_negotiated_version": quic.get("negotiated_version"),
        "vn_server_versions": tuple(vn.get("server_versions", [])),
        "vn_failure_reason": vn.get("failure_reason"),
        "zero_rtt_attempted": zrtt.get("attempted"),
        "zero_rtt_accepted": zrtt.get("accepted"),

        "tls_cipher_suite": tls.get("cipher_suite"),
        "tls_key_exchange_group": tls.get("key_exchange_group"),
        "tls_cert_issuer": tls.get("cert_issuer"),
        "tls_ech_supported": tls.get("ech_supported"),

        "ips": tuple(rec.get("ips", [])),
        "asns": rec.get("asns", []),  # kept as list-of-dicts; ownership derived below
        "ownership_class": _rederive_ownership_class(rec.get("asns", [])),

        "cross_validation_sampled": xval.get("sampled", False),
        "cross_validation_agrees": xval.get("agrees_with_primary"),

        "total_duration_ms": (rec.get("probe_durations_ms") or {}).get("total"),
    }


def load_viability_runs(run_ids: list[str] | None = None) -> pd.DataFrame:
    """One row per (run_id, reference_server, idle_interval) -- idle_survival
    is exploded so each interval is its own row; handshake/mtu/throughput
    fields are repeated across those rows for convenience (small dataset,
    this redundancy costs nothing and keeps downstream code simple)."""
    if run_ids:
        paths = [DATA_RAW_DIR / f"viability_{rid}.jsonl" for rid in run_ids]
    else:
        paths = sorted(
            p for p in DATA_RAW_DIR.glob("viability_*.jsonl")
            if "pilot-buggy" not in p.name
        )

    rows = []
    for path in paths:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if "dns_failure" in rec:
                    continue
                h = rec.get("handshake", {})
                tq = rec.get("throughput", {}).get("quic", {})
                tt = rec.get("throughput", {}).get("tcp_h2", {})
                mtu_fail_sizes = [m["target_datagram_size"] for m in rec.get("mtu", []) if not m["success"]]
                for interval_rec in rec.get("idle_survival", []):
                    rows.append({
                        "run_id": path.stem.replace("viability_", ""),
                        "host": rec["host"],
                        "operator": rec.get("operator"),
                        "vantage_point": rec.get("vantage_point"),
                        "handshake_success": h.get("success"),
                        "handshake_ms": h.get("handshake_duration_ms"),
                        "mtu_fail_sizes": tuple(mtu_fail_sizes),
                        "mtu_all_ok": len(mtu_fail_sizes) == 0,
                        "idle_s": interval_rec["idle_s"],
                        "resumed_ok": interval_rec["resumed_ok"],
                        "idle_failure_reason": interval_rec.get("failure_reason"),
                        "throughput_quic_bps": tq.get("bytes_per_s"),
                        "throughput_tcp_h2_bps": tt.get("bytes_per_s"),
                    })

    return pd.DataFrame(rows)


def load_handshake_bench_runs(run_ids: list[str] | None = None) -> pd.DataFrame:
    """One row per (run_id, host, trial_index) from perf/handshake_bench.py.
    Already flat -- almost no nesting to unpack -- so this is closer to a
    thin JSONL-concat than the other two loaders, minus archived
    "*-partial-wrong-scope*" runs (see phases.md Phase 4a)."""
    if run_ids:
        paths = [DATA_RAW_DIR / f"handshake_bench_{rid}.jsonl" for rid in run_ids]
    else:
        paths = sorted(
            p for p in DATA_RAW_DIR.glob("handshake_bench_*.jsonl")
            if "partial-wrong-scope" not in p.name and "pilot-buggy" not in p.name
        )

    rows = []
    for path in paths:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))

    return pd.DataFrame(rows)


def load_pageload_bench_runs(run_ids: list[str] | None = None) -> pd.DataFrame:
    """One row per (run_id, host, trial_index) from perf/pageload_bench.py.
    Flattens `timing` (Navigation/Paint Timing fields) and `netlog_check`
    (the mandatory protocol-verification verdict) into top-level columns
    -- every downstream script needs `verified` to filter to trustworthy
    rows, so it shouldn't have to reach into a nested dict to get it."""
    if run_ids:
        paths = [DATA_RAW_DIR / f"pageload_bench_{rid}.jsonl" for rid in run_ids]
    else:
        paths = sorted(DATA_RAW_DIR.glob("pageload_bench_*.jsonl"))

    rows = []
    for path in paths:
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                timing = rec.get("timing") or {}
                netlog_check = rec.get("netlog_check") or {}
                rows.append({
                    "run_id": rec.get("run_id"),
                    "domain": rec.get("domain"),
                    "host": rec.get("host"),
                    "host_type": rec.get("host_type"),
                    "trial_index": rec.get("trial_index"),
                    "protocol": rec.get("protocol"),
                    "vantage_point": rec.get("vantage_point"),
                    "baseline_rtt_ms": rec.get("baseline_rtt_ms"),
                    "timestamp_utc": rec.get("timestamp_utc"),
                    "success": rec.get("success"),
                    "failure_reason": rec.get("failure_reason"),
                    "request_count": rec.get("request_count"),
                    "navigated_host": rec.get("navigated_host"),
                    "next_hop_protocol": timing.get("nextHopProtocol"),
                    "dom_content_loaded_ms": timing.get("domContentLoaded_ms"),
                    "load_event_ms": timing.get("loadEvent_ms"),
                    "dom_interactive_ms": timing.get("domInteractive_ms"),
                    "response_start_ms": timing.get("responseStart_ms"),
                    "first_paint_ms": timing.get("firstPaint_ms"),
                    "first_contentful_paint_ms": timing.get("firstContentfulPaint_ms"),
                    "verified": netlog_check.get("verified", False),
                    "discard_reason": netlog_check.get("discard_reason"),
                })

    return pd.DataFrame(rows)
