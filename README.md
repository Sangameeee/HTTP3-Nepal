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

## Corpus construction methodology (for the paper's Methods section)

Four independent sources feed the corpus, merged and deduplicated by
*registrable domain* (via `tldextract`, which correctly treats `gov.np`,
`com.np`, etc. as public suffixes rather than as part of the domain):

1. **Curated per-sector seed lists** (`corpus/sources/*.yaml`) — hand-compiled
   from the sector-specific authorities named in the brief (NRB licensed BFI
   classes, NTA operator rankings, UGC's university list, Press Council-style
   outlet lists, NIA insurer lists). **Provenance is recorded per file, per
   entry group**, and is not uniformly strong — see "Corpus confidence" below.
2. **Certificate Transparency** (`crt.sh`, `%.gov.np` / `%.edu.np` /
   `%.org.np` / `%.com.np` / `%.net.np` / `%.mil.np`) — the primary
   enumeration path for `.np`, since the registry publishes no zone file.
3. **A bounded, robots.txt-respecting crawl** of `nepal.gov.np` (max 25
   pages) for linked `*.gov.np` subdomains not already surfaced by CT.
4. **Tranco top-1M cross-check** — filtered for `.np` TLDs (sector inferred
   from suffix where unambiguous, else `unclassified`) and for non-`.np`
   domains that were *already* found by one of the other three sources (used
   only to confirm popularity ranking, never to introduce a domain outright).

Source priority on merge: curated > CT > gov crawl > Tranco. A domain found
by multiple sources keeps the source that discovered it with the most
context (a human curator assigning a sector beats a suffix-pattern match).

**Every candidate must resolve** (A, AAAA, or CNAME) against at least one of
three public resolvers (1.1.1.1, 8.8.8.8, 9.9.9.9 — deliberately *not* the
local ISP resolver, so corpus membership doesn't depend on which network the
build happened to run on) before it enters `domains.csv`. Non-resolving
candidates are never silently dropped — they're written to `rejected.csv`
with a specific reason (`nxdomain`, `timeout`, `error:<type>`).

**Apex vs. `www` are intentionally *not* both stored as separate corpus
rows.** `domains.csv` holds one row per registrable domain. Phase 2's
`scan.py` is responsible for expanding each row into an apex-host probe and a
`www`-host probe, because that is where the apex/`www` split actually
matters (probing), and collapsing it here would double-count every domain in
every corpus-level statistic (sector counts, TLD counts) for no benefit.

### Corpus confidence — read before trusting a sector's numbers

This corpus was built from a language model's training-data recall of
Nepali institutional names and domains, **partially corroborated** against
live web search during construction (2026-08-05) and **fully gated** by live
DNS resolution. That combination means:

- **High confidence:** which domains ended up in `domains.csv` — every one
  of them resolves, right now, from a public resolver. This is not a list of
  guesses; it's a list of guesses that survived a real DNS lookup.
- **Medium confidence:** sector labels for CT/crawl/Tranco-derived domains
  under `gov.np` and `edu.np` (suffix is a strong sector signal) or already
  on a curated list.
- **Lower confidence, flagged explicitly in `corpus/sources/*.yaml`:**
  sector labels and domain-name guesses for **Class A commercial banks and
  all insurers** (Wikipedia and Investopaper confirmed the *institution
  names* but not their domains — those domains are recalled, not
  corroborated) and for microfinance institutions and most curated
  government ministry subdomains. A wrong-but-*resolving* guess (e.g. a
  parked or unrelated domain that happens to share a plausible name) would
  **not** be caught by DNS validation. **Before treating the banking or
  insurance sector counts as final, spot-check `corpus/sources/banking_finance.yaml`
  against nrb.org.np and nia.gov.np directly** — this is flagged, not fixed,
  because verifying ~50 institutional domains against primary sources is
  exactly the kind of check that belongs to the author of record, not to an
  automated build step.
- Sector completeness is bounded by CT/crawl coverage on the day the build
  ran. crt.sh's reliability issues (see above) mean a given run's government
  and education counts may undercount until CT is retried on a healthier day.

## Sector reclassification pass and probing scope

The raw build left 579/1200 domains (48%) labeled `unclassified` -- CT-derived
`org.np`/`com.np`/`net.np` hits and Tranco `.np` entries with no suffix-based
sector signal. Manual inspection showed this bucket is dominated by
personal-name domains (e.g. `ananda-subedi.com.np`) and small NGOs, not
mis-sectored businesses. `corpus/classify_sectors.py` (keyword rules in
`config/sector_keywords.yaml`) reclassified 56 of those 579:

- 38 into one of the six RQ1 sectors (a bank, outlet, or shop that happened
  to use a bare `.com.np`/`.org.np` and was missed by suffix-only logic)
- 18 into `other_not_in_scope` (NGOs, clubs, religious/sporting bodies --
  *confirmed* not one of the six sectors, which is a different claim than
  "unclassified")
- 523 remain genuinely `unclassified` -- no keyword matched. This is expected,
  not a bug: personal domains don't carry a sector signal in the hostname,
  and no further automated pass will meaningfully shrink this without a
  WHOIS/organization lookup that doesn't exist for `.np`.

Every reclassification decision is logged, domain by domain, in
`corpus/classification_log.csv` for audit.

**Post-classification sector counts:** government 250, education 288,
banking 49, media 49, ecommerce_fintech 15, telecom 8, other_not_in_scope 18,
unclassified 523. The six confirmed RQ1 sectors sum to **659 domains**, which
on its own clears the brief's 600-domain floor.

**Decision (2026-08-05): Phase 2 active probing and the RQ1-RQ5 analysis
default to the 659-domain confirmed-sector subset**, not the full 1200-row
`domains.csv`. Rationale: the study is about "Nepal's significant web
services," not every live `.np` registration CT happens to surface;
personal blogs and small clubs are neither, and actively probing 523+18 of
them would spend request budget and raise the ethical footprint (Phase 2
sends real QUIC/TLS handshakes to every domain it probes) for no RQ this
paper asks. `domains.csv` keeps all 1200 rows for transparency and
reproducibility; `probe/scan.py` (Phase 2, not yet built) should filter to
`sector not in ("unclassified", "other_not_in_scope")` by default, with an
explicit flag to widen scope if that judgment call should be revisited.

## Ethics (corpus construction)

- User-Agent on every request identifies the study and a contact email
  (`config/settings.yaml: study.user_agent`).
- The `nepal.gov.np` crawl checks `robots.txt` before fetching anything and
  fails closed (crawls nothing) if `robots.txt` can't be fetched or parsed.
- No authentication, fuzzing, or non-idempotent requests anywhere in corpus
  construction — GET requests to crt.sh's public API, the gov portal's public
  pages, and Tranco's public CSV snapshot only.
- Corpus construction's own network footprint is small (curated-list load +
  CT queries + bounded crawl + one Tranco download); the request-budget guard
  described above exists mainly to protect **Phase 2's** much larger request
  volume, but is enforced uniformly.

## Manuscript (Phase 5)

`paper/manuscript_full.docx` and `paper/manuscript_blinded.docx` are
generated from a single source of truth, `paper/content.py`, by
`paper/build_manuscript.py` (run it again any time results.md's numbers
change — the two .docx files cannot drift apart from each other since
they're built from the same content, but they can drift from results.md
if it's updated without a matching content.py edit). `paper/verify_format.py`
checks the rendered files against KJSE's Author Guidelines (fonts, heading
case/size, no numbered headings, abstract/keyword word counts, reference
order, and — for the blinded file — no residual author-identifying text or
metadata); it currently passes clean on both files.

**Two formatting conflicts in KJSE's own published guidelines, surfaced
here rather than silently resolved — please confirm the intended reading
with the editor (`okrp@kecktm.edu.np`) before final submission:**

1. **Font size.** The detailed formatting section specifies Times New Roman
   10 pt with 1 pt paragraph spacing; the separate Submission Preparation
   Checklist specifies 12 pt. Both manuscript files are built at **10 pt**,
   matching the more specific, detailed instruction (which also matches the
   guidelines' stated heading sizes) — but this is a real contradiction in
   the source document, not a judgment call this pipeline should make
   unilaterally.
2. **Table/figure placement.** The "Document formatting" section says
   tables and figures go inline at the point they're discussed, "not
   collected at the end"; the separate "Mandated section order" list
   enumerates Tables, Figures, and Legends as their own sections *after*
   References. Both manuscript files place tables and figures **inline**,
   matching the more specific, explicit instruction — but many journals
   genuinely do want tables/figures collected at the end for blind review
   and moved inline only at production, so this is worth a direct
   confirmation rather than assuming the first reading is the intended one.

## Remaining work

- **Phase 2**: complete — all 3 required full scans on 3 different
  calendar days (`run-2026-08-05b`, `run-2026-08-07`, `run-2026-08-08`).
  Union + per-run variance computed by `analysis/cross_run_variance.py`
  and reported in `results.md`'s RQ1/RQ2 sections and in the manuscript.
- **Additional vantage points** (second wired ISP, mobile carriers,
  institutional network) — needs the operator's own additional network
  access; not achievable from this single-machine session.
- **Manuscript content review**: `paper/content.py` was drafted from and
  kept in sync with `results.md`'s numbers (last synced 2026-08-08, after
  the 3rd Phase 2 run) and passes automated format checks, but the prose
  itself — the Discussion's causal explanations in particular — has not
  been reviewed by anyone but the agent that wrote it, and should be read
  closely before submission.
