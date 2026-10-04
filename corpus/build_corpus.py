#!/usr/bin/env python3
"""Phase 1 -- corpus construction.

Builds the domain corpus for "Who Actually Serves HTTP/3 in Nepal?" from:

  1. Curated per-sector seed lists (corpus/sources/*.yaml)          -> sector known
  2. Certificate Transparency enumeration via crt.sh (%.gov.np etc)  -> sector inferred from suffix
  3. A bounded crawl of nepal.gov.np for linked ministry subdomains  -> sector = government
  4. A Tranco top-1M cross-check for .np and known-Nepali domains    -> sector inferred where possible

Output: corpus/domains.csv (domain, sector, source, date_added, tld, in_curated_seed)
        corpus/rejected.csv (domain, reason, source)
        corpus/corpus_summary.md (human-readable summary table)
        corpus/build_log.jsonl (one record per build step, for the run log)

Design notes for reviewers:
  - Deduplication is by *registrable domain* (tldextract, using the bundled
    Public Suffix List, which correctly treats e.g. `gov.np` and `com.np` as
    suffixes). `www.example.com.np` and `example.com.np` collapse to one
    corpus row; Phase 2's scan.py is responsible for probing the apex and the
    `www` host *separately*, because CDN configuration is known to differ
    between them and collapsing that at the probing stage would hide a
    finding, not just reduce noise.
  - A domain only enters domains.csv if it resolves (A, AAAA, or CNAME) against
    at least one public resolver at build time. Non-resolving candidates are
    recorded in rejected.csv with the reason, never silently dropped.
  - Every row records its discovery `source` and `date_added`. A domain
    discovered by multiple sources keeps the first (highest-priority) source;
    curated lists > CT > gov crawl > Tranco, on the theory that a human who
    put a domain on a sector list did so with more confidence than a
    substring match against a suffix.
  - Sector assignment for CT/crawl/Tranco-derived domains that don't already
    appear on a curated list is done by suffix mapping only where that is
    unambiguous (gov.np -> government, edu.np -> education). Everything else
    is recorded as sector "unclassified" rather than guessed -- see the
    corpus summary's "unclassified" row. This is a documented limitation, not
    a bug: RQ1 needs sector labels, but false sector labels are worse than an
    honest "unclassified" bucket a reviewer can inspect.
"""
from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import logging
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import dns.exception
import dns.resolver
import tldextract
import yaml
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "corpus"
SOURCES_DIR = CORPUS_DIR / "sources"
CACHE_DIR = CORPUS_DIR / "cache"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("build_corpus")

# tldextract with a bundled/offline PSL snapshot so corpus membership doesn't
# depend on a live fetch of publicsuffix.org at build time (that fetch is one
# more thing that can silently degrade a "reproducible" pipeline).
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def registrable_domain(host: str) -> str | None:
    """Return the registrable domain (e.g. example.com.np) for a hostname."""
    host = host.strip().strip(".").lower()
    if not host or " " in host:
        return None
    ext = _TLD_EXTRACT(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def load_settings() -> dict:
    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        return yaml.safe_load(f)


@dataclass
class Candidate:
    domain: str  # registrable domain
    sector: str
    source: str
    discovered_host: str  # the exact hostname seen (may equal domain, may be a subdomain)


@dataclass
class BuildStats:
    requests_made: int = 0
    steps: list[dict] = field(default_factory=list)

    def log_step(self, name: str, **kw):
        rec = {"step": name, "ts": datetime.now(timezone.utc).isoformat(), **kw}
        self.steps.append(rec)
        log.info("[%s] %s", name, {k: v for k, v in kw.items()})


class RequestBudget:
    """Hard ceiling on network requests to source services (crt.sh, gov crawl).

    The operator brief requires sign-off before a run makes more than ~2000
    requests to real services. This budget aborts the build outright when
    exceeded, because it covers requests to a shared community resource
    (crt.sh) and to government target infrastructure (nepal.gov.np) -- the
    exact kind of traffic the operator asked to be asked about.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.count = 0

    def spend(self, n: int = 1):
        self.count += n
        if self.count > self.limit:
            raise RuntimeError(
                f"Request budget exceeded: {self.count} > {self.limit}. "
                "Aborting rather than silently making more network requests "
                "than the operator authorized. Raise config.corpus.max_network_requests "
                "only after explicit sign-off."
            )


class SoftBudget:
    """Ceiling on DNS-validation lookups against public resolvers.

    Unlike RequestBudget, this does not hard-abort the whole build: DNS
    lookups here go to 1.1.1.1 / 8.8.8.8 / 9.9.9.9, never to Nepali target
    infrastructure, so exceeding it is low-risk. If exceeded, validation
    stops early and every not-yet-validated candidate is recorded as such
    (never silently dropped) so a partial run still produces a usable,
    honestly-labeled corpus instead of losing all progress.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.count = 0

    def spend(self, n: int = 1) -> bool:
        """Returns False once the budget is exhausted (caller should stop)."""
        self.count += n
        return self.count <= self.limit


# --------------------------------------------------------------------------
# Source 1: curated per-sector seed lists
# --------------------------------------------------------------------------

def load_curated_sources() -> list[Candidate]:
    candidates = []
    for path in sorted(SOURCES_DIR.glob("*.yaml")):
        with open(path) as f:
            spec = yaml.safe_load(f)
        sector = spec["sector"]
        source = spec["source"]
        for host in spec["domains"]:
            dom = registrable_domain(host)
            if dom is None:
                log.warning("curated seed %r in %s did not parse as a domain", host, path.name)
                continue
            candidates.append(Candidate(domain=dom, sector=sector, source=source, discovered_host=host))
    return candidates


# --------------------------------------------------------------------------
# Source 2: Certificate Transparency via crt.sh
# --------------------------------------------------------------------------

SUFFIX_TO_SECTOR = {
    "gov.np": "government",
    "mil.np": "government",
    "edu.np": "education",
    "org.np": "unclassified",
    "com.np": "unclassified",
    "net.np": "unclassified",
}

_UA_TEMPLATE = "quic-nepal-research/0.1 (+mailto:{email}) academic corpus-building crawler"


def http_get(url: str, user_agent: str, timeout: float, budget: RequestBudget) -> bytes | None:
    budget.spend(1)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def crtsh_query(suffix: str, cfg: dict, user_agent: str, budget: RequestBudget, stats: BuildStats) -> set[str]:
    """Query crt.sh for '%.suffix' and return the set of hostnames seen.

    crt.sh is a shared community resource with no SLA; it is queried at most
    `attempts` times per suffix with a long per-attempt timeout, cached to
    disk so re-runs of this script don't hammer it, and the failure mode is
    "log a warning and move on" rather than "crash the whole corpus build."
    """
    cache_dir = REPO_ROOT / cfg["cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{suffix}.json"

    if cache_file.exists():
        age_h = (time.time() - cache_file.stat().st_mtime) / 3600
        log.info("crt.sh: using cached result for %s (age %.1fh)", suffix, age_h)
        raw = cache_file.read_bytes()
    else:
        raw = None
        url = f"{cfg['base_url']}?q=%25.{suffix}&output=json"
        for attempt in range(1, cfg["attempts"] + 1):
            raw = http_get(url, user_agent, cfg["timeout_s"], budget)
            if raw:
                try:
                    json.loads(raw)
                    break
                except json.JSONDecodeError:
                    log.warning("crt.sh: attempt %d/%d for %s returned non-JSON, retrying",
                                attempt, cfg["attempts"], suffix)
                    raw = None
            time.sleep(2 * attempt)
        if raw:
            cache_file.write_bytes(raw)

    if not raw:
        stats.log_step("crtsh_query_failed", suffix=suffix)
        return set()

    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        stats.log_step("crtsh_query_unparseable", suffix=suffix)
        return set()

    hosts: set[str] = set()
    for row in rows:
        name_value = row.get("name_value", "")
        for line in name_value.splitlines():
            h = line.strip().lstrip("*.").lower()
            if h.endswith("." + suffix) or h == suffix:
                hosts.add(h)
    stats.log_step("crtsh_query_ok", suffix=suffix, hosts_found=len(hosts), certs_seen=len(rows))
    return hosts


def crt_sh_candidates(cfg: dict, user_agent: str, budget: RequestBudget, stats: BuildStats) -> list[Candidate]:
    if not cfg["ct"]["enabled"]:
        return []
    out = []
    for suffix in cfg["ct"]["suffixes"]:
        hosts = crtsh_query(suffix, cfg["ct"], user_agent, budget, stats)
        sector = SUFFIX_TO_SECTOR.get(suffix, "unclassified")
        for h in hosts:
            dom = registrable_domain(h)
            if dom is None:
                continue
            out.append(Candidate(domain=dom, sector=sector, source=f"crtsh:%.{suffix}", discovered_host=h))
    return out


# --------------------------------------------------------------------------
# Source 3: bounded crawl of nepal.gov.np
# --------------------------------------------------------------------------

def gov_crawl_candidates(cfg: dict, user_agent: str, budget: RequestBudget, stats: BuildStats) -> list[Candidate]:
    gc = cfg["gov_crawl"]
    if not gc["enabled"]:
        return []

    seed = gc["seed"]
    parsed_seed = urlparse(seed)

    rp = urllib.robotparser.RobotFileParser()
    robots_url = f"{parsed_seed.scheme}://{parsed_seed.netloc}/robots.txt"
    try:
        raw = http_get(robots_url, user_agent, 10, budget)
        if raw is not None:
            rp.parse(raw.decode("utf-8", errors="replace").splitlines())
        else:
            rp = None  # couldn't fetch robots.txt; treat as "crawl nothing further" below
    except Exception as e:
        log.warning("robots.txt fetch/parse failed for %s: %s", robots_url, e)
        rp = None

    def allowed(url: str) -> bool:
        if rp is None:
            # Fail closed: if we can't confirm robots.txt permits it, don't crawl it.
            return False
        try:
            return rp.can_fetch(user_agent, url)
        except Exception:
            return False

    visited: set[str] = set()
    to_visit = [seed]
    found_hosts: set[str] = set()
    domain_link_re = re.compile(r"\.gov\.np$")

    if not allowed(seed):
        stats.log_step("gov_crawl_blocked_by_robots", seed=seed)
        return []

    while to_visit and len(visited) < gc["max_pages"]:
        url = to_visit.pop(0)
        if url in visited or not allowed(url):
            continue
        visited.add(url)
        raw = http_get(url, user_agent, 15, budget)
        if raw is None:
            continue
        try:
            soup = BeautifulSoup(raw, "html.parser")
        except Exception as e:
            log.warning("parse failed for %s: %s", url, e)
            continue
        for a in soup.find_all("a", href=True):
            href = a["href"]
            abs_url = urljoin(url, href)
            host = urlparse(abs_url).hostname
            if not host:
                continue
            host = host.lower()
            if domain_link_re.search(host):
                found_hosts.add(host)
                # Follow same-site links shallowly to find more subdomain links,
                # but never leave the .gov.np namespace.
                if abs_url not in visited and len(to_visit) + len(visited) < gc["max_pages"]:
                    to_visit.append(abs_url)

    stats.log_step("gov_crawl_done", pages_visited=len(visited), hosts_found=len(found_hosts))
    return [
        Candidate(domain=d, sector="government", source="gov_crawl:nepal.gov.np", discovered_host=h)
        for h in found_hosts
        if (d := registrable_domain(h)) is not None
    ]


# --------------------------------------------------------------------------
# Source 4: Tranco top-1M cross-check
# --------------------------------------------------------------------------

NEPALI_OPERATOR_KEYWORDS = [
    # Substring match against Tranco's non-.np entries, used only to surface
    # candidates for manual review -- never auto-added to domains.csv without
    # already being registrable-domain-equal to a curated/CT/crawl hit.
    "nepal", "kathmandu", "esewa", "khalti", "daraz", "ntc", "ncell",
]


def tranco_candidates(cfg: dict, user_agent: str, budget: RequestBudget, stats: BuildStats,
                       known_domains: set[str]) -> list[Candidate]:
    tc = cfg["tranco"]
    if not tc["enabled"]:
        return []
    cache_dir = REPO_ROOT / tc["cache_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / "top-1m.csv.zip"

    if not zip_path.exists():
        raw = http_get(tc["url"], user_agent, 120, budget)
        if raw is None:
            stats.log_step("tranco_fetch_failed")
            return []
        zip_path.write_bytes(raw)
    else:
        log.info("tranco: using cached snapshot at %s", zip_path)

    np_hits: list[Candidate] = []
    cross_confirmed: list[Candidate] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            inner_name = zf.namelist()[0]
            with zf.open(inner_name) as f:
                reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
                for rank, host in reader:
                    host = host.strip().lower()
                    dom = registrable_domain(host)
                    if dom is None:
                        continue
                    if dom.endswith(".np"):
                        sector = _sector_from_np_domain(dom)
                        np_hits.append(Candidate(domain=dom, sector=sector,
                                                  source="tranco:np_tld", discovered_host=host))
                    elif dom in known_domains:
                        cross_confirmed.append(Candidate(domain=dom, sector="unclassified",
                                                          source="tranco:cross_confirmed",
                                                          discovered_host=host))
    except zipfile.BadZipFile:
        stats.log_step("tranco_bad_zip")
        return []

    stats.log_step("tranco_done", np_hits=len(np_hits), cross_confirmed=len(cross_confirmed))
    return np_hits + cross_confirmed


def _sector_from_np_domain(dom: str) -> str:
    for suffix, sector in SUFFIX_TO_SECTOR.items():
        if dom.endswith("." + suffix) or dom == suffix:
            return sector
    return "unclassified"


# --------------------------------------------------------------------------
# DNS resolution validation
# --------------------------------------------------------------------------

def build_resolver(nameservers: list[str], timeout_s: float) -> dns.resolver.Resolver:
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = nameservers
    r.timeout = timeout_s
    r.lifetime = timeout_s
    return r


def resolves(host: str, resolver: dns.resolver.Resolver) -> tuple[bool, str]:
    for rtype in ("A", "AAAA", "CNAME"):
        try:
            resolver.resolve(host, rtype)
            return True, "ok"
        except dns.resolver.NXDOMAIN:
            return False, "nxdomain"
        except dns.resolver.NoAnswer:
            continue
        except dns.exception.Timeout:
            return False, "timeout"
        except Exception as e:  # noqa: BLE001 -- resolution has many failure modes
            return False, f"error:{type(e).__name__}"
    return False, "no_answer_any_type"


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------

def merge_candidates(all_candidates: Iterable[Candidate]) -> dict[str, Candidate]:
    """Dedup by registrable domain. Source priority: curated > crtsh > gov_crawl > tranco.

    All curated seed files declare a `source:` starting with "curated_" (see
    corpus/sources/*.yaml), so a simple prefix match is sufficient here.
    """

    def prio(c: Candidate) -> int:
        if c.source.startswith("curated"):
            return 0
        if c.source.startswith("crtsh"):
            return 1
        if c.source.startswith("gov_crawl"):
            return 2
        if c.source.startswith("tranco"):
            return 3
        return 9

    best: dict[str, Candidate] = {}
    for c in all_candidates:
        cur = best.get(c.domain)
        if cur is None or prio(c) < prio(cur):
            best[c.domain] = c
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-ct", action="store_true", help="skip crt.sh enumeration")
    ap.add_argument("--skip-gov-crawl", action="store_true", help="skip nepal.gov.np crawl")
    ap.add_argument("--skip-tranco", action="store_true", help="skip Tranco cross-check")
    ap.add_argument("--skip-dns-validation", action="store_true",
                     help="do not filter candidates by DNS resolvability (debug only)")
    args = ap.parse_args()

    cfg = load_settings()
    corpus_cfg = cfg["corpus"]
    random.seed(cfg["study"]["random_seed"])
    user_agent = cfg["study"]["user_agent"]
    budget = RequestBudget(corpus_cfg["max_network_requests"])
    stats = BuildStats()

    log.info("=== Phase 1: corpus construction ===")
    log.info("User-Agent: %s", user_agent)
    log.info("Request budget: %d", budget.limit)

    all_candidates: list[Candidate] = []

    log.info("--- Source 1: curated seed lists ---")
    curated = load_curated_sources()
    stats.log_step("curated_loaded", count=len(curated))
    all_candidates.extend(curated)
    known_registrable = {c.domain for c in curated}

    if not args.skip_ct:
        log.info("--- Source 2: Certificate Transparency (crt.sh) ---")
        ct_candidates = crt_sh_candidates(corpus_cfg, user_agent, budget, stats)
        all_candidates.extend(ct_candidates)
    else:
        log.info("--- Source 2: skipped (--skip-ct) ---")

    if not args.skip_gov_crawl:
        log.info("--- Source 3: nepal.gov.np crawl ---")
        crawl_candidates = gov_crawl_candidates(corpus_cfg, user_agent, budget, stats)
        all_candidates.extend(crawl_candidates)
    else:
        log.info("--- Source 3: skipped (--skip-gov-crawl) ---")

    if not args.skip_tranco:
        log.info("--- Source 4: Tranco top-1M cross-check ---")
        tranco_result = tranco_candidates(corpus_cfg, user_agent, budget, stats, known_registrable)
        all_candidates.extend(tranco_result)
    else:
        log.info("--- Source 4: skipped (--skip-tranco) ---")

    log.info("--- Merging %d raw candidates by registrable domain ---", len(all_candidates))
    merged = merge_candidates(all_candidates)
    stats.log_step("merged", unique_domains=len(merged))

    log.info("--- DNS resolvability validation ---")
    date_added = datetime.now(timezone.utc).date().isoformat()
    accepted: list[dict] = []
    rejected: list[dict] = []
    unvalidated: list[dict] = []

    if args.skip_dns_validation:
        for dom, c in merged.items():
            accepted.append({"domain": dom, "sector": c.sector, "source": c.source, "date_added": date_added})
    else:
        dns_budget = SoftBudget(corpus_cfg["dns_validation_budget"])
        resolver_factory = lambda: build_resolver(corpus_cfg["validation_resolvers"], corpus_cfg["dns_timeout_s"])
        items = sorted(merged.items())
        done = 0
        budget_exhausted_at: int | None = None
        with ThreadPoolExecutor(max_workers=corpus_cfg["dns_concurrency"]) as pool:
            thread_local = threading.local()

            def _resolve_one(pair):
                dom, c = pair
                if not hasattr(thread_local, "resolver"):
                    thread_local.resolver = resolver_factory()
                ok, reason = resolves(dom, thread_local.resolver)
                return dom, c, ok, reason

            for dom, c, ok, reason in pool.map(_resolve_one, items):
                done += 1
                if not dns_budget.spend(1):
                    if budget_exhausted_at is None:
                        budget_exhausted_at = done
                    unvalidated.append({"domain": dom, "sector": c.sector, "source": c.source,
                                         "reason": "dns_validation_budget_exhausted"})
                    continue
                if ok:
                    accepted.append({"domain": dom, "sector": c.sector, "source": c.source, "date_added": date_added})
                else:
                    rejected.append({"domain": dom, "reason": reason, "source": c.source})
                if done % 200 == 0:
                    log.info("  validated %d/%d ...", done, len(items))
        if budget_exhausted_at is not None:
            log.warning("DNS validation budget (%d) exhausted after %d/%d candidates; "
                         "%d candidates left unvalidated (see corpus/unvalidated.csv)",
                         corpus_cfg["dns_validation_budget"], budget_exhausted_at, len(items), len(unvalidated))
            stats.log_step("dns_validation_budget_exhausted", at=budget_exhausted_at, remaining=len(unvalidated))

    stats.log_step("dns_validation_done", accepted=len(accepted), rejected=len(rejected),
                    unvalidated=len(unvalidated))

    # --- stratified sampling down to the target corpus size, if needed ---
    sampled_out: list[dict] = []
    if corpus_cfg.get("sample_to_target_if_exceeded") and len(accepted) > corpus_cfg["target_max"]:
        accepted, sampled_out = stratified_sample(
            accepted, corpus_cfg["target_max"], cfg["study"]["random_seed"]
        )
        stats.log_step("sampled_to_target", kept=len(accepted), sampled_out=len(sampled_out),
                        target_max=corpus_cfg["target_max"])
        log.info("Sampled %d -> %d domains to respect corpus.target_max=%d (stratified by sector, seed=%d)",
                  len(accepted) + len(sampled_out), len(accepted),
                  corpus_cfg["target_max"], cfg["study"]["random_seed"])

    # --- write outputs ---
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    domains_csv = CORPUS_DIR / "domains.csv"
    with open(domains_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "sector", "source", "date_added"])
        w.writeheader()
        for row in sorted(accepted, key=lambda r: (r["sector"], r["domain"])):
            w.writerow(row)

    rejected_csv = CORPUS_DIR / "rejected.csv"
    with open(rejected_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["domain", "reason", "source"])
        w.writeheader()
        for row in sorted(rejected, key=lambda r: r["domain"]):
            w.writerow(row)

    if unvalidated:
        unvalidated_csv = CORPUS_DIR / "unvalidated.csv"
        with open(unvalidated_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["domain", "sector", "source", "reason"])
            w.writeheader()
            for row in sorted(unvalidated, key=lambda r: r["domain"]):
                w.writerow(row)

    if sampled_out:
        sampled_out_csv = CORPUS_DIR / "sampled_out.csv"
        with open(sampled_out_csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["domain", "sector", "source", "date_added"])
            w.writeheader()
            for row in sorted(sampled_out, key=lambda r: (r["sector"], r["domain"])):
                w.writerow(row)

    build_log = CORPUS_DIR / "build_log.jsonl"
    with open(build_log, "a") as f:
        for step in stats.steps:
            f.write(json.dumps(step) + "\n")
        f.write(json.dumps({
            "step": "build_complete",
            "ts": datetime.now(timezone.utc).isoformat(),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "unvalidated": len(unvalidated),
            "sampled_out": len(sampled_out),
            "total_source_requests": budget.count,
        }) + "\n")

    write_summary(accepted, rejected, stats, budget, unvalidated, sampled_out)

    log.info("=== Phase 1 complete: %d domains accepted, %d rejected, %d unvalidated, %d sampled out ===",
              len(accepted), len(rejected), len(unvalidated), len(sampled_out))
    log.info("Wrote %s, %s, %s", domains_csv, rejected_csv, CORPUS_DIR / "corpus_summary.md")


def stratified_sample(accepted: list[dict], target_max: int, seed: int) -> tuple[list[dict], list[dict]]:
    """Sample `accepted` down to target_max, proportionally by sector.

    Deterministic given `seed`. Every sector keeps at least 1 domain (if it
    had any) so a small sector is thinned, never erased, by this step.
    """
    rng = random.Random(seed)
    by_sector: dict[str, list[dict]] = {}
    for row in accepted:
        by_sector.setdefault(row["sector"], []).append(row)

    total = len(accepted)
    keep: list[dict] = []
    drop: list[dict] = []
    for sector, rows in by_sector.items():
        rng.shuffle(rows)
        quota = max(1, round(len(rows) * target_max / total))
        keep.extend(rows[:quota])
        drop.extend(rows[quota:])

    # Rounding can push slightly over/under target_max; trim or top up from
    # the largest sector's drop pile so the final count matches exactly.
    if len(keep) > target_max:
        rng.shuffle(keep)
        drop.extend(keep[target_max:])
        keep = keep[:target_max]
    elif len(keep) < target_max and drop:
        rng.shuffle(drop)
        need = target_max - len(keep)
        keep.extend(drop[:need])
        drop = drop[need:]

    return keep, drop


def write_summary(accepted: list[dict], rejected: list[dict], stats: BuildStats, budget: RequestBudget,
                   unvalidated: list[dict] | None = None, sampled_out: list[dict] | None = None):
    unvalidated = unvalidated or []
    sampled_out = sampled_out or []
    by_sector: dict[str, int] = {}
    by_tld: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in accepted:
        by_sector[row["sector"]] = by_sector.get(row["sector"], 0) + 1
        tld = ".".join(row["domain"].split(".")[-2:]) if row["domain"].count(".") >= 2 and row["domain"].endswith(".np") else row["domain"].rsplit(".", 1)[-1]
        by_tld[tld] = by_tld.get(tld, 0) + 1
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1

    lines = []
    lines.append("# Corpus summary\n")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"Total accepted domains: **{len(accepted)}**  \n")
    lines.append(f"Total rejected candidates (DNS non-resolving): **{len(rejected)}**  \n")
    if unvalidated:
        lines.append(f"Left unvalidated (DNS validation budget exhausted): **{len(unvalidated)}**  \n")
    if sampled_out:
        lines.append(f"Sampled out to respect target_max (stratified by sector, fixed seed): **{len(sampled_out)}**  \n")
    lines.append(f"Total source-fetch requests (crt.sh/gov crawl/tranco): **{budget.count}** / {budget.limit} budget\n")

    lines.append("\n## By sector\n")
    lines.append("| Sector | Count |\n|---|---|\n")
    for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |\n")

    lines.append("\n## By TLD / suffix\n")
    lines.append("| TLD | Count |\n|---|---|\n")
    for k, v in sorted(by_tld.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |\n")

    lines.append("\n## By discovery source\n")
    lines.append("| Source | Count |\n|---|---|\n")
    for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |\n")

    lines.append("\n## Rejection reasons\n")
    by_reason: dict[str, int] = {}
    for row in rejected:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    lines.append("| Reason | Count |\n|---|---|\n")
    for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} |\n")

    lines.append("\n## Build steps log\n")
    lines.append("| Step | Details |\n|---|---|\n")
    for step in stats.steps:
        details = {k: v for k, v in step.items() if k not in ("step", "ts")}
        lines.append(f"| {step['step']} | {details} |\n")

    with open(CORPUS_DIR / "corpus_summary.md", "w") as f:
        f.writelines(lines)


if __name__ == "__main__":
    sys.exit(main())
