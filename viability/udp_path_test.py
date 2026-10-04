#!/usr/bin/env python3
"""Phase 3 -- UDP/443 path viability (RQ5).

Is UDP/443 actually usable across Nepali ISPs, or do blocking,
rate-limiting, MTU issues, or NAT rebinding force TCP fallback? Tested
against a fixed list of reference servers with known-good, stable HTTP/3
support (config/settings.yaml: viability.reference_servers), not against
the Nepali corpus -- Phase 2 already establishes per-domain QUIC support;
this phase characterizes the *path*, using servers whose own QUIC support
is not in question, so a failure here is attributable to the network path,
not to the reference server.

Four measurements per reference server, matching the brief's Phase 3 list:

1. **Handshake completion** -- does UDP/443 complete a QUIC handshake at
   all, reusing quic_probe.direct_attempt.
2. **MTU / PMTUD behavior** -- attempt a handshake with the client's
   max_datagram_size (hence Initial-packet target padding) swept across
   config.viability.initial_padding_bytes. A large size failing while a
   smaller one succeeds is exactly the brief's stated signature of
   middlebox interference. Caveat, stated plainly: this varies aioquic's
   configured *target* datagram size, which is what aioquic uses to decide
   how much to pad outgoing packets -- it is not a byte-level guarantee of
   what left the network interface, and this script does not independently
   capture packets to confirm it. Treated as a reasonable, standard way to
   exercise this behavior, not as a wire-level certification.
3. **Idle-period survival** -- establishes a real HTTP/3 connection, idles
   for each of config.viability.idle_intervals_s, then sends a follow-up
   request on the *same* connection object and records whether it
   completes. **This is deliberately described as idle-survival, not NAT
   rebinding.** A genuine NAT-rebinding test requires the client's own
   observable 5-tuple to actually change mid-connection (simulating what a
   carrier-grade NAT does when it remaps a UDP mapping) and confirming the
   server still recognizes the connection via its QUIC connection ID --
   aioquic's asyncio wrapper has no supported public API for swapping a
   live connection onto a new local transport mid-flight, and building one
   correctly (bypassing the wrapper to drive a raw QuicConnection object)
   was judged out of scope for this pass. Idle-survival is a necessary
   precondition for rebinding-survival (a connection that dies from idling
   alone would obviously also fail a real rebind), so it's still an
   informative, honestly-scoped measurement -- just not the whole claim the
   brief describes. Flagged here and in phases.md, not silently narrowed.
4. **Asymmetric throughput** -- downloads each reference server's own
   homepage over QUIC (H3) vs. TCP (H2), comparing achieved throughput.
   Originally planned against a dedicated multi-MB speed-test payload
   (Cloudflare's speed.cloudflare.com), which turned out not to support
   HTTP/3 at all when checked directly -- see config/settings.yaml's
   comment on `throughput_test_bytes_note`. Falls back to each server's own
   homepage, which is real traffic but not a controlled-size payload;
   documented, not hidden.

Output: one JSON record per (reference_server, vantage_point) appended to
data/raw/viability_<run_id>.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from aioquic.asyncio import connect
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW_DIR = REPO_ROOT / "data" / "raw"

sys.path.insert(0, str(REPO_ROOT))
from probe.quic_probe import _classify_quic_error, install_quiet_exception_handler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("udp_path_test")


def load_settings() -> dict:
    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


class _ThroughputH3Protocol:
    """Not a QuicConnectionProtocol subclass -- a thin event handler shared
    by the idle-survival and throughput tests, both of which need to issue
    more than one HTTP/3 request on a connection that's already open."""

    def __init__(self, quic):
        self.http = H3Connection(quic)
        self.pending: dict[int, asyncio.Future] = {}
        self.bytes_received: dict[int, int] = {}

    def handle(self, event):
        for http_event in self.http.handle_event(event):
            if isinstance(http_event, (HeadersReceived, DataReceived)):
                sid = http_event.stream_id
                if isinstance(http_event, DataReceived):
                    self.bytes_received[sid] = self.bytes_received.get(sid, 0) + len(http_event.data)
                if getattr(http_event, "stream_ended", False):
                    fut = self.pending.pop(sid, None)
                    if fut and not fut.done():
                        fut.set_result(True)

    async def get(self, protocol, host: str, path: str, user_agent: str, timeout_s: float) -> tuple[bool, int]:
        stream_id = protocol._quic.get_next_available_stream_id()
        self.bytes_received[stream_id] = 0
        self.http.send_headers(stream_id, [
            (b":method", b"GET"), (b":scheme", b"https"),
            (b":authority", host.encode()), (b":path", path.encode()),
            (b"user-agent", user_agent.encode()),
        ], end_stream=True)
        fut = asyncio.get_event_loop().create_future()
        self.pending[stream_id] = fut
        protocol.transmit()
        try:
            await asyncio.wait_for(fut, timeout=timeout_s)
            return True, self.bytes_received.get(stream_id, 0)
        except asyncio.TimeoutError:
            return False, self.bytes_received.get(stream_id, 0)


def _make_protocol_class():
    """create_protocol callables must be zero-extra-arg constructors per
    aioquic's API; wraps _ThroughputH3Protocol as the QuicConnectionProtocol
    subclass connect() actually instantiates."""
    from aioquic.asyncio.protocol import QuicConnectionProtocol

    class _P(QuicConnectionProtocol):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.h3 = _ThroughputH3Protocol(self._quic)

        def quic_event_received(self, event):
            self.h3.handle(event)

    return _P


async def test_handshake(ip: str, host: str, timeout_s: float) -> dict:
    from probe.quic_probe import direct_attempt
    quic_result, _, _ = await direct_attempt(ip, 443, host, timeout_s)
    return {
        "success": quic_result["direct_attempt_success"],
        "handshake_duration_ms": quic_result["handshake_duration_ms"],
        "failure_reason": quic_result["failure_reason"],
    }


async def test_mtu(ip: str, host: str, sizes: list[int], timeout_s: float) -> list[dict]:
    results = []
    for size in sizes:
        config = QuicConfiguration(alpn_protocols=H3_ALPN, is_client=True, verify_mode=ssl.CERT_REQUIRED,
                                     max_datagram_size=size, idle_timeout=10.0)
        config.server_name = host
        start = time.monotonic()
        outcome = {"target_datagram_size": size, "success": False, "duration_ms": None, "failure_reason": None}
        try:
            async with asyncio.timeout(timeout_s):
                async with connect(ip, 443, configuration=config, wait_connected=True):
                    outcome["success"] = True
                    outcome["duration_ms"] = (time.monotonic() - start) * 1000
        except Exception as e:  # noqa: BLE001
            outcome["failure_reason"] = _classify_quic_error(e)
        results.append(outcome)
    return results


async def test_idle_survival(ip: str, host: str, idle_intervals_s: list[int], timeout_s: float,
                               user_agent: str) -> list[dict]:
    """One connection per interval (not one connection tested against all
    intervals in sequence) -- reusing a single connection across
    successively longer idles would confound "did it survive 30s" with "did
    it survive 30s+60s+120s of prior traffic," which isn't what the brief
    is asking."""
    results = []
    protocol_cls = _make_protocol_class()
    for interval in idle_intervals_s:
        outcome = {"idle_s": interval, "handshake_ok": False, "resumed_ok": False, "failure_reason": None}
        config = QuicConfiguration(alpn_protocols=H3_ALPN, is_client=True, verify_mode=ssl.CERT_REQUIRED, idle_timeout=max(60.0, interval + 20.0))
        config.server_name = host

        # NOTE: setup and the resume request each get their own bounded
        # timeout (timeout_s, 10s); the idle sleep in between is
        # deliberately NOT wrapped in any timeout, since interval can be up
        # to 120s. An earlier version of this function wrapped the whole
        # block -- including the sleep -- in a single `asyncio.timeout(timeout_s)`,
        # which silently cancelled every idle-survival test after ~10s
        # regardless of the configured interval. That bug produced a
        # suspiciously uniform 100% "failure" across every server and every
        # interval in the first run, which is what caught it (a real
        # NAT-rebinding effect would not be *that* uniform). See phases.md
        # Phase 3 for the full account.
        cm = connect(ip, 443, configuration=config, create_protocol=protocol_cls, wait_connected=True)
        protocol = None
        try:
            async with asyncio.timeout(timeout_s):
                protocol = await cm.__aenter__()
            outcome["handshake_ok"] = True
            await protocol.h3.get(protocol, host, "/", user_agent, timeout_s)  # warm-up request
            await asyncio.sleep(interval)
            async with asyncio.timeout(timeout_s):
                ok, _ = await protocol.h3.get(protocol, host, "/", user_agent, timeout_s)
            outcome["resumed_ok"] = ok
            if not ok:
                outcome["failure_reason"] = "timeout"
        except Exception as e:  # noqa: BLE001
            outcome["failure_reason"] = _classify_quic_error(e)
        finally:
            if protocol is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        results.append(outcome)
    return results


async def test_throughput(ip: str, host: str, timeout_s: float, user_agent: str) -> dict:
    result = {
        "quic": {"success": False, "bytes": 0, "duration_ms": None, "bytes_per_s": None, "failure_reason": None},
        "tcp_h2": {"success": False, "bytes": 0, "duration_ms": None, "bytes_per_s": None, "failure_reason": None},
    }

    protocol_cls = _make_protocol_class()
    config = QuicConfiguration(alpn_protocols=H3_ALPN, is_client=True, verify_mode=ssl.CERT_REQUIRED, idle_timeout=10.0)
    config.server_name = host
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(ip, 443, configuration=config, create_protocol=protocol_cls, wait_connected=True) as protocol:
                start = time.monotonic()
                ok, nbytes = await protocol.h3.get(protocol, host, "/", user_agent, timeout_s)
                dur = (time.monotonic() - start) * 1000
                result["quic"] = {"success": ok, "bytes": nbytes, "duration_ms": dur,
                                    "bytes_per_s": (nbytes / (dur / 1000)) if ok and dur > 0 else None,
                                    "failure_reason": None if ok else "timeout"}
    except Exception as e:  # noqa: BLE001
        result["quic"]["failure_reason"] = _classify_quic_error(e)

    try:
        async with httpx.AsyncClient(http2=True, http1=False, timeout=timeout_s,
                                       headers={"User-Agent": user_agent}) as client:
            start = time.monotonic()
            resp = await client.get(f"https://{host}/")
            dur = (time.monotonic() - start) * 1000
            nbytes = len(resp.content)
            result["tcp_h2"] = {"success": resp.status_code < 500, "bytes": nbytes, "duration_ms": dur,
                                  "bytes_per_s": (nbytes / (dur / 1000)) if dur > 0 else None, "failure_reason": None}
    except Exception as e:  # noqa: BLE001
        result["tcp_h2"]["failure_reason"] = f"error:{type(e).__name__}"

    return result


async def run_one_server(host: str, operator: str, cfg: dict, vantage_point: str) -> dict:
    import socket as socket_mod
    viability_cfg = cfg["viability"]
    timeout_s = cfg["probe"]["per_domain_timeout_s"]
    user_agent = cfg["study"]["user_agent"]

    rec = {
        "host": host, "operator": operator, "vantage_point": vantage_point,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    try:
        ip = socket_mod.getaddrinfo(host, 443, socket_mod.AF_INET)[0][4][0]
    except Exception as e:
        rec["dns_failure"] = f"error:{type(e).__name__}"
        return rec
    rec["ip"] = ip

    log.info("[%s] handshake test", host)
    rec["handshake"] = await test_handshake(ip, host, timeout_s)

    log.info("[%s] MTU/PMTUD sweep", host)
    rec["mtu"] = await test_mtu(ip, host, viability_cfg["initial_padding_bytes"], timeout_s)

    log.info("[%s] idle-survival test", host)
    rec["idle_survival"] = await test_idle_survival(ip, host, viability_cfg["idle_intervals_s"], timeout_s, user_agent)

    log.info("[%s] throughput comparison", host)
    rec["throughput"] = await test_throughput(ip, host, timeout_s, user_agent)

    return rec


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--vantage-point", required=True)
    args = ap.parse_args()

    cfg = load_settings()
    install_quiet_exception_handler(asyncio.get_event_loop())

    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_RAW_DIR / f"viability_{args.run_id}.jsonl"

    servers = cfg["viability"]["reference_servers"]
    log.info("Testing %d reference servers from vantage point %s", len(servers), args.vantage_point)

    with open(out_path, "a", buffering=1) as f:
        for s in servers:
            rec = await run_one_server(s["host"], s["operator"], cfg, args.vantage_point)
            f.write(json.dumps(rec) + "\n")
            f.flush()

    log.info("=== Phase 3 complete: %d servers tested, output: %s ===", len(servers), out_path)


if __name__ == "__main__":
    asyncio.run(main())
