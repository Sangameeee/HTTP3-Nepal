#!/usr/bin/env python3
"""Phase 4b -- full page-load timing, H3 (QUIC) vs H2, same origin.

Uses Playwright + Chromium (CDP) rather than a bare HTTP client, per the
brief -- a bare client can't reproduce what a real browser actually does
(parallel subresource fetches, render-blocking CSS/JS, connection reuse
within a page). Complements Phase 4a's handshake-only measurement with
what a user actually experiences.

Protocol is forced at the Chromium-process level, not per-request:
  - H3:  --origin-to-force-quic-on=<host>:443
  - H2 baseline: --disable-http3
A **fresh browser process is launched per trial** (not a fresh page/tab in
a shared browser) -- this is deliberate and more expensive, but it is the
only way to guarantee no QUIC/TCP connection or session-resumption state
survives from one trial to the next, which the brief requires ("disable
connection pooling"). A shared profile across trials would let an H3
trial's warm QUIC session quietly speed up a later "fresh" H3 trial, or
let H2 connection reuse do the same -- either would bias the comparison.

**Mandatory netlog verification** (perf/netlog_verify.py): each trial
writes its own --log-net-log capture and the result is checked against
what actually happened on the wire, not just what was requested. Trials
where the forced protocol didn't actually take effect (e.g. an H3 trial
that silently fell back to H2 because the QUIC handshake failed) are
recorded but flagged unverified and excluded from the timing comparison
-- averaging them in would silently corrobort an H2 result as if it were
H3's number.

Timing captured via the Navigation Timing / Paint Timing APIs
(performance.getEntriesByType), read from the page after load -- not
Playwright's own request/response event timestamps, which measure
Playwright's IPC overhead as much as the page's.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW_DIR = REPO_ROOT / "data" / "raw"
sys.path.insert(0, str(REPO_ROOT))

from perf.handshake_bench import qualifying_domains, ping_rtt_ms  # noqa: E402
from perf.netlog_verify import verify_protocol  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("pageload_bench")

_NAV_TIMING_JS = """() => {
    const n = performance.getEntriesByType('navigation')[0];
    if (!n) return null;
    const paints = {};
    for (const p of performance.getEntriesByType('paint')) paints[p.name] = p.startTime;
    return {
        nextHopProtocol: n.nextHopProtocol,
        domContentLoaded_ms: n.domContentLoadedEventEnd,
        loadEvent_ms: n.loadEventEnd,
        domInteractive_ms: n.domInteractive,
        responseStart_ms: n.responseStart,
        requestStart_ms: n.requestStart,
        transferSize: n.transferSize,
        firstPaint_ms: paints['first-paint'] ?? null,
        firstContentfulPaint_ms: paints['first-contentful-paint'] ?? null,
    };
}"""


def load_settings() -> dict:
    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


async def run_trial(playwright, host: str, protocol: str, timeout_s: float, netlog_dir: Path) -> dict:
    netlog_path = netlog_dir / f"{host}_{protocol}_{datetime.now().strftime('%H%M%S%f')}.json"
    args = [f"--log-net-log={netlog_path}"]
    if protocol == "h3":
        args.append(f"--origin-to-force-quic-on={host}:443")
    else:
        args.append("--disable-http3")

    result = {
        "success": False, "failure_reason": None, "timing": None,
        "netlog_check": None, "request_count": None,
    }
    browser = None
    try:
        browser = await playwright.chromium.launch(headless=True, args=args, timeout=timeout_s * 1000)
        context = await browser.new_context(ignore_https_errors=False)
        page = await context.new_page()
        requests = []
        page.on("request", lambda req: requests.append(req))
        navigated_host = host
        try:
            await page.goto(f"https://{host}/", wait_until="load", timeout=timeout_s * 1000)
            result["success"] = True
            navigated_host = urlparse(page.url).hostname or host
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            result["failure_reason"] = "timeout" if "timeout" in msg else f"error:{type(e).__name__}"
        result["request_count"] = len(requests)
        result["navigated_host"] = navigated_host
        timing = None
        if result["success"]:
            try:
                timing = await page.evaluate(_NAV_TIMING_JS)
            except Exception:  # noqa: BLE001
                pass
        result["timing"] = timing
    except Exception as e:  # noqa: BLE001
        result["failure_reason"] = f"launch_error:{type(e).__name__}"
    finally:
        if browser is not None:
            await browser.close()

    next_hop = result["timing"]["nextHopProtocol"] if result["timing"] else None
    result["netlog_check"] = verify_protocol(netlog_path, protocol, next_hop, result["navigated_host"])
    netlog_path.unlink(missing_ok=True)  # keep only the verification verdict, not the (large) raw capture
    return result


async def run_domain_trials(playwright, host: str, n_trials: int, timeout_s: float,
                             vantage_point: str, netlog_dir: Path) -> list[dict]:
    records = []
    for i in range(n_trials):
        protocol = "h3" if i % 2 == 0 else "h2"
        rtt = ping_rtt_ms(host)
        trial = await run_trial(playwright, host, protocol, timeout_s, netlog_dir)
        records.append({
            "host": host, "trial_index": i, "protocol": protocol,
            "vantage_point": vantage_point, "baseline_rtt_ms": rtt,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            **trial,
        })
    return records


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--vantage-point", required=True)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--limit", type=int, default=None, help="only the first N qualifying hosts (smoke testing)")
    ap.add_argument("--sample-domains", type=int, default=None,
                     help="randomly subsample to N domains (config random_seed, reproducible) -- "
                          "for Phase 4b's reduced-domain scope: page loads are far more request-heavy "
                          "than Phase 4a's raw handshakes, so the full domain set is often not worth the "
                          "wall-clock/request-count cost (see phases.md Phase 4b)")
    ap.add_argument("--timeout-s", type=float, default=20.0, help="per-navigation timeout (page loads are slower than raw handshakes)")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    hosts = qualifying_domains()
    if args.sample_domains and args.sample_domains < len(hosts):
        cfg = load_settings()
        rng = random.Random(cfg["study"]["random_seed"])
        hosts = rng.sample(hosts, args.sample_domains)
        hosts.sort(key=lambda r: r["domain"])  # deterministic order for resumability/log readability
    if args.limit:
        hosts = hosts[: args.limit]
    log.info("Page-load benchmark: %d qualifying hosts, %d trials each (%d total page loads)",
              len(hosts), args.trials, len(hosts) * args.trials)

    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_RAW_DIR / f"pageload_bench_{args.run_id}.jsonl"

    with tempfile.TemporaryDirectory(prefix="netlog_") as tmpdir:
        netlog_dir = Path(tmpdir)
        async with async_playwright() as playwright:
            with open(out_path, "a", buffering=1) as f:
                for i, row in enumerate(hosts):
                    host = row["host"]
                    log.info("[%d/%d] %s", i + 1, len(hosts), host)
                    records = await run_domain_trials(playwright, host, args.trials, args.timeout_s,
                                                        args.vantage_point, netlog_dir)
                    n_verified = sum(1 for r in records if r["netlog_check"]["verified"])
                    log.info("  %s: %d/%d trials verified", host, n_verified, len(records))
                    for rec in records:
                        rec["run_id"] = args.run_id
                        rec["domain"] = row["domain"]
                        rec["host_type"] = row["host_type"]
                        f.write(json.dumps(rec) + "\n")
                        f.flush()

    log.info("=== Page-load benchmark complete: output %s ===", out_path)


if __name__ == "__main__":
    asyncio.run(main())
