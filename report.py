"""CLI report tool for alert outcome analysis.

Usage:
    python report.py --days 7
    python report.py --days 30 --export csv
"""

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import storage


def _pct_change(price_at_alert: float | None, price_later: float | None) -> float | None:
    """Calculate percentage change."""
    if price_at_alert is None or price_later is None or price_at_alert <= 0:
        return None
    return ((price_later - price_at_alert) / price_at_alert) * 100


def _format_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"


def _compute_stats(rows: list[dict], window: str) -> dict:
    """Compute win rate and avg return for a specific window."""
    price_col = f"price_{window}"
    checked_col = f"checked_{window}"

    valid = [r for r in rows if r.get(checked_col) and r.get(price_col) is not None and r.get("price_at_alert")]
    if not valid:
        return {"samples": 0, "win_rate": None, "avg_return": None, "max_return": None}

    returns = []
    for r in valid:
        pct = _pct_change(r["price_at_alert"], r[price_col])
        if pct is not None:
            returns.append(pct)

    if not returns:
        return {"samples": 0, "win_rate": None, "avg_return": None, "max_return": None}

    wins = sum(1 for r in returns if r > 0)
    return {
        "samples": len(returns),
        "win_rate": (wins / len(returns)) * 100,
        "avg_return": sum(returns) / len(returns),
        "max_return": max(returns),
    }


def _print_report(rows: list[dict], days: int) -> None:
    """Print the formatted performance report."""
    total = len(rows)
    rugged = sum(1 for r in rows if r.get("rugged"))
    rug_rate = (rugged / total * 100) if total > 0 else 0

    print()
    print("=" * 70)
    print(f"  MEMESCOUT PERFORMANCE REPORT  --  Last {days} days")
    print("=" * 70)
    print()
    print(f"  Total alerts: {total}        Rugged: {rugged} ({rug_rate:.1f}%)")
    print()

    # Overall win rate table
    print("-" * 70)
    print(f"  {'Window':<10} {'Samples':<10} {'Win Rate':<12} {'Avg Return':<12} {'Max Gain':<12}")
    print("-" * 70)
    for window in ["15m", "1h", "6h", "24h"]:
        s = _compute_stats(rows, window)
        samples = str(s["samples"]) if s["samples"] else "-"
        wr = f"{s['win_rate']:.1f}%" if s["win_rate"] is not None else "-"
        ar = _format_pct(s["avg_return"]) if s["avg_return"] is not None else "-"
        mx = _format_pct(s["max_return"]) if s["max_return"] is not None else "-"
        print(f"  {window:<10} {samples:<10} {wr:<12} {ar:<12} {mx:<12}")
    print("-" * 70)
    print()

    # By chain
    chains = sorted(set(r.get("chain_id", "?") for r in rows))
    print("-" * 70)
    print(f"  {'Chain':<10} {'Alerts':<8} {'Win @1h':<10} {'Avg @1h':<10} {'Rugged':<8} {'Rug Rate':<10}")
    print("-" * 70)
    for chain in chains:
        chain_rows = [r for r in rows if r.get("chain_id") == chain]
        c_total = len(chain_rows)
        c_rugs = sum(1 for r in chain_rows if r.get("rugged"))
        c_rug_rate = (c_rugs / c_total * 100) if c_total > 0 else 0
        s = _compute_stats(chain_rows, "1h")
        wr = f"{s['win_rate']:.1f}%" if s["win_rate"] is not None else "-"
        ar = _format_pct(s["avg_return"]) if s["avg_return"] is not None else "-"
        print(f"  {chain.upper():<10} {c_total:<8} {wr:<10} {ar:<10} {c_rugs:<8} {c_rug_rate:.1f}%")
    print("-" * 70)
    print()

    # By score bucket
    buckets = [(50, 60), (60, 70), (70, 80), (80, 100)]
    print("-" * 70)
    print(f"  {'Score':<10} {'Alerts':<8} {'Win @1h':<10} {'Avg @1h':<10} {'Avg @24h':<10} {'Rug Rate':<10}")
    print("-" * 70)
    for lo, hi in buckets:
        bucket_rows = [r for r in rows if lo <= (r.get("score_at_alert") or 0) < hi]
        if not bucket_rows:
            continue
        b_total = len(bucket_rows)
        b_rugs = sum(1 for r in bucket_rows if r.get("rugged"))
        b_rug_rate = (b_rugs / b_total * 100) if b_total > 0 else 0
        s1 = _compute_stats(bucket_rows, "1h")
        s24 = _compute_stats(bucket_rows, "24h")
        wr = f"{s1['win_rate']:.1f}%" if s1["win_rate"] is not None else "-"
        ar1 = _format_pct(s1["avg_return"]) if s1["avg_return"] is not None else "-"
        ar24 = _format_pct(s24["avg_return"]) if s24["avg_return"] is not None else "-"
        print(f"  {lo}-{hi:<7} {b_total:<8} {wr:<10} {ar1:<10} {ar24:<10} {b_rug_rate:.1f}%")
    print("-" * 70)
    print()

    # Best and worst performers
    all_returns_1h = []
    for r in rows:
        if r.get("checked_1h") and r.get("price_1h") and r.get("price_at_alert"):
            pct = _pct_change(r["price_at_alert"], r["price_1h"])
            if pct is not None:
                all_returns_1h.append((r, pct))

    if all_returns_1h:
        best = max(all_returns_1h, key=lambda x: x[1])
        worst = min(all_returns_1h, key=lambda x: x[1])
        print(f"  BEST:  ${best[0].get('token_symbol', '?')} ({best[0].get('chain_id', '?').upper()}) "
              f"-- {_format_pct(best[1])} @ 1h  (score: {best[0].get('score_at_alert', '?'):.0f})")
        rug_note = " (rugged)" if worst[0].get("rugged") else ""
        print(f"  WORST: ${worst[0].get('token_symbol', '?')} ({worst[0].get('chain_id', '?').upper()}) "
              f"-- {_format_pct(worst[1])} @ 1h  (score: {worst[0].get('score_at_alert', '?'):.0f}{rug_note})")
    else:
        print("  No 1h price data available yet for best/worst analysis.")

    print()
    print("=" * 70)


def _export_csv(rows: list[dict], days: int) -> None:
    """Export raw data to CSV."""
    if not rows:
        print("No data to export.")
        return

    filename = f"alert_outcomes_{datetime.now().strftime('%Y-%m-%d')}.csv"
    filepath = Path(__file__).parent / filename

    fieldnames = [
        "id", "token_address", "chain_id", "pair_address", "token_symbol",
        "alerted_at", "score_at_alert", "price_at_alert", "liquidity_at_alert",
        "market_cap_at_alert", "price_15m", "price_1h", "price_6h", "price_24h",
        "max_price_24h", "checked_15m", "checked_1h", "checked_6h", "checked_24h",
        "rugged",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} rows to {filepath}")


def main():
    parser = argparse.ArgumentParser(description="MemeScout performance report")
    parser.add_argument("--days", type=int, default=7, help="Number of days to report on (default: 7)")
    parser.add_argument("--export", choices=["csv"], help="Export format (csv)")
    args = parser.parse_args()

    rows = storage.get_outcomes_for_report(args.days)

    if not rows:
        print(f"No alert outcomes found in the last {args.days} days.")
        sys.exit(0)

    if args.export == "csv":
        _export_csv(rows, args.days)
    else:
        _print_report(rows, args.days)


if __name__ == "__main__":
    main()
