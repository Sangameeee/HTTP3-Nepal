"""HTTP/2 baseline fetch + Alt-Svc parsing.

This is the baseline every other probe is compared against: same-origin H2
vs H3 timing (Phase 4) only means anything if we know H2 actually worked and
what it advertised. Also the origin of one of Phase 2's more interesting
recorded variables -- whether a server's *actual* QUIC support (from
quic_probe.py) matches what it *advertised* here via Alt-Svc (brief Phase 2
item 3).
"""
from __future__ import annotations

import re
import time

import httpx

# Matches one Alt-Svc entry: token=quoted-host-port, optionally followed by
# ";ma=N" and/or ";persist=1". Alt-Svc can list multiple such entries
# separated by commas, e.g.: h3=":443"; ma=86400, h3-29=":443"; ma=86400
_ALT_SVC_ENTRY_RE = re.compile(
    r'([\w-]+)="([^"]*)"'          # protocol="host:port"
    r'((?:\s*;\s*\w+=[^,;]+)*)'    # trailing ;key=value params
)
_PARAM_RE = re.compile(r"(\w+)=([^,;\s]+)")


def parse_alt_svc(raw: str | None) -> list[dict]:
    if not raw:
        return []
    out = []
    for proto, hostport, params_str in _ALT_SVC_ENTRY_RE.findall(raw):
        host, _, port = hostport.rpartition(":")
        entry = {"protocol": proto, "host": host or None, "port": port or None, "ma": None, "persist": False}
        for key, val in _PARAM_RE.findall(params_str):
            key = key.strip().lower()
            if key == "ma":
                try:
                    entry["ma"] = int(val)
                except ValueError:
                    pass
            elif key == "persist":
                entry["persist"] = val.strip() == "1"
        out.append(entry)
    return out


def _classify_httpx_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout) or isinstance(exc, httpx.PoolTimeout):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc).lower()
        if "nodename nor servname" in msg or "name or service not known" in msg:
            return "dns_nxdomain"
        if "refused" in msg:
            return "connection_refused"
        return "connection_refused"
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ProtocolError)):
        return "tls_error"
    return f"error:{type(exc).__name__}"


async def probe_h2(host: str, user_agent: str, timeout_s: float) -> dict:
    """GETs https://<host>/ over HTTP/2 (falls back to whatever httpx
    negotiates if the server doesn't do H2, recorded via response.http_version).
    Returns a dict matching schema.py's "h2" section."""
    result = {
        "attempted": True, "success": False, "status_code": None,
        "alt_svc_raw": None, "alt_svc_parsed": [], "duration_ms": None,
        "http_version": None, "failure_reason": None,
    }
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            http2=True, http1=True, verify=True, follow_redirects=True,
            timeout=timeout_s, headers={"User-Agent": user_agent},
        ) as client:
            resp = await client.get(f"https://{host}/")
            result["status_code"] = resp.status_code
            result["http_version"] = resp.http_version
            result["success"] = resp.status_code < 500
            alt_svc = resp.headers.get("alt-svc")
            result["alt_svc_raw"] = alt_svc
            result["alt_svc_parsed"] = parse_alt_svc(alt_svc)
    except Exception as e:  # noqa: BLE001 -- many distinct failure modes, all recorded
        result["failure_reason"] = _classify_httpx_error(e)
    result["duration_ms"] = (time.monotonic() - start) * 1000
    return result
