#!/usr/bin/env python3
"""Mandatory netlog-based protocol verification for Phase 4b page-load
trials -- per the brief, "the single most common way studies like this
get invalidated" is trusting only a JS-visible signal (nextHopProtocol)
that the intended forced protocol was actually what got used on the wire.

Chromium's own network stack writes the ground truth to its netlog
(--log-net-log=<path>) independent of the renderer/JS layer. This module
parses that JSON and extracts the *actual* wire protocol via the
HTTP_STREAM_JOB event of type "main" (not "dns_alpn_h3", which is a
speculative alt-job that may not end up used) for the navigated host,
reading its `using_quic` boolean. Verified against real captures against
cloudflare-quic.com with both --origin-to-force-quic-on and
--disable-http3 (see phases.md Phase 4b) -- this is the actual event
Chromium's H3/H2 stream selection logic emits, not a guessed schema.

A trial is "verified" only when netlog's using_quic and the page's own
performance.getEntriesByType('navigation')[0].nextHopProtocol (passed in
by the caller) agree on h3-vs-not-h3. Disagreement -- e.g. the page
silently fell back to H2 despite --origin-to-force-quic-on, which does
happen when a QUIC handshake fails and Chromium's HTTP stream job
controller races a TCP fallback -- means the trial doesn't actually test
what it claims to, and must be discarded, not averaged in.
"""
from __future__ import annotations

import json
from pathlib import Path


def _event_type_map(netlog: dict) -> dict[int, str]:
    return {v: k for k, v in netlog["constants"]["logEventTypes"].items()}


def verify_protocol(netlog_path: str | Path, expected_protocol: str, next_hop_protocol: str | None,
                     navigated_host: str) -> dict:
    """expected_protocol: "h3" or "h2" (what the trial *tried* to force).
    next_hop_protocol: the JS-visible performance API value for this trial,
    or None if the page load didn't complete far enough to read it.
    navigated_host: the host of the page actually rendered (from
    page.url after load completes, NOT the originally-requested host) --
    a same-origin redirect (apex -> www or http -> https) changes which
    HTTP_STREAM_JOB corresponds to the document the timing numbers
    describe. A same-page subresource (analytics beacon, web font, CDN
    script) from a *different* origin gets its own "main"-type
    HTTP_STREAM_JOB too and can appear anywhere in log order relative to
    the actual document -- confirmed by inspecting a real capture (see
    phases.md Phase 4b), where a trailing google-analytics.com beacon
    landed after the document's own QUIC job and would have been
    misread as the page's own protocol if jobs weren't filtered to the
    navigated host specifically.

    Returns a dict with:
      netlog_using_quic: bool | None (None if no matching main stream job found)
      netlog_ok: bool (parse succeeded, at least one main job found)
      js_says_h3: bool | None
      agrees: bool (netlog and JS-level signals agree on h3-vs-not)
      matches_expected: bool (the agreed-upon protocol matches what was forced)
      verified: bool (agrees AND matches_expected -- the only trials safe to keep)
      discard_reason: str | None
    """
    path = Path(netlog_path)
    result = {
        "netlog_using_quic": None, "netlog_ok": False,
        "js_says_h3": None, "agrees": False,
        "matches_expected": False, "verified": False,
        "discard_reason": None,
    }

    if not path.exists() or path.stat().st_size == 0:
        result["discard_reason"] = "netlog_missing_or_empty"
        return result

    try:
        netlog = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        result["discard_reason"] = f"netlog_unparseable:{type(e).__name__}"
        return result

    type_map = _event_type_map(netlog)
    expected_destination = f"https://{navigated_host}"
    main_jobs = [
        e["params"] for e in netlog.get("events", [])
        if type_map.get(e.get("type")) == "HTTP_STREAM_JOB"
        and e.get("params", {}).get("type") == "main"
        and e.get("params", {}).get("destination") == expected_destination
    ]
    if not main_jobs:
        result["discard_reason"] = "no_main_stream_job_for_navigated_host"
        return result

    # First matching job is the navigation request itself -- same-origin
    # subresources issue their own later "main" jobs to the same
    # destination and would otherwise be indistinguishable from it, but
    # the document's own connection is always established first.
    result["netlog_using_quic"] = bool(main_jobs[0].get("using_quic"))
    result["netlog_ok"] = True

    if next_hop_protocol is not None:
        result["js_says_h3"] = next_hop_protocol == "h3"
        result["agrees"] = result["js_says_h3"] == result["netlog_using_quic"]
    else:
        result["discard_reason"] = "no_next_hop_protocol_from_js"
        return result

    actual_is_h3 = result["netlog_using_quic"]  # trust netlog as ground truth once agreement is confirmed
    result["matches_expected"] = actual_is_h3 == (expected_protocol == "h3")
    result["verified"] = result["agrees"] and result["matches_expected"]

    if not result["agrees"]:
        result["discard_reason"] = "netlog_js_disagree"
    elif not result["matches_expected"]:
        result["discard_reason"] = "protocol_fallback_from_forced_setting"

    return result
