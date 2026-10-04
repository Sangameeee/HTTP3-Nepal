"""Versioned schema for the per-host probe record.

One JSON object per *host* (apex and `www` are separate records — see
README's "apex vs www" note) per scan run, appended to a JSONL file in
`data/raw/`. `data/raw/` is append-only; nothing here is ever edited after
being written (Correctness requirement 6 in the brief).

Bump SCHEMA_VERSION on any incompatible field change and note the change in
phases.md; analysis/load.py should branch on schema_version rather than
assume every record it reads was written by today's code.

Failure reasons are deliberately an open set of short strings, not an enum,
so a probe module can record something specific ("udp_no_reply" vs.
"timeout" vs. "connection_refused") without a schema migration -- but the
constants below are the vocabulary every probe module should draw from
first, so failure_reason values stay comparable across the dataset instead
of accumulating one-off spellings.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0.0"

# Shared failure-reason vocabulary. A probe module may still emit a reason
# not in this list if none of these fit, but should check here first -- this
# list exists so "the connection timed out" isn't spelled three different
# ways across dns_probe.py / h2_probe.py / quic_probe.py.
FAILURE_REASONS = {
    "timeout",
    "connection_refused",
    "tls_error",
    "no_quic_response",   # TCP/H2 worked but nothing came back over QUIC -- distinct from udp_no_reply
    "udp_no_reply",       # UDP packet(s) sent, nothing at all came back (possible path blocking)
    "dns_nxdomain",
    "dns_no_answer",
    "dns_servfail",
    "http_error",         # got an HTTP response, but not one usable for the probe (5xx, etc.)
    "protocol_mismatch",  # e.g. netlog shows H2 was actually used despite forcing H3
    "not_attempted",      # probe intentionally skipped (e.g. 0-RTT skipped because 1-RTT already failed)
}


def new_record(*, domain: str, host: str, host_type: str, sector: str, run_id: str,
                vantage_point: str, tool_versions: dict[str, str]) -> dict[str, Any]:
    """Skeleton for one probe record. Every probe module fills in its own
    top-level section; nothing here is optional to include (use null/None
    for "not yet run", not a missing key) so downstream analysis code can
    assume every key is present."""
    return {
        "schema_version": SCHEMA_VERSION,
        "domain": domain,          # registrable domain, from corpus/domains.csv
        "host": host,              # the exact hostname probed (== domain for apex, "www."+domain for www)
        "host_type": host_type,    # "apex" | "www"
        "sector": sector,
        "run_id": run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "vantage_point": vantage_point,
        "tool_versions": tool_versions,

        "dns": {
            "a": [],
            "aaaa": [],
            "https_rr": {
                "present": False,
                "records": [],       # [{priority, target, alpn[], ipv4hint[], ipv6hint[], ech_present}]
                "failure_reason": None,
            },
            "failure_reason": None,  # set if A/AAAA lookup itself failed (nxdomain etc.)
        },

        "h2": {
            "attempted": False,
            "success": False,
            "status_code": None,
            "alt_svc_raw": None,
            "alt_svc_parsed": [],    # [{protocol, host, port, ma, persist}]
            "duration_ms": None,
            "failure_reason": None,
        },

        # Derived booleans, filled in by scan.py after dns+h2+quic all ran:
        #   h3_advertised = HTTPS-RR alpn=h3 present OR Alt-Svc advertises h3
        #   h3_supported  = direct QUIC attempt succeeded, regardless of advertisement
        # The mismatch between the two is itself a recorded finding (brief Phase 2 item 3).
        "h3_advertised": None,
        "h3_supported": None,

        "quic": {
            "attempted": False,
            "direct_attempt_success": False,
            "handshake_duration_ms": None,
            "negotiated_version": None,      # e.g. "0x00000001"
            "version_negotiation": {
                "attempted": False,
                "sent_version": "0x1a2a3a4a",  # deliberately-reserved version, forces a VN packet
                "server_versions": [],          # every version the server listed in its VN packet
                "failure_reason": None,
            },
            "zero_rtt": {
                "attempted": False,
                "accepted": None,
                "failure_reason": None,
            },
            "failure_reason": None,
        },

        "tls": {
            "cipher_suite": None,
            "key_exchange_group": None,
            "cert_issuer": None,
            "cert_chain_length": None,
            "cert_not_before": None,
            "cert_not_after": None,
            "ech_supported": None,
        },

        "ips": [],           # union of resolved A/AAAA
        "asns": [],           # [{ip, asn, as_name, ownership_class}]
        "ownership_class": None,  # foreign_cdn | foreign_hosting | nepali_hosting | unknown | mixed | None

        "cross_validation": {
            "sampled": False,
            "curl_http3_attempted": False,
            "curl_http3_success": None,
            "agrees_with_primary": None,
        },

        "failure_reason": None,   # top-level: set only if the whole record is unusable end to end
        "probe_durations_ms": {"dns": None, "h2": None, "quic": None, "tls": None, "asn": None, "total": None},
    }
