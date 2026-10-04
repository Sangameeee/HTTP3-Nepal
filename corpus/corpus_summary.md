# Corpus summary
Generated: 2026-08-05T11:54:28.528791+00:00
Total accepted domains: **1200**  
Total rejected candidates (DNS non-resolving): **1259**  
Sampled out to respect target_max (stratified by sector, fixed seed): **975**  
Total source-fetch requests (crt.sh/gov crawl/tranco): **6** / 2000 budget

## By sector
| Sector | Count |
|---|---|
| unclassified | 579 |
| education | 286 |
| government | 250 |
| banking | 37 |
| media | 30 |
| ecommerce_fintech | 11 |
| telecom | 7 |

## By TLD / suffix
| TLD | Count |
|---|---|
| org.np | 310 |
| edu.np | 286 |
| com.np | 254 |
| gov.np | 249 |
| net.np | 49 |
| com | 49 |
| mil.np | 2 |
| org | 1 |

## By discovery source
| Source | Count |
|---|---|
| crtsh:%.org.np | 302 |
| crtsh:%.edu.np | 262 |
| crtsh:%.gov.np | 209 |
| crtsh:%.com.np | 180 |
| tranco:np_tld | 75 |
| crtsh:%.net.np | 45 |
| curated_nrb_nia_nepse | 37 |
| curated_press_council | 30 |
| curated_gov_portal | 27 |
| curated_ugc | 13 |
| curated_manual | 11 |
| curated_nta_licensees | 7 |
| crtsh:%.mil.np | 2 |

## Rejection reasons
| Reason | Count |
|---|---|
| error:NoNameservers | 869 |
| timeout | 270 |
| no_answer_any_type | 68 |
| nxdomain | 52 |

## Build steps log
| Step | Details |
|---|---|
| curated_loaded | {'count': 286} |
| crtsh_query_ok | {'suffix': 'gov.np', 'hosts_found': 2091, 'certs_seen': 2546} |
| crtsh_query_ok | {'suffix': 'edu.np', 'hosts_found': 3494, 'certs_seen': 2659} |
| crtsh_query_ok | {'suffix': 'org.np', 'hosts_found': 3318, 'certs_seen': 2845} |
| crtsh_query_ok | {'suffix': 'com.np', 'hosts_found': 647, 'certs_seen': 4234} |
| crtsh_query_ok | {'suffix': 'net.np', 'hosts_found': 792, 'certs_seen': 3840} |
| crtsh_query_ok | {'suffix': 'mil.np', 'hosts_found': 19, 'certs_seen': 52} |
| gov_crawl_blocked_by_robots | {'seed': 'https://nepal.gov.np/'} |
| tranco_done | {'np_hits': 266, 'cross_confirmed': 48} |
| merged | {'unique_domains': 3434} |
| dns_validation_done | {'accepted': 2175, 'rejected': 1259, 'unvalidated': 0} |
| sampled_to_target | {'kept': 1200, 'sampled_out': 975, 'target_max': 1200} |

## Post-processing: sector reclassification (2026-08-05T12:00:02.254760+00:00)
Ran `corpus/classify_sectors.py` against `config/sector_keywords.yaml` to split the original "unclassified" bucket into confirmed RQ1 sectors, `other_not_in_scope` (NGOs/personal sites/clubs -- confirmed not one of the 6 sectors), and a smaller honest `unclassified` residual. See `corpus/classification_log.csv` for every individual decision.

Reclassified: **56** rows (38 into an RQ1 sector, 18 into other_not_in_scope).

### Sector counts after reclassification
| Sector | Count |
|---|---|
| unclassified | 523 |
| education | 288 |
| government | 250 |
| banking | 49 |
| media | 49 |
| other_not_in_scope | 18 |
| ecommerce_fintech | 15 |
| telecom | 8 |
