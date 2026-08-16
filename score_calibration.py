"""Empirical, source-aware calibration of alert scores against 1h outcomes."""

import time
from typing import Any

import config

_cache: tuple[float, list[dict]] = (0.0, [])
CACHE_SECONDS = 300


def _outcomes() -> list[dict]:
    global _cache
    if time.time() - _cache[0] > CACHE_SECONDS:
        import feature_logger
        cutoff = time.time() - 90 * 86400
        _cache = (time.time(), [row for row in feature_logger.export_training_data()
                                if float(row.get("alerted_at") or 0) >= cutoff])
    return _cache[1]


def _return_1h(row: dict) -> float | None:
    if row.get("rugged"):
        return -100.0
    entry = float(row.get("price_at_alert") or row.get("price_usd") or 0)
    price = float(row.get("price_1h") or 0)
    if entry <= 0 or price <= 0:
        return None
    return (price - entry) / entry * 100


def _historical_entry_score(row: dict) -> float:
    """Apply today's late-entry penalty to historical feature snapshots."""
    if row.get("score_setup") is not None:
        return max(0.0, float(row.get("score_setup") or 0) - float(row.get("entry_risk_penalty") or 0))
    import filters
    pair = {
        "priceChange": {
            "m5": row.get("price_change_5m", 0), "h1": row.get("price_change_1h", 0),
            "h6": row.get("price_change_6h", 0),
        },
        "volume": {"h24": row.get("volume_24h", 0)},
        "liquidity": {"usd": row.get("liquidity_usd", 0)},
        "txns": {"h1": {"buys": row.get("txns_1h_buys", 0),
                          "sells": row.get("txns_1h_sells", 0)}},
    }
    penalty, _ = filters._entry_risk_penalty(pair)
    return max(0.0, float(row.get("score_total") or 0) - penalty)


def calibrate(source: str, score: float, chain_id: str | None = None) -> dict[str, Any]:
    """Return Bayesian win probability and shrinkage expectancy for this cohort."""
    rows = _outcomes()
    if chain_id:
        rows = [row for row in rows if (row.get("chain_id") or "").lower() == chain_id.lower()]
    usable = [(row, _return_1h(row)) for row in rows]
    usable = [(row, value) for row, value in usable if value is not None]
    global_returns = [value for _, value in usable]
    global_win = (sum(value >= 10 for value in global_returns) / len(global_returns)
                  if global_returns else 0.25)
    global_avg = sum(global_returns) / len(global_returns) if global_returns else -10.0

    source_rows = [(row, value) for row, value in usable
                   if (row.get("alert_source") or "legacy") == source]
    if source == "scan":
        bucket = int(max(0, min(99, score)) // 10) * 10
        cohort = [(row, value) for row, value in source_rows
                  if bucket <= _historical_entry_score(row) < bucket + 10]
        cohort_name = f"scan {bucket}-{bucket + 9}"
    else:
        cohort = source_rows
        cohort_name = source

    returns = [value for _, value in cohort]
    samples = len(returns)
    prior_strength = 20
    wins = sum(value >= 10 for value in returns)
    probability = (wins + global_win * prior_strength) / (samples + prior_strength)
    expectancy = ((sum(returns) if returns else 0) + global_avg * prior_strength) / (samples + prior_strength)
    min_samples = config.AUTO_BUY_MIN_CALIBRATION_SAMPLES
    eligible = (
        samples >= min_samples
        and probability >= config.AUTO_BUY_MIN_CALIBRATED_PROB
        and expectancy >= config.AUTO_BUY_MIN_EXPECTANCY_PCT
    )
    return {
        "probability": round(probability, 3),
        "expectancy_pct": round(expectancy, 1),
        "samples": samples,
        "eligible": eligible,
        "cohort": cohort_name,
        "chain_id": chain_id or "all",
        "win_definition": "+10% at 1h",
    }


def clear_cache() -> None:
    global _cache
    _cache = (0.0, [])
