"""TLS 1.3 detail extraction from a completed aioquic connection.

This module does not open any connections itself -- quic_probe.py does the
handshake (QUIC and TLS 1.3 are cryptographically bound together per RFC
9001, so there's no separate "TLS probe" connection to make) and calls
`extract(protocol)` here once the handshake has completed.

Two things this module CANNOT measure, documented rather than silently
under-reported:

1. **Key-exchange group is read via a monkeypatch of aioquic's private TLS
   internals** (`aioquic.tls.Context`/`pull_server_hello`), because aioquic
   does not expose the negotiated group as a public attribute. The patch is
   narrow, pinned to aioquic 1.3.0's internal structure, and fails soft
   (returns None, logs nothing crash-worthy) if aioquic's internals change
   in a future version -- see `_patch_group_capture()`.

2. **Hybrid post-quantum groups (e.g. X25519MLKEM768) can never be observed
   by this probe**, not just "usually absent." aioquic's TLS client only
   advertises classical groups (secp256r1/384/521, X25519, X448) in its
   ClientHello `supported_groups` extension -- there is no hybrid-PQ support
   in the library. A TLS 1.3 server can only select a group the client
   offered, so even a server with full ML-KEM support will always negotiate
   a classical group against this probe. **RQ3's "hybrid PQ group presence"
   sub-question is therefore not answerable from this probe's output** --
   `key_exchange_group` here reflects "the best classical group aioquic and
   the server have in common," not "the group the server would prefer."
   Flagged in phases.md; would need a different TLS stack (e.g. a
   curl/OpenSSL 3.2+ build, or Chrome via Phase 4's CDP harness) to probe
   properly.

3. **ECH is not measured by a live handshake at all** -- aioquic has no ECH
   client implementation, so `ech_supported` here is *not* "the server
   accepted an ECH-encrypted ClientHello from us" (we never sent one). It's
   populated by scan.py from the DNS HTTPS-RR's `ech` SvcParam (see
   dns_probe.py) instead -- "the server published an ECH config," a
   reasonable but weaker proxy. This module leaves `ech_supported` as None;
   scan.py fills it in from the DNS side.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_PATCHED = False


def _patch_group_capture():
    """Monkeypatch aioquic.tls so the client-negotiated key-exchange group
    ends up on `Context._quic_nepal_key_exchange_group` after a handshake.

    Why a monkeypatch instead of a clean API: aioquic's `Context` parses the
    ServerHello's key_share (group, public_bytes) entirely inside
    `_client_handle_hello`, uses the group immediately to perform ECDH, and
    never stores it anywhere the caller can read afterward. Re-parsing the
    ServerHello ourselves would mean re-implementing TLS record framing.

    Safety: `_client_handle_hello` is a fully synchronous method (no
    `await` inside it), so even under asyncio concurrency, nothing else can
    run between `pull_server_hello` populating the module-level `_capture`
    dict and this wrapper reading it back onto the specific `self`
    (Context instance) that triggered it -- there is no window for two
    concurrent connections' handshakes to interleave and clobber each
    other's captured group.
    """
    global _PATCHED
    if _PATCHED:
        return
    try:
        import aioquic.tls as tls

        _capture: dict[str, int | None] = {}
        _orig_pull_server_hello = tls.pull_server_hello
        _orig_client_handle_hello = tls.Context._client_handle_hello

        def _capturing_pull_server_hello(buf):
            hello = _orig_pull_server_hello(buf)
            _capture["group"] = hello.key_share[0] if hello.key_share else None
            return hello

        def _patched_client_handle_hello(self, input_buf, output_buf):
            _orig_client_handle_hello(self, input_buf, output_buf)
            self._quic_nepal_key_exchange_group = _capture.get("group")

        tls.pull_server_hello = _capturing_pull_server_hello
        tls.Context._client_handle_hello = _patched_client_handle_hello
        _PATCHED = True
    except Exception as e:  # noqa: BLE001 -- this is best-effort instrumentation
        logger.warning("Could not patch aioquic for key-exchange-group capture: %s", e)


_patch_group_capture()

_GROUP_NAMES = {
    0x0017: "secp256r1",
    0x0018: "secp384r1",
    0x0019: "secp521r1",
    0x001D: "x25519",
    0x001E: "x448",
}


def extract(protocol) -> dict:
    """`protocol` is a connected aioquic QuicConnectionProtocol. Returns a
    dict matching schema.py's "tls" section (minus ech_supported, filled in
    by scan.py from the DNS side)."""
    result = {
        "cipher_suite": None, "key_exchange_group": None, "cert_issuer": None,
        "cert_chain_length": None, "cert_not_before": None, "cert_not_after": None,
        "ech_supported": None,
    }
    tls_ctx = getattr(protocol._quic, "tls", None)
    if tls_ctx is None:
        return result

    key_schedule = getattr(tls_ctx, "key_schedule", None)
    if key_schedule is not None:
        result["cipher_suite"] = getattr(key_schedule.cipher_suite, "name", str(key_schedule.cipher_suite))

    group_id = getattr(tls_ctx, "_quic_nepal_key_exchange_group", None)
    if group_id is not None:
        result["key_exchange_group"] = _GROUP_NAMES.get(group_id, f"unknown(0x{group_id:04x})")

    cert = getattr(tls_ctx, "_peer_certificate", None)
    chain = getattr(tls_ctx, "_peer_certificate_chain", None) or []
    if cert is not None:
        result["cert_issuer"] = cert.issuer.rfc4514_string()
        result["cert_chain_length"] = 1 + len(chain)
        result["cert_not_before"] = cert.not_valid_before_utc.isoformat()
        result["cert_not_after"] = cert.not_valid_after_utc.isoformat()

    return result
