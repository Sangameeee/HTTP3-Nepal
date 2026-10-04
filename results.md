# Results (as of 2026-08-08, Phases 1-5)

This is a results-focused summary, organized by research question, for
quick reference and as a starting point for the paper's Results section.
For the *process* — what was built, bugs found and fixed, decisions made
and why, and the full list of caveats behind each number — see
`phases.md`. Every number below is reproducible from the files cited, and
now also from a real pipeline: `analysis/rq1_adoption.py`,
`rq2_ownership.py`, `rq3_config.py`, `rq4_performance.py`,
`rq5_viability.py`, and `analysis/cross_run_variance.py` regenerate every
table in `output/tables/` (CSV + LaTeX) from `data/raw/`, and
`analysis/figures.py` regenerates the seven vector-PDF figures in
`output/figures/` — run them fresh any time rather than trusting numbers
transcribed by hand into this file.

**Status:** All 5 phases have been run at least once. **Phase 2 (active
probing) now has all 3 required runs on 3 different calendar days**
(`run-2026-08-05b`, `run-2026-08-07`, `run-2026-08-08`) — the brief's full
design ("run the full scan at least 3 times on different days; report the
union plus per-run variance") is satisfied. Per-run tables below are
primarily from the first run for detailed drill-down (RQ3's config-quality
numbers, RQ2's ASN classification narrative, etc.), with the 3-run union
and per-run variance now reported explicitly for RQ1 and RQ2 — the two
sections where cross-run stability actually matters most for the paper's
central claims. Phase 5 (manuscript) has a first full draft; see
`paper/`.

**Vantage point:** single machine, WorldLink Communications (AS17501),
Kathmandu — confirmed via live ASN lookup on the machine's own egress IP,
not assumed. The brief's design calls for additional vantage points
(second wired ISP, NTC/Ncell mobile, institutional network); none of those
are available in this session.

---

## Corpus (Phase 1)

Source: `corpus/domains.csv`, `corpus/corpus_summary.md`.

- **1,200 domains** total, DNS-validated live (public resolvers), sampled
  down from 3,434 candidates (curated sector lists + Certificate
  Transparency + Tranco top-1M) to the brief's 600–1,200 target via
  sector-stratified random sampling (seed 42).
- **659 domains fall into one of the six RQ1 sectors** after a keyword
  reclassification pass on the raw "unclassified" bucket; the rest (523
  unclassified + 18 confirmed NGO/personal/other) are excluded from
  RQ1–RQ5 analysis by default, kept in the corpus file for transparency.
- **Sector breakdown (confirmed, n=659):** education 288, government 250,
  banking 49, media 49, ecommerce_fintech 15, telecom 8.
- Telecom (n=8) and ecommerce_fintech (n=15) are thin; treat their
  percentages below as low-confidence / wide-interval, not point estimates.

---

## RQ1 — Adoption by sector

Source: `data/raw/run-2026-08-05b.jsonl`, apex hosts only (n=659, avoids
double-counting apex+`www` as two domains).

| Sector | HTTP/3-capable | Total | Adoption rate |
|---|---:|---:|---:|
| Media | 27 | 49 | **55.1%** |
| Education | 154 | 288 | **53.5%** |
| E-commerce/fintech | 7 | 15 | 46.7% (n=15, low confidence) |
| Banking | 11 | 49 | **22.4%** |
| Telecom | 1 | 8 | 12.5% (n=8, low confidence) |
| **Government** | **1** | **250** | **0.4%** |
| **All confirmed sectors** | **201** | **659** | **30.5%** |

**Headline finding:** government-sector adoption is essentially zero
(1/250), a ~130x gap versus education and media. This is the sharpest,
most publication-ready number in the dataset so far. The single positive
government case should be manually verified before the paper cites it
(which domain, and is its "government" sector label correct) — see
`phases.md`.

**Run-to-run variance and union, all 3 required runs (different calendar
days: 2026-08-05, 2026-08-07, 2026-08-08)**: computed by
`analysis/cross_run_variance.py`, which takes a genuine per-host OR
across all 3 runs' `h3_supported` values (not a naive file concatenation
— see that script's docstring for why the distinction matters). Every
sector except Education is byte-for-byte identical across all 3 runs.
Education creeps upward each run — 53.5% → 54.9% → 55.6%, union 55.9%
(161/288) — and the overall rate follows the same small upward drift:
30.5% → 31.1% → 31.4%, union 31.6% (208/659).

| Sector | Run 1 | Run 2 | Run 3 | 3-run union |
|---|---:|---:|---:|---:|
| Government | 0.4% | 0.4% | 0.4% | 0.4% |
| Banking | 22.4% | 22.4% | 22.4% | 22.4% |
| Telecom | 12.5% | 12.5% | 12.5% | 12.5% |
| Media | 55.1% | 55.1% | 55.1% | 55.1% |
| Education | 53.5% | 54.9% | 55.6% | 55.9% |
| E-commerce/Fintech | 46.7% | 46.7% | 46.7% | 46.7% |
| **All confirmed sectors** | **30.5%** | **31.1%** | **31.4%** | **31.6%** |

Across the full 1,318-host set, 409 hosts (31.0%) were HTTP/3-capable in
*at least one* of the 3 runs, but only 391 (29.7%) were capable in *all
three* — 18 hosts (1.4% of the corpus) are genuinely flaky, appearing
capable in some runs and not others. This is a small, second piece of
direct evidence (alongside the curl cross-validation clustering discussed
under RQ3) that a real subset of Nepali HTTP/3 deployments are marginal
rather than stably configured, not a probing-methodology artifact — the
same ~18-host-scale effect shows up independently in three different
places in this dataset now.

---

## RQ2 — Ownership (foreign CDN vs. origin-native)

Source: same file, `ownership_class` **re-derived from each host's raw ASN
numbers against the current `config/asn_classification.yaml`** (not the
value stored at probe time — see "unknown bucket" note below for why that
distinction matters here).

| Ownership class | Count | Share |
|---|---:|---:|
| Foreign CDN | 103 | **51.2%** |
| Foreign hosting (non-CDN) | 76 | **37.8%** |
| Nepali hosting | 22 | **10.9%** |
| Unknown | 0 | 0% |

**Headline finding:** over half of Nepal's HTTP/3-capable web services owe
that capability to a foreign CDN, and foreign CDN + foreign hosting
together account for **89.0%** — barely more than one in ten HTTP/3-capable
Nepali web services is actually served from domestic infrastructure. This
is the paper's central claim, directly and now fully supported (no
unresolved "unknown" residual left sitting in the denominator).

**CDN concentration (brief Correctness requirement 4 — "if 90% of the
H3-capable set is one CDN, state this plainly"): 101 of the 103
foreign-CDN hosts (98.1%) are Cloudflare specifically** — only 2 are
Sucuri, no other CDN operator appears at all. As a share of *all 201*
HTTP/3-capable hosts, Cloudflare alone accounts for 50.2% — essentially
half of Nepal's entire measured HTTP/3 adoption is one company's edge
network. **This must be stated plainly in the paper's Discussion, exactly
as the brief requires**: any Phase 4 performance comparison built from this
corpus will overwhelmingly describe Cloudflare's global edge, not Nepali
infrastructure, and that caveat has to travel with every RQ4 number, not
just live in a footnote here.

**"Unknown" bucket — how it went from 27.4% to 0%, and why that matters
methodologically:** the first pass showed 55 hosts (27.4%) as
unclassified. Rather than treat that as final, the underlying ASNs were
inspected directly (`data/raw/run-2026-08-05b.jsonl` stores the raw ASN
number and name per host, independent of classification) — all 55 turned
out to be legitimate, identifiable Western hosting providers (WHG Hosting
Services across 6 regional ASNs, Leaseweb, Interserver, PrivateSystems,
LiquidNet, FortressITX) simply missing from the config file, not genuinely
ambiguous cases. Added all of them as `foreign_hosting` and re-derived
`ownership_class` from the stored raw ASNs — this is *why* `ownership_class`
is treated as "classification at probe time, not final": the config file
can improve after the fact and every past run's data benefits without
re-probing. Confirmed here as a working pattern, not just a design note.

**Run-to-run variance and union, all 3 runs**:

| Ownership class | Run 1 | Run 2 | Run 3 | 3-run union |
|---|---:|---:|---:|---:|
| Foreign CDN | 103 (51.2%) | 103 (50.2%) | 103 (49.8%) | 103 (49.5%) |
| Foreign hosting (non-CDN) | 76 (37.8%) | 75 (36.6%) | 77 (37.2%) | 78 (37.5%) |
| Nepali hosting | 22 (10.9%) | 27 (13.2%) | 27 (13.0%) | 27 (13.0%) |
| Unknown | 0 | 0 | 0 | 0 |

The **Foreign CDN host count is exactly 103 in all 3 runs** — Cloudflare's
footprint in this corpus is completely stable; only its *share* drifts
slightly because the denominator (total HTTP/3-capable hosts) grows
across runs as flaky hosts occasionally register capable. Nepali hosting
stabilized at 27 from run 2 onward, up from 22 in run 1 (the same
`.edu.np`-driven effect as RQ1, since several of those domains sit on
Nepali institutional ASNs). The central claim — foreign CDN + foreign
hosting together dominate — holds in every run and the union alike
(89.0%, 86.8%, 87.0%, 87.0% respectively). **For the paper, report Nepali
hosting as 27/208 (13.0%) using the 3-run union**, not the first run's
22/201 (10.9%) — the union is the more defensible number now that all 3
runs exist, precisely because the brief asked for it instead of a single
run's point estimate.

---

## RQ3 — Configuration quality

Source: same file, restricted to the 394 apex+`www` hosts with
`h3_supported=true` (not just apex, since this is about configuration
quality of whichever hosts actually support HTTP/3).

**QUIC version negotiated:**

| Version | Count | Share |
|---|---:|---:|
| v1 (RFC 9000, `0x00000001`) | 203 | 51.5% |
| v2 (RFC 9369, `0x6b3343cf`) | 191 | 48.5% |

Near-even v1/v2 split — v2 adoption is notably high for a newer standard;
plausibly explained by the heavy foreign-CDN presence (RQ2), since major
CDN operators have been early v2 adopters.

**TLS 1.3 key-exchange group:**

| Group | Count | Share |
|---|---:|---:|
| x25519 | 391 | 99.2% |
| secp256r1 | 3 | 0.8% |

**Caveat, repeating from `phases.md` because it directly bears on how this
table should be read:** this probe's TLS client (aioquic) only ever offers
classical groups in its ClientHello. **Hybrid post-quantum groups (e.g.
X25519MLKEM768) are structurally impossible for this tool to observe** — a
server can only negotiate a group the client offered. This table describes
"the best classical group aioquic and the server have in common," not "the
group the server would prefer if asked." **RQ3's PQ-hybrid-group question
cannot be answered from this data**, full stop, not just "shows near-zero."

**Certificate issuers** (top 5 of 394):

| Issuer | Count | Share |
|---|---:|---:|
| Google Trust Services (WE1) | 180 | 45.7% |
| Let's Encrypt (YR2) | 90 | 22.8% |
| Let's Encrypt (YR1) | 79 | 20.1% |
| Let's Encrypt (YE2) | 24 | 6.1% |
| Let's Encrypt (YE1) | 15 | 3.8% |

Google Trust Services + Let's Encrypt together account for 98.5% of certs
among HTTP/3-capable hosts — consistent with the CDN-heavy, automated-cert
picture RQ2 already suggests.

**0-RTT:** attempted on 392 of 394 h3-capable hosts, **accepted on only 29
(7.4%)**. Most HTTP/3-capable origins in this corpus do not enable 0-RTT,
even though 1-RTT works — worth its own line in the paper (matches the
validation-phase finding that nginx declined 0-RTT while Cloudflare
accepted it).

**HTTPS/SVCB record (RR type 65) presence:** 207 of 1,318 hosts (15.7%),
199 of those advertising `alpn=h3`. The brief predicted "near-total
absence" for this record type in Nepal — **the actual figure (15.7%) is
higher than that prior expectation**, worth flagging as a finding that
refines rather than confirms the brief's a priori hypothesis, not silently
matched to it.

**Advertised vs. actual mismatch** (brief Phase 2 item 3): 59 of 1,318
hosts (4.5%) show a mismatch between what they advertise (Alt-Svc or
HTTPS-RR) and what actually works:
- 49 advertise HTTP/3 but it doesn't actually work (stale/broken config)
- 10 support HTTP/3 without advertising it at all (silently capable)

---

## RQ4 — Performance

**Phase 4a (handshake timing) and Phase 4b (page-load timing) both
complete.** Source: `data/raw/handshake_bench_run-2026-08-06.jsonl`, 185
same-origin H2+H3 domains (one host per domain, apex preferred), 30
interleaved trials each (H3/H2/H3/H2…), 5,550 total connection attempts.

**Success rate**: H2 99.7% (2768/2775), H3 99.3% (2756/2775) — both high.
H2's few failures were all timeouts (7); H3's were all connection errors
(19), concentrated in 4 domains.

**Handshake duration** (successful trials, first packet → handshake
complete):

| Protocol | n | Median | IQR | Mean | Std |
|---|---|---|---|---|---|
| HTTP/3 (QUIC) | 2756 | 54.0 ms | 36.9–206.7 ms | 163.6 ms | 198.2 ms |
| HTTP/2 (TCP+TLS) | 2768 | 33.0 ms | 25.4–220.3 ms | 189.1 ms | 382.3 ms |

Mann-Whitney U (two-sided): p = 2.3e-73 — significant, though at n>2700
per group that's expected even for a modest effect; **the 33ms vs. 54ms
median gap is the number that matters, not the p-value alone.**

**Headline finding, and it cuts against the "QUIC is faster" prior**: at
the median, plain TCP+TLS (H2) completes its handshake faster than QUIC
(H3) on this vantage point's path to these same-origin hosts. Per-domain,
H2 has the lower median at 144/185 domains vs. H3 at 41/185 — this isn't
a pooled-trial artifact from a few high-volume domains. Plausible
explanation (not yet confirmed): every trial here is a fresh connection
with no 0-RTT resumption, so QUIC isn't getting its usual 1-RTT-vs-2-RTT
edge over TCP+TLS 1.3's already-1-RTT handshake, and several of these
origins run less mature/less-optimized QUIC stacks (consistent with
RQ3's config-quality findings). At the tail (IQR high ~207–220ms, both
protocols) the gap mostly disappears.

**Read this as "handshake only," not "H3 is slower for users here"** —
the full page-load result below is what actually establishes user-facing
impact.

### Phase 4b — full page-load timing

Source: `data/raw/pageload_bench_run-2026-08-07.jsonl`, a random 50-domain
subsample (seed 42, reproducible) of the 185 Phase 4a domains — chosen
after measuring, not guessing, that full page loads pull ~65–106
subresource requests each, making the full 185-domain scope run to
~300k+ requests and ~3.5 hours; 50 domains stayed above the config's own
`min_qualifying_domains: 40` floor while keeping the brief's "≥30
trials/domain" requirement intact. 30 interleaved trials each via
Playwright + Chromium (CDP), one fresh browser process per trial (no
shared profile/connection pooling across trials), protocol forced via
Chromium flags (`--origin-to-force-quic-on` / `--disable-http3`).

**Mandatory netlog verification is the headline finding here, not a
footnote.** Every trial's *actual* wire protocol was independently
checked against Chromium's own `--log-net-log` capture, not just trusted
from the JS-visible `nextHopProtocol`. Result: **H2 verified at 99.6%
(747/750) — H3 verified at only 69.7% (523/750).** The other 30% of H3
trials split between silently falling back to H2 despite being forced to
QUIC (127 trials — the page loaded fine, just not over HTTP/3) and
outright navigation failures (100 trials: 86 generic errors, 14
timeouts). A naive benchmark that only checked "did the page load"
would have quietly averaged those 127 fallback trials into H3's numbers
as if QUIC had been used — netlog verification is what catches that.

**Timing** (netlog-verified trials only, n=523 H3 / 747 H2):

| Metric | HTTP/3 median | HTTP/2 median |
|---|---|---|
| First contentful paint | 614 ms | 772 ms |
| DOMContentLoaded | 713 ms | 851 ms |
| Full load event | 1,865.8 ms | 1,821.0 ms |

H3 is faster to first paint and DOMContentLoaded, but the two protocols
are statistically indistinguishable on full page-load time
(Mann-Whitney U, p=0.195 — not significant at α=0.05). Per-domain, H3 has
the lower median load-event time at 31/50 domains vs. H2 at 10/50 (9
domains had no verified H3 trials at all, mostly the fallback/error
cluster above).

**Putting 4a and 4b together**: the handshake-level result (H2 faster)
and the page-load-level result (H3 faster to first paint, tied overall)
aren't contradictory — a full page load is dominated by subresource
fetching and rendering, not the initial handshake. The more
consequential, more publishable finding isn't a speed difference at
all — **it's reliability**: HTTP/3 page loads on these Nepali/Nepal-
serving origins fail or silently fall back to HTTP/2 roughly 30% of the
time in this sample, which matters far more for real users than a
20–150ms timing delta either direction.

**Limitations carried forward**: single vantage point (wired_isp_a,
WorldLink) — no cross-ISP or wired-vs-mobile comparison, same limitation
as Phase 2/3/4a. 50-domain subsample, not the full 185 — a scope decision
made explicitly after measuring real per-trial cost, not a silent
shortcut (see phases.md Phase 4b).

---

## RQ5 — Path viability

Source: `data/raw/viability_run-2026-08-05b.jsonl`, 8 reference servers
across 5 operators (Cloudflare, AWS-hosted nginx, Google, Meta, LiteSpeed
x2, aioquic project).

**Handshake completion: 8/8** reference servers, cleanly (25–420 ms). No
evidence of blanket UDP/443 blocking from this vantage point.

**MTU/PMTUD: 5/8 clean at all sizes tested (1200–1500 bytes); 3/8 show
size-dependent failure**, clustered right at the 1450–1500-byte boundary:

| Server | Fails at (bytes) |
|---|---|
| www.google.com | 1500 |
| www.facebook.com | 1450, 1500 |
| http3.is | 1500 |

**Idle-survival** (30s / 60s / 120s idle, then a follow-up request on the
same connection):

| Server | 30s | 60s | 120s |
|---|:---:|:---:|:---:|
| cloudflare-quic.com | ✓ | ✗ | ✗ |
| www.cloudflare.com | ✓ | ✗ | ✗ |
| www.litespeedtech.com | ✓ | ✗ | ✗ |
| quic.nginx.org | ✓ | ✓ | ✗ |
| www.facebook.com | ✓ | ✓ | ✗ |
| quic.aiortc.org | ✓ | ✓ | ✗ |
| www.google.com | ✓ | ✓ | ✓ |
| http3.is | ✗ | ✗ | ✗ |

**Read this carefully, not as a NAT-blocking headline:** the pattern
clusters by *operator* (both Cloudflare properties fail identically at
60s; Google survives to 120s) rather than uniformly across every
destination. A client-side NAT-rebinding effect would be expected to
affect every destination similarly, regardless of which server is on the
other end — it doesn't. **The more parsimonious explanation is that this
measures each server's own QUIC max_idle_timeout policy, not a Nepal-side
network effect.** This test cannot cleanly distinguish the two without a
genuine client-address-migration test, which was judged out of scope for
this pass (see `phases.md`). Report this as "idle-survival varies by
operator, consistent with server-side timeout policy" — not as evidence of
UDP path blocking, which the data doesn't support.

**Throughput (QUIC vs. TCP/H2, each server's own homepage):** QUIC faster
on all 7 servers with a valid comparison, by 1.2x–3.9x. Caveat: payload
sizes ranged from ~0.6 KB to ~1.3 MB across servers (a page-size fetch, not
a controlled bulk-transfer benchmark) — the originally-planned dedicated
speed-test target (speed.cloudflare.com) turned out not to support HTTP/3
at all, itself confirmed independently two ways.

---

## Open items before any of this is submission-ready

1. Additional vantage points (second wired ISP, mobile, institutional) —
   needs the operator's own network access; not achievable from this
   single-machine session.
2. `paper/content.py`'s prose (especially the Discussion section's causal
   explanations) has only been reviewed for factual/numeric accuracy
   against this file, not read closely as prose by anyone but the agent
   that wrote it.

**Done since the first version of this file:**
- ~~Per-CDN breakdown of foreign_cdn hosts~~ — done: 98.1% Cloudflare (see RQ2).
- ~~Manual verification of the single government-sector HTTP/3 case~~ —
  done: `nphl.gov.np`, National Public Health Laboratory, confirmed live
  and legitimate (genuinely Nepali-hosted, on Access World Tech, AS59370 —
  a real example of origin-native government HTTP/3, worth naming in the
  paper alongside the 0.4% headline rate).
- ~~Second pass shrinking the RQ2 "unknown" bucket~~ — done: 27.4% → 0%,
  see RQ2's "unknown bucket" note for how and why.
- ~~Two more Phase 2 runs, on different days~~ — done: all 3 required runs
  complete (2026-08-05, -07, -08); union + per-run variance now reported
  in RQ1/RQ2 above via `analysis/cross_run_variance.py`.
- ~~Phases 4 and 5~~ — done: Phase 4 (handshake + page-load performance)
  and Phase 5 (manuscript, `paper/manuscript_full.docx` /
  `manuscript_blinded.docx`) both complete.
