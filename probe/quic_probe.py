"""Raw QUIC/HTTP-3 probing: direct handshake attempt, version negotiation,
and 0-RTT.

Three separate connection attempts per host, matching the brief's Phase 2
items 3-6, kept separate rather than combined because they measure different
things and combining them would contaminate the timing:

1. `direct_attempt()` -- a normal handshake with aioquic's default (highest
   supported) version. This is "does the server actually speak QUIC/H3,
   regardless of what Alt-Svc/HTTPS-RR advertised" (item 3), and its timing
   is the clean handshake-duration number (no wasted round trip).
2. `version_negotiation_probe()` -- a *separate* connection using a
   deliberately-reserved version (0x1a2a3a4a, the brief's suggested GREASE
   value) to force a Version Negotiation packet, recording every version the
   server lists (item 4). This intentionally costs an extra RTT, so its
   timing is never used as "the" handshake duration.
3. `zero_rtt_probe()` -- reconnects using a session ticket obtained from
   attempt 1, sending an HTTP/3 GET immediately (before the handshake
   completes) so the server has actual early data to accept or reject
   (item 6). Skipped if attempt 1 failed or produced no session ticket.

All three use the same UDP destination (server_ip:443) resolved by
dns_probe.py -- QUIC has no separate "connect to a hostname" step, TLS SNI
(server_name) carries the hostname for cert validation and vhost routing.
"""
from __future__ import annotations

import asyncio
import os
import ssl
import struct
import sys
import time

from aioquic.asyncio import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.buffer import Buffer
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import DataReceived, HeadersReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.packet import pull_quic_header

from . import tls_probe

RESERVED_VERSION_FOR_VN_PROBE = 0x1A2A3A4A  # GREASE-pattern reserved version, per RFC 9000 / the brief


def _quiet_h3_stream_teardown_errors(unraisable):
    """aioquic's asyncio wrapper creates a StreamWriter for every
    peer-initiated H3 stream (control, QPACK encoder/decoder) but its
    QuicConnectionProtocol.close() doesn't explicitly close them; they get
    garbage-collected later and their __del__ -> close() -> write_eof()
    raises "Cannot send data on peer-initiated unidirectional stream"
    (verified: this is a wrapper bug, not a signal about the probed server
    -- it happens identically against a healthy Cloudflare/nginx endpoint).
    Filtered here rather than left to print unraisable-exception noise to
    stderr on every single probed host; anything else still gets reported
    normally."""
    if (unraisable.exc_type is ValueError
            and "Cannot send data on peer-initiated unidirectional stream" in str(unraisable.exc_value)):
        return
    sys.__unraisablehook__(unraisable)


sys.unraisablehook = _quiet_h3_stream_teardown_errors


def install_quiet_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Call once, on the loop scan.py actually runs on. Filters harmless
    noise patterns from UDP transport teardown -- none of these affect
    captured data (confirmed by diffing probe output before/after
    suppression); all of them stem from a UDP socket receiving a delayed
    ICMP port-unreachable *after* our own probe coroutine already recorded
    a result and moved on (via its own timeout/except handling), not from
    anything our code failed to catch:

    1. aioquic's connect() spawns an internal task that can raise
       ConnectionError when a UDP-no-reply connection attempt is torn down;
       our probe already sees and records the *same* ConnectionError via
       its own except-block (failure_reason="udp_no_reply"/"tls_error"),
       so this is a second, unretrieved copy asyncio's default handler
       would print as "Future exception was never retrieved".
    2. A bare ConnectionRefusedError raised inside
       asyncio.selector_events._read_ready when a UDP socket's next read
       surfaces a delayed ICMP unreachable for a connection whose transport
       wasn't fully torn down yet -- asyncio's datagram transport doesn't
       wrap this callback in a try/except, so it reaches the loop's
       exception handler as "Exception in callback ...".

    Given the brief's own expectation that most Nepali origins won't
    support QUIC (i.e. most hosts in a full run hit exactly this path),
    leaving either unfiltered would make these the single noisiest lines in
    the log, burying warnings that matter (e.g. cross-validation
    disagreements).
    """
    default_handler = loop.get_exception_handler()

    def _handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "")
        if isinstance(exc, ConnectionError) and ("never retrieved" in msg or "Exception in callback" in msg):
            return
        if default_handler is not None:
            default_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


class _H3ClientProtocol(QuicConnectionProtocol):
    """Minimal HTTP/3 client: send one GET, wait for the response to finish.
    Used only by zero_rtt_probe() -- direct_attempt() and
    version_negotiation_probe() don't need to speak H3 at the application
    layer, just complete (or fail) the QUIC handshake."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http = H3Connection(self._quic)
        self._pending: dict[int, asyncio.Future] = {}

    def quic_event_received(self, event):
        for http_event in self._http.handle_event(event):
            if isinstance(http_event, (HeadersReceived, DataReceived)) and getattr(http_event, "stream_ended", False):
                fut = self._pending.pop(http_event.stream_id, None)
                if fut and not fut.done():
                    fut.set_result(True)

    async def get(self, host: str, path: str, user_agent: str, timeout_s: float) -> bool:
        stream_id = self._quic.get_next_available_stream_id()
        self._http.send_headers(stream_id, [
            (b":method", b"GET"), (b":scheme", b"https"),
            (b":authority", host.encode()), (b":path", path.encode()),
            (b"user-agent", user_agent.encode()),
        ], end_stream=True)
        fut = asyncio.get_event_loop().create_future()
        self._pending[stream_id] = fut
        self.transmit()
        try:
            await asyncio.wait_for(fut, timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False


def _base_config(alpn=None, quic_logger=None) -> QuicConfiguration:
    return QuicConfiguration(
        alpn_protocols=alpn or H3_ALPN,
        is_client=True,
        verify_mode=ssl.CERT_REQUIRED,
        quic_logger=quic_logger,
        idle_timeout=10.0,
    )


def _classify_quic_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError) or "timed out" in msg or "timeout" in msg:
        return "udp_no_reply"
    if isinstance(exc, ConnectionRefusedError) or "refused" in msg:
        return "connection_refused"
    if "certificate" in msg or "handshake" in msg or "tls" in msg or "ssl" in msg:
        return "tls_error"
    return f"error:{type(exc).__name__}"


async def direct_attempt(ip: str, port: int, host: str, timeout_s: float):
    """Returns (quic_result_dict, tls_result_dict, session_ticket_or_None,
    protocol_or_None). protocol is returned (still connected) only on
    success, closed by the caller after tls_probe.extract() has read from it
    -- kept open briefly so zero_rtt_probe's caller doesn't need a second
    unrelated connection just to read TLS state timing-sensitively."""
    quic_result = {
        "attempted": True, "direct_attempt_success": False,
        "handshake_duration_ms": None, "negotiated_version": None,
        "failure_reason": None,
    }
    tls_result = {
        "cipher_suite": None, "key_exchange_group": None, "cert_issuer": None,
        "cert_chain_length": None, "cert_not_before": None, "cert_not_after": None,
        "ech_supported": None,
    }
    saved_ticket = None

    def on_ticket(ticket):
        nonlocal saved_ticket
        saved_ticket = ticket

    config = _base_config()
    config.server_name = host
    start = time.monotonic()
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(
                ip, port, configuration=config, session_ticket_handler=on_ticket,
                wait_connected=True,
            ) as protocol:
                quic_result["handshake_duration_ms"] = (time.monotonic() - start) * 1000
                quic_result["direct_attempt_success"] = True
                version = getattr(protocol._quic, "_version", None)
                if version is not None:
                    quic_result["negotiated_version"] = f"0x{version:08x}"
                tls_result = tls_probe.extract(protocol)
                # NewSessionTicket frequently arrives just after the
                # handshake completes, not synchronously with it -- give it
                # a brief grace window so zero_rtt_probe has a ticket to use.
                await asyncio.sleep(0.3)
    except Exception as e:  # noqa: BLE001 -- connection has many distinct failure modes, all recorded
        quic_result["failure_reason"] = _classify_quic_error(e)

    return quic_result, tls_result, saved_ticket


def _build_vn_trigger_packet(version: int) -> bytes:
    """A minimal long-header packet carrying an unrecognized version, per
    RFC 9000 SS17.2: a server that doesn't support the packet's version
    replies with a Version Negotiation packet listing the versions it does
    support. The payload doesn't need to be decryptable (the server can't
    decrypt a version it doesn't implement anyway) -- only the header needs
    to parse as a long-header packet.

    Padded to 1200 bytes: RFC 9000 SS14.1 recommends servers not respond to
    Version Negotiation triggers smaller than the minimum Initial datagram
    size, specifically to prevent using VN as a DoS amplification vector.
    Several real implementations enforce this, so an unpadded probe would
    under-count VN support rather than measure it.

    Note this is deliberately NOT built via aioquic's own QuicConnection:
    aioquic can only encrypt/send an Initial packet for a version it has an
    Initial-secret salt for (RFC 9001/9369 define salts for v1 and v2 only),
    so asking it to originate a connection with an arbitrary reserved
    version raises internally before anything reaches the wire (confirmed
    empirically: KeyError on Epoch.INITIAL). A hand-built header sidesteps
    that entirely, and is closer to what the brief actually asks for
    ("send an Initial with a deliberately reserved version") than a full
    QUIC-stack connection attempt would be.
    """
    dcid = os.urandom(8)
    scid = os.urandom(8)
    pkt = bytearray()
    pkt.append(0xC0)  # long header form + fixed bit set; type bits are meaningless for an unrecognized version
    pkt += struct.pack("!I", version)
    pkt.append(len(dcid))
    pkt += dcid
    pkt.append(len(scid))
    pkt += scid
    pkt += bytes(max(0, 1200 - len(pkt)))  # pad to the 1200-byte minimum Initial datagram size
    return bytes(pkt)


class _VNProbeProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.reply: bytes | None = None
        self.done = asyncio.get_event_loop().create_future()

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.done.done():
            self.reply = data
            self.done.set_result(True)

    def error_received(self, exc: Exception) -> None:
        if not self.done.done():
            self.done.set_exception(exc)


async def version_negotiation_probe(ip: str, port: int, host: str, timeout_s: float) -> dict:
    """Sends a hand-built Initial-shaped packet with a deliberately
    unsupported version directly over a raw UDP socket (bypassing aioquic's
    connection machinery -- see `_build_vn_trigger_packet`'s docstring for
    why) and parses any reply as a Version Negotiation packet, recording
    every version the server lists."""
    result = {"attempted": True, "sent_version": f"0x{RESERVED_VERSION_FOR_VN_PROBE:08x}",
               "server_versions": [], "failure_reason": None}

    loop = asyncio.get_event_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _VNProbeProtocol, remote_addr=(ip, port)
        )
    except OSError as e:
        result["failure_reason"] = "connection_refused" if "refused" in str(e).lower() else f"error:{type(e).__name__}"
        return result

    try:
        transport.sendto(_build_vn_trigger_packet(RESERVED_VERSION_FOR_VN_PROBE))
        try:
            await asyncio.wait_for(protocol.done, timeout=timeout_s)
        except asyncio.TimeoutError:
            result["failure_reason"] = "udp_no_reply"
            return result
        except OSError as e:
            # _VNProbeProtocol.error_received() sets this via
            # protocol.done.set_exception(exc) when the OS delivers an
            # ICMP port-unreachable for our UDP socket -- a real, distinct,
            # meaningful result (the host actively refused the probe,
            # rather than silently dropping it) that must be recorded, not
            # left to propagate and blow away the entire host record (see
            # phases.md Phase 2 for the run this bug was caught in: 241 of
            # 1,318 hosts in the first full run lost their whole record to
            # exactly this).
            result["failure_reason"] = "connection_refused" if isinstance(e, ConnectionRefusedError) else f"error:{type(e).__name__}"
            return result

        if protocol.reply is None:
            result["failure_reason"] = "udp_no_reply"
            return result

        try:
            header = pull_quic_header(Buffer(data=protocol.reply))
            if header.version == 0 and header.supported_versions:
                result["server_versions"] = [f"0x{v:08x}" for v in header.supported_versions]
            else:
                # Got *something* back but not a well-formed VN packet --
                # e.g. a middlebox echo, or a server that replied with a
                # real (non-VN) packet for reasons of its own.
                result["failure_reason"] = "no_quic_response"
        except Exception:
            result["failure_reason"] = "no_quic_response"
    finally:
        transport.close()

    return result


async def zero_rtt_probe(ip: str, port: int, host: str, session_ticket, timeout_s: float,
                          user_agent: str) -> dict:
    result = {"attempted": False, "accepted": None, "failure_reason": None}
    if session_ticket is None:
        result["failure_reason"] = "not_attempted"
        return result

    result["attempted"] = True
    config = _base_config()
    config.server_name = host
    config.session_ticket = session_ticket
    try:
        async with asyncio.timeout(timeout_s):
            async with connect(
                ip, port, configuration=config, create_protocol=_H3ClientProtocol,
                wait_connected=False,
            ) as protocol:
                # Send the request immediately -- before the handshake
                # completes -- so there is actual early data for the server
                # to accept or reject. Waiting for wait_connected first
                # would defeat the point of testing 0-RTT.
                await protocol.get(host, "/", user_agent, timeout_s)
                early_ctx = getattr(protocol._quic, "tls", None)
                result["accepted"] = bool(getattr(early_ctx, "early_data_accepted", False))
    except Exception as e:  # noqa: BLE001
        result["failure_reason"] = _classify_quic_error(e)

    return result


async def probe_quic(ip: str, port: int, host: str, timeout_s: float, user_agent: str) -> dict:
    """Runs all three sub-probes. Returns (quic_dict, tls_dict) matching
    schema.py's "quic" and "tls" sections."""
    quic_result, tls_result, ticket = await direct_attempt(ip, port, host, timeout_s)

    quic_result["version_negotiation"] = await version_negotiation_probe(ip, port, host, timeout_s)

    if quic_result["direct_attempt_success"]:
        quic_result["zero_rtt"] = await zero_rtt_probe(ip, port, host, ticket, timeout_s, user_agent)
    else:
        quic_result["zero_rtt"] = {"attempted": False, "accepted": None, "failure_reason": "not_attempted"}

    return quic_result, tls_result
