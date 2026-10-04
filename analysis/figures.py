#!/usr/bin/env python3
"""Generates every figure from this repo's own measurement data (brief
Correctness requirement: never reproduce a figure from prior literature).
Writes vector PDFs to output/figures/.

Color usage follows the dataviz skill's method: categorical hues assigned
in fixed order (never cycled, never a rainbow on a single-series chart),
using the skill's validated default palette (references/palette.md in the
skill bundle) at its documented light-mode values -- these figures target a
printed/PDF manuscript, so only light mode applies; there is no dark-mode
variant to ship. Single-series bar charts (RQ1) use one hue throughout, not
one color per bar, since color would otherwise imply a categorical
grouping that isn't there. Genuinely multi-category charts (RQ2 ownership
classes, RQ5 operators) use the fixed categorical order. RQ5's
idle-survival grid uses the status palette's good/critical pair (a
survives/fails state, not an identity), shipped with direct checkmark/cross
labels in every cell rather than relying on color alone.

Sized at 6in x 3.5in by default -- a reasonable general-purpose aspect
ratio; Phase 5 (manuscript) may need to resize for the journal's specific
single-column page once that phase actually starts, per the brief's
instruction to generate figures at the correct aspect ratio for inline
placement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from analysis.load import (  # noqa: E402
    load_probe_runs, load_viability_runs, load_handshake_bench_runs, load_pageload_bench_runs,
)
from analysis.rq1_adoption import compute as compute_rq1, SECTOR_ORDER, SECTOR_LABELS  # noqa: E402
from analysis.rq2_ownership import compute_ownership  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "output" / "figures"

# Categorical slots (light mode), fixed order, per the dataviz skill's
# validated default palette -- documented as passing CVD/contrast checks
# for the first 3 (all-pairs) and first 8 (adjacent-pairs) slots.
BLUE, ORANGE, AQUA, YELLOW, MAGENTA, GREEN, VIOLET, RED = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948",
)
INK = "#0b0b0b"
SECONDARY_INK = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
STATUS_GOOD = "#0ca30c"
STATUS_CRITICAL = "#d03b3b"

plt.rcParams.update({
    "font.family": "sans-serif",
    "axes.edgecolor": MUTED,
    "axes.labelcolor": INK,
    "text.color": INK,
    "xtick.color": SECONDARY_INK,
    "ytick.color": SECONDARY_INK,
    "axes.grid": False,
    "svg.fonttype": "none",
})


def _save_both(fig, name: str) -> Path:
    """Saves vector PDF (paper submission, print) + 300dpi PNG (Word
    embedding -- python-docx/Word can't place a vector PDF inline, so the
    manuscript pipeline needs a raster fallback at print-legible
    resolution). Returns the PDF path, the one every other caller in this
    file already expects."""
    pdf_path = OUTPUT_DIR / f"{name}.pdf"
    fig.savefig(pdf_path)
    fig.savefig(OUTPUT_DIR / f"{name}.png", dpi=300)
    return pdf_path


def _clean_axes(ax):
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(MUTED)
    ax.spines["bottom"].set_color(MUTED)


def _fig_adoption_by_sector(table, name, subtitle):
    table = table.set_index("Sector").reindex(
        [SECTOR_LABELS[s] for s in SECTOR_ORDER]
    ).sort_values("adoption_rate_pct", ascending=True)

    fig, ax = plt.subplots(figsize=(6, 3.5))
    bars = ax.barh(table.index, table["adoption_rate_pct"], color=BLUE, height=0.6)
    for bar, (_, row) in zip(bars, table.iterrows()):
        ax.text(bar.get_width() + 1.5, bar.get_y() + bar.get_height() / 2,
                 f"{row['adoption_rate_pct']:.1f}% (n={int(row['total'])})",
                 va="center", ha="left", fontsize=9, color=SECONDARY_INK)
    ax.set_xlim(0, 68)
    ax.set_xlabel("HTTP/3 adoption rate (%)")
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    fig.suptitle("HTTP/3 adoption by sector", x=0.02, ha="left", fontsize=12, fontweight="bold", y=0.98)
    fig.text(0.02, 0.90, subtitle, fontsize=8, color=MUTED)
    fig.tight_layout(rect=[0, 0, 1, 0.88])
    out = _save_both(fig, name)
    plt.close(fig)
    return out


def fig_rq1_adoption(df):
    return _fig_adoption_by_sector(compute_rq1(df), "rq1_adoption_by_sector",
                                    "Apex hosts, run-2026-08-05b, n=659 domains across 6 sectors")


def fig_rq1_adoption_union(union_table, n_domains: int):
    return _fig_adoption_by_sector(
        union_table, "rq1_adoption_by_sector_union",
        f"Apex hosts, 3-run union (2026-08-05/07/08), n={n_domains} domains across 6 sectors")


def _fig_ownership(table, name, subtitle):
    colors = [BLUE, ORANGE, AQUA, VIOLET, MUTED]  # foreign_cdn, foreign_hosting, nepali_hosting, mixed, unknown
    color_map = dict(zip(["Foreign CDN", "Foreign hosting (non-CDN)", "Nepali hosting",
                           "Mixed (A/AAAA disagree)", "Unknown"], colors))
    table = table.sort_values("count", ascending=True)
    bar_colors = [color_map.get(c, MUTED) for c in table["Ownership class"]]

    fig, ax = plt.subplots(figsize=(6, 3.0))
    bars = ax.barh(table["Ownership class"], table["count"], color=bar_colors, height=0.55)
    for bar, (_, row) in zip(bars, table.iterrows()):
        ax.text(bar.get_width() + 1.5, bar.get_y() + bar.get_height() / 2,
                 f"{int(row['count'])} ({row['share_pct']:.1f}%)",
                 va="center", ha="left", fontsize=9, color=SECONDARY_INK)
    ax.set_xlim(0, max(table["count"]) * 1.35)
    ax.set_xlabel("HTTP/3-capable apex hosts")
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    fig.suptitle("Who serves Nepal's HTTP/3: ownership class", x=0.02, ha="left",
                  fontsize=12, fontweight="bold", y=0.98)
    fig.text(0.02, 0.87, subtitle, fontsize=8, color=MUTED)
    fig.tight_layout(rect=[0, 0, 1, 0.84])
    out = _save_both(fig, name)
    plt.close(fig)
    return out


def fig_rq2_ownership(df):
    return _fig_ownership(compute_ownership(df), "rq2_ownership",
                           "n=201 HTTP/3-capable apex hosts; 98.1% of foreign_cdn is Cloudflare")


def fig_rq2_ownership_union(union_table, n_total: int, cloudflare_pct_of_cdn: float):
    return _fig_ownership(
        union_table, "rq2_ownership_union",
        f"n={n_total} HTTP/3-capable apex hosts, 3-run union; {cloudflare_pct_of_cdn:.1f}% of foreign_cdn is Cloudflare")


def fig_rq5_idle_survival(vdf):
    pivot = vdf.pivot_table(index="host", columns="idle_s", values="resumed_ok", aggfunc="first")
    pivot = pivot[[30, 60, 120]]
    # Order rows by "how long it survived" so the pattern reads top-to-bottom
    survives_count = pivot.sum(axis=1)
    pivot = pivot.loc[survives_count.sort_values(ascending=False).index]

    fig, ax = plt.subplots(figsize=(6, 3.2))
    n_rows, n_cols = pivot.shape
    for i, host in enumerate(pivot.index):
        for j, interval in enumerate(pivot.columns):
            ok = pivot.loc[host, interval]
            color = STATUS_GOOD if ok else STATUS_CRITICAL
            ax.add_patch(plt.Rectangle((j, n_rows - 1 - i), 0.92, 0.82, facecolor=color, edgecolor="white", linewidth=1.5))
            ax.text(j + 0.46, n_rows - 1 - i + 0.41, "✓" if ok else "✗",
                     ha="center", va="center", color="white", fontsize=11, fontweight="bold")

    ax.set_xlim(0, n_cols)
    ax.set_ylim(0, n_rows)
    ax.set_xticks([j + 0.46 for j in range(n_cols)])
    ax.set_xticklabels([f"{c}s idle" for c in pivot.columns], fontsize=9)
    ax.set_yticks([n_rows - 1 - i + 0.41 for i in range(n_rows)])
    ax.set_yticklabels(pivot.index, fontsize=9)
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    fig.suptitle("Connection survival after idling, by reference server", x=0.02, ha="left",
                  fontsize=12, fontweight="bold", y=0.985)
    fig.text(0.02, 0.85,
              "Clusters by operator, not uniformly -- consistent with server-side idle-timeout\n"
              "policy, not confirmed NAT rebinding (see phases.md Phase 3)",
              fontsize=8, color=MUTED, va="top")
    fig.tight_layout(rect=[0, 0, 1, 0.74])
    out = _save_both(fig, "rq5_idle_survival")
    plt.close(fig)
    return out


def fig_rq4_handshake_cdf(hdf):
    """Empirical CDF, H3 vs. H2 handshake duration -- a distribution
    comparison (Correctness point: never summarize a skewed latency
    distribution with a bar of the mean alone), per the brief's explicit
    ask for CDF plots in RQ4. Two-series line chart -> categorical color,
    fixed order (H3 first, matching every other figure's protocol-identity
    convention in this repo), single shared axis (x=ms), legend present
    since n=2 series."""
    ok = hdf[(hdf["success"] == True) & hdf["handshake_duration_ms"].notna()]

    fig, ax = plt.subplots(figsize=(6, 3.5))
    for proto, label, color in (("h3", "HTTP/3 (QUIC)", BLUE), ("h2", "HTTP/2 (TCP+TLS)", ORANGE)):
        vals = ok[ok["protocol"] == proto]["handshake_duration_ms"].sort_values()
        y = (pd.Series(range(1, len(vals) + 1)) / len(vals) * 100).values
        ax.plot(vals.values, y, color=color, linewidth=2, label=label)

    ax.set_xlim(0, 700)
    ax.set_xlabel("Handshake completion time (ms)")
    ax.set_ylabel("Cumulative % of trials")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
    ax.grid(axis="both", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    fig.suptitle("H3 vs. H2 handshake time (CDF)", x=0.02, ha="left", fontsize=12, fontweight="bold", y=0.98)
    fig.text(0.02, 0.90,
              "n=185 same-origin domains, 30 interleaved trials each (run-2026-08-06)\n"
              "Mann-Whitney U p<0.001 -- H2 faster at the median",
              fontsize=8, color=MUTED, va="top")
    fig.tight_layout(rect=[0, 0, 1, 0.80])
    out = _save_both(fig, "rq4_handshake_cdf")
    plt.close(fig)
    return out


def fig_rq4_pageload_cdf(pldf):
    """Empirical CDF, H3 vs. H2 full page-load (`loadEventEnd`) time --
    netlog-verified trials only (see analysis/rq4_performance.py's own
    docstring on why: an unverified trial's forced protocol didn't
    actually happen on the wire, so including it would blend the two
    series together)."""
    ok = pldf[pldf["verified"] & pldf["load_event_ms"].notna()]

    fig, ax = plt.subplots(figsize=(6, 3.5))
    for proto, label, color in (("h3", "HTTP/3 (QUIC)", BLUE), ("h2", "HTTP/2 (TCP+TLS)", ORANGE)):
        vals = ok[ok["protocol"] == proto]["load_event_ms"].sort_values()
        y = (pd.Series(range(1, len(vals) + 1)) / len(vals) * 100).values
        ax.plot(vals.values, y, color=color, linewidth=2, label=label)

    ax.set_xlim(0, 8000)
    ax.set_xlabel("Full page load time (ms)")
    ax.set_ylabel("Cumulative % of trials")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(decimals=0))
    ax.grid(axis="both", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    _clean_axes(ax)
    ax.legend(loc="lower right", frameon=False, fontsize=9)
    fig.suptitle("H3 vs. H2 full page-load time (CDF)", x=0.02, ha="left", fontsize=12, fontweight="bold", y=0.98)
    fig.text(0.02, 0.90,
              "n=50 domains, 30 interleaved trials each (run-2026-08-07), netlog-verified only\n"
              "Mann-Whitney U p=0.19 -- no significant difference at the median",
              fontsize=8, color=MUTED, va="top")
    fig.tight_layout(rect=[0, 0, 1, 0.80])
    out = _save_both(fig, "rq4_pageload_cdf")
    plt.close(fig)
    return out


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []

    df = load_probe_runs()
    if not df.empty:
        written.append(fig_rq1_adoption(df))
        written.append(fig_rq2_ownership(df))
    else:
        print("No probe data found -- skipping RQ1/RQ2 figures.")

    try:
        from analysis.cross_run_variance import (
            RUN_IDS, build_union, compute_union_rq1_table, compute_union_ownership_table,
        )
        run_dfs = {rid: load_probe_runs(run_ids=[rid]) for rid in RUN_IDS}
        if all(not d.empty for d in run_dfs.values()):
            union_df = build_union(run_dfs)
            rq1_union = compute_union_rq1_table(union_df)
            written.append(fig_rq1_adoption_union(rq1_union, n_domains=int(rq1_union["total"].sum())))

            rq2_union = compute_union_ownership_table(union_df)
            n_total = int(rq2_union["count"].sum())
            cdn_count = int(rq2_union.loc[rq2_union["Ownership class"] == "Foreign CDN", "count"].iloc[0])
            # Cloudflare-of-foreign_cdn ratio is stable at 101/103 across all
            # 3 runs (see phases.md Phase 2's cross-run variance section) --
            # not re-derived here since compute_union_ownership_table doesn't
            # break foreign_cdn down by individual provider.
            cloudflare_pct = round(100 * 101 / cdn_count, 1) if cdn_count else 0.0
            written.append(fig_rq2_ownership_union(rq2_union, n_total, cloudflare_pct))
        else:
            print("Not all 3 cross-run probe files present -- skipping RQ1/RQ2 union figures.")
    except FileNotFoundError:
        print("Cross-run data incomplete -- skipping RQ1/RQ2 union figures.")

    vdf = load_viability_runs()
    if not vdf.empty:
        written.append(fig_rq5_idle_survival(vdf))
    else:
        print("No viability data found -- skipping RQ5 figure.")

    hdf = load_handshake_bench_runs()
    if not hdf.empty:
        written.append(fig_rq4_handshake_cdf(hdf))
    else:
        print("No handshake benchmark data found -- skipping RQ4 handshake figure.")

    pldf = load_pageload_bench_runs()
    if not pldf.empty:
        written.append(fig_rq4_pageload_cdf(pldf))
    else:
        print("No page-load benchmark data found -- skipping RQ4 page-load figure.")

    for path in written:
        print("Wrote", path)


if __name__ == "__main__":
    main()
