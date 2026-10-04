"""DNS probing: A, AAAA, and HTTPS/SVCB (RR type 65) records.

The HTTPS record's `alpn` parameter is the modern H3-discovery path (brief
Phase 2 item 1) -- it's more meaningful than Alt-Svc because a client can
decide to try QUIC before ever opening a TCP connection. Expected to be
near-absent in Nepal; that absence is itself a recorded finding (explicit
False + empty list, never a missing/null field that could be misread as "we
didn't check").

Deliberately uses the *local* resolver (whatever `dns.asyncresolver` picks up
from the OS, i.e. the ISP's resolver on this vantage point) rather than a
public one. That is the opposite choice from corpus/build_corpus.py, which
uses public resolvers on purpose so corpus membership doesn't depend on
which network built it. Here, the local ISP resolver *is* the thing under
test -- Phase 2 is a from-Kathmandu measurement, and DNS behavior can differ
by resolver (e.g. an ISP resolver that strips unknown RR types).
"""
from __future__ import annotations

import dns.asyncresolver
import dns.exception
import dns.rdatatype
import dns.resolver


def _classify_dns_error(exc: Exception) -> str:
    if isinstance(exc, dns.resolver.NXDOMAIN):
        return "dns_nxdomain"
    if isinstance(exc, dns.resolver.NoAnswer):
        return "dns_no_answer"
    if isinstance(exc, dns.resolver.NoNameservers):
        return "dns_servfail"
    if isinstance(exc, dns.exception.Timeout):
        return "timeout"
    return f"error:{type(exc).__name__}"


async def _resolve(resolver: dns.asyncresolver.Resolver, host: str, rdtype: str):
    try:
        answer = await resolver.resolve(host, rdtype)
        return list(answer), None
    except dns.resolver.NoAnswer:
        return [], None  # valid: domain exists, just no record of this type
    except Exception as e:  # noqa: BLE001 -- many distinct DNS failure modes, all recorded
        return None, _classify_dns_error(e)


def _parse_https_rr(rdata) -> dict:
    params = getattr(rdata, "params", {}) or {}
    alpn = []
    ipv4hint = []
    ipv6hint = []
    ech_present = False
    for key, val in params.items():
        # dnspython exposes SVCB/HTTPS params keyed by an SvcParam subclass;
        # str(key)/param class name is the stable way to identify them
        # across dnspython versions without importing private enums.
        name = type(val).__name__.lower()
        if "alpn" in name:
            alpn = list(getattr(val, "ids", []))
            alpn = [a.decode() if isinstance(a, bytes) else a for a in alpn]
        elif "ipv4hint" in name:
            ipv4hint = [str(a) for a in getattr(val, "addresses", [])]
        elif "ipv6hint" in name:
            ipv6hint = [str(a) for a in getattr(val, "addresses", [])]
        elif "ech" in name:
            ech_present = True
    return {
        "priority": rdata.priority,
        "target": str(rdata.target),
        "alpn": alpn,
        "ipv4hint": ipv4hint,
        "ipv6hint": ipv6hint,
        "ech_present": ech_present,
    }


async def probe_dns(host: str, timeout_s: float) -> dict:
    """Returns a dict matching schema.py's "dns" section."""
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = timeout_s
    resolver.lifetime = timeout_s

    result = {
        "a": [], "aaaa": [],
        "https_rr": {"present": False, "records": [], "failure_reason": None},
        "failure_reason": None,
    }

    a_answers, a_err = await _resolve(resolver, host, "A")
    aaaa_answers, aaaa_err = await _resolve(resolver, host, "AAAA")

    if a_answers is not None:
        result["a"] = [r.address for r in a_answers]
    if aaaa_answers is not None:
        result["aaaa"] = [r.address for r in aaaa_answers]

    # Top-level failure_reason: only set if BOTH A and AAAA failed outright
    # (NXDOMAIN etc.) -- if either succeeded (even with an empty list, i.e.
    # NoAnswer) the host is not considered DNS-dead.
    if a_answers is None and aaaa_answers is None:
        result["failure_reason"] = a_err or aaaa_err

    https_answers, https_err = await _resolve(resolver, host, "HTTPS")
    if https_answers is not None:
        result["https_rr"]["present"] = len(https_answers) > 0
        result["https_rr"]["records"] = [_parse_https_rr(r) for r in https_answers]
    else:
        result["https_rr"]["failure_reason"] = https_err

    return result
