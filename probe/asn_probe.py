"""IP -> ASN -> ownership-class resolution, via Team Cymru's DNS whois
service (`origin.asn.cymru.com` / `origin6.asn.cymru.com` for the ASN,
`AS<n>.asn.cymru.com` for the AS name), classified against
`config/asn_classification.yaml` (RQ2's central artifact).

Deliberately DNS-based rather than a local `pyasn`/RouteViews database:
Cymru's service is free, requires no local multi-hundred-MB RIB download or
periodic refresh, and its answers are cacheable per-run just like everything
else here. The tradeoff -- two extra DNS round trips per unique IP -- is
small against ~659 domains with typically 1-2 IPs each, and is exactly the
kind of traffic the corpus-build budget conversation was about (see
phases.md Phase 1): these are DNS lookups to a public whois service, not
requests to Nepali target infrastructure.

`unknown` is never silently merged into another class -- see
`classify_asn()` and the config file's own header comment.
"""
from __future__ import annotations

import functools
import ipaddress
import re

import dns.asyncresolver
import dns.exception
import yaml

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)
def _load_classification() -> dict:
    with open(REPO_ROOT / "config" / "asn_classification.yaml") as f:
        return yaml.safe_load(f)


def classify_asn(asn: int | None, as_name: str | None) -> str:
    """Returns one of foreign_cdn / foreign_hosting / nepali_hosting /
    unknown. `unknown` covers both "lookup failed" (asn is None) and "ASN
    resolved but isn't in our mapping and no name pattern matched" -- both
    are reported as their own count in RQ2 tables, never folded into
    another class (config file's own stated policy)."""
    spec = _load_classification()
    if asn is not None:
        entry = spec.get("asns", {}).get(asn)
        if entry:
            return entry["class"]
    if as_name:
        for cls, patterns in spec.get("name_patterns", {}).items():
            for pattern in patterns:
                if re.search(pattern, as_name):
                    return cls
    return "unknown"


async def _cymru_txt(resolver: dns.asyncresolver.Resolver, query: str) -> str | None:
    try:
        answer = await resolver.resolve(query, "TXT")
        # dnspython TXT records may be split into multiple byte-strings; join them.
        return b"".join(answer[0].strings).decode()
    except Exception:  # noqa: BLE001 -- treat any Cymru lookup failure as "no data", not fatal
        return None


def _origin_query_name(ip: str) -> str | None:
    addr = ipaddress.ip_address(ip)
    if addr.version == 4:
        octets = ip.split(".")
        return ".".join(reversed(octets)) + ".origin.asn.cymru.com"
    else:
        # reverse_pointer gives "<nibbles>.ip6.arpa"; Cymru wants the same
        # nibbles under origin6.asn.cymru.com instead.
        nibbles = addr.reverse_pointer.removesuffix(".ip6.arpa")
        return nibbles + ".origin6.asn.cymru.com"


_ip_cache: dict[str, dict] = {}


async def lookup_ip(ip: str, timeout_s: float) -> dict:
    """Returns {"ip": ip, "asn": int|None, "as_name": str|None, "ownership_class": str}.

    Cached per-IP for the lifetime of the process. Many hosts in the corpus
    share CDN edge IPs (a large fraction are expected to be behind
    Cloudflare/Akamai/etc.), so a full scan would otherwise re-resolve the
    same handful of IPs hundreds of times -- this cache is what keeps the
    ASN-lookup share of the per-run request budget proportional to the
    number of *distinct* IPs actually observed, not the number of hosts
    probed. Safe within a single run: ASN-to-IP announcements don't change
    on a timescale that matters here. Not persisted across runs (a fresh
    process starts cold) since a stale ASN mapping silently feeding into a
    later run would be a worse failure mode than a few repeated lookups.
    """
    if ip in _ip_cache:
        return _ip_cache[ip]
    result = await _lookup_ip_uncached(ip, timeout_s)
    _ip_cache[ip] = result
    return result


async def _lookup_ip_uncached(ip: str, timeout_s: float) -> dict:
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = timeout_s
    resolver.lifetime = timeout_s

    result = {"ip": ip, "asn": None, "as_name": None, "ownership_class": "unknown"}

    query = _origin_query_name(ip)
    if query is None:
        return result

    origin_txt = await _cymru_txt(resolver, query)
    if not origin_txt:
        return result

    # Format: "13335 | 104.18.16.0/20 | US | arin | 2014-03-28"
    # A single IP can be covered by multiple announcements (rare); take the first.
    first_record = origin_txt.split("\n")[0]
    fields = [f.strip() for f in first_record.split("|")]
    asn_field = fields[0].split()[0] if fields else ""
    try:
        asn = int(asn_field)
    except ValueError:
        return result
    result["asn"] = asn

    name_txt = await _cymru_txt(resolver, f"AS{asn}.asn.cymru.com")
    if name_txt:
        # Format: "13335 | US | arin | 2010-07-14 | CLOUDFLARENET - Cloudflare, Inc., US"
        name_fields = [f.strip() for f in name_txt.split("|")]
        if len(name_fields) >= 5:
            result["as_name"] = name_fields[4]

    result["ownership_class"] = classify_asn(result["asn"], result["as_name"])
    return result


async def probe_asns(ips: list[str], timeout_s: float) -> tuple[list[dict], str]:
    """Looks up every IP, returns (asns_list, overall_ownership_class).
    overall_ownership_class is the single class if every IP agrees, "mixed"
    if IPs disagree (e.g. A record on Nepali hosting, AAAA on a foreign CDN
    -- itself a finding worth keeping visible rather than picking one IP
    arbitrarily), or "unknown" if nothing resolved."""
    results = []
    for ip in ips:
        results.append(await lookup_ip(ip, timeout_s))

    classes = {r["ownership_class"] for r in results if r["ownership_class"] != "unknown"}
    if not results:
        overall = "unknown"
    elif len(classes) == 1:
        overall = next(iter(classes))
    elif len(classes) > 1:
        overall = "mixed"
    else:
        overall = "unknown"

    return results, overall
