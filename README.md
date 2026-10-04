# quic-nepal

Measurement pipeline for the paper *"Who Actually Serves HTTP/3 in Nepal?
Disentangling CDN-Provided and Origin-Native QUIC Deployment."*

Single author, single machine (Apple Silicon Mac, macOS, 16 GB RAM), all
measurements taken from Kathmandu. There is no lab hardware, no cloud budget,
and no testbed — every design choice below exists to make that machine
sufficient and the results defensible under peer review anyway.

Status: **Phases 1-5 have all been run at least once.** Phase 2 (active
probing) has 2 of the 3 required runs on different calendar days; a third is
still needed. Everything else — corpus, UDP viability, handshake and
page-load performance, and a first full manuscript draft — is complete for
this session. **See `phases.md` for a running log of what was done in each
phase, what worked, what failed, and which numbers are shaky** — updated at
the end of every phase — and `results.md` for the current numbers organized
by research question.

## Repository layout

```
quic-nepal/
├── config/
│   ├── settings.yaml            # every tunable: timeouts, concurrency, seed, vantage points
│   └── asn_classification.yaml  # ASN -> ownership class mapping (RQ2 artifact)
├── corpus/
│   ├── build_corpus.py          # Phase 1 (implemented)
│   ├── sources/                 # curated per-sector seed lists, with provenance notes
│   ├── cache/                   # cached crt.sh / Tranco responses (gitignored)
│   ├── domains.csv              # OUTPUT: the corpus
│   ├── rejected.csv             # OUTPUT: candidates that didn't resolve, with reason
│   ├── corpus_summary.md        # OUTPUT: human-readable summary table
│   └── build_log.jsonl          # OUTPUT: append-only log of every build step
├── probe/    viability/    perf/    analysis/    # Phases 2-4 (not yet implemented)
├── data/{raw,processed}/                          # Phase 2+ output (not yet populated)
└── output/{figures,tables}/                       # Phase 5 output (not yet populated)
```

## Setup

Requires Python 3.11+. This repo was built and run with Python 3.12 managed
via [`uv`](https://docs.astral.sh/uv/):

```bash
cd quic-nepal
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

Phase 1 (corpus construction) needs only the base dependency set above —
`httpx`, `dnspython`, `pyyaml`, `pandas`, `tldextract`, `beautifulsoup4` — all
pure-Python or prebuilt-wheel packages, no compiler required.

Phases 2+ will need the `probe` extra (`aioquic`), `perf` extra
(`playwright`), and `paper` extra (`python-docx`) respectively:

```bash
uv pip install --python .venv/bin/python -e ".[probe,perf,paper]"
```

**`aioquic` and `playwright` were not installed as part of this delivery.**
Per the brief, I ask before adding a dependency that needs compilation —
`aioquic` builds against a bundled OpenSSL/BoringSSL-derived native module,
so this needs your explicit go-ahead before Phase 2 begins.

`playwright` itself is a pure-wheel install, but it downloads a full Chromium
binary (~150-300 MB) on first `playwright install`; flagging that download
before it happens rather than triggering it silently in Phase 4.

Homebrew curl with HTTP/3 support is required from Phase 2 onward for
cross-validation:

```bash
brew install curl
/opt/homebrew/opt/curl/bin/curl -V | grep HTTP3
```

The system `/usr/bin/curl` on this machine (LibreSSL-linked, no HTTP/3) is
never used for measurement — only Homebrew curl at the path above, per
`config/settings.yaml: probe.curl_path`.

## Running Phase 1 (corpus construction)

```bash
.venv/bin/python corpus/build_corpus.py
```

Flags:
- `--skip-ct` — skip crt.sh Certificate Transparency enumeration
- `--skip-gov-crawl` — skip the bounded crawl of nepal.gov.np
- `--skip-tranco` — skip the Tranco top-1M cross-check
- `--skip-dns-validation` — keep candidates that don't resolve (debug only;
  never use this for a corpus that will be probed or published)

Each source is independently resumable via its on-disk cache
(`corpus/cache/crtsh/<suffix>.json`, `corpus/cache/tranco/top-1m.csv.zip`):
delete the relevant cache file to force a fresh fetch, or re-run with the
corresponding `--skip-*` flag to reuse curated seeds only.

**Network request budget.** A single build run is capped at
`config/settings.yaml: corpus.max_network_requests` (default 2000, matching
the operator's "ask before >2000 requests in a run" instruction). The build
aborts with a `RuntimeError` rather than silently exceeding it — if you hit
this, it means Certificate Transparency returned far more candidate
hostnames than expected (this has happened for `%.com.np` in testing) and the
DNS-validation pass alone would blow the budget; raise the ceiling only after
explicit sign-off.

**A note on crt.sh reliability.** crt.sh is a free, shared community
resource with no SLA. During development it returned `502 Bad Gateway` and
outright connection timeouts on a majority of attempts in a single session.
`build_corpus.py` retries each suffix up to `corpus.ct.attempts` times with
backoff, caches any successful response to disk, and degrades gracefully
(logs a warning, contributes zero hostnames for that suffix) rather than
failing the whole build. **If a run's `corpus_summary.md` shows
`crtsh_query_failed` for a suffix, re-run later** — CT enumeration is the
brief's stated primary path for `.gov.np` (the registry publishes no zone
file), so a degraded CT run measurably shrinks the government-sector sample
and should be retried before that sample is treated as final.

