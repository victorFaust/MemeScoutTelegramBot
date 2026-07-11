"""Scoring and filtering logic for memecoin pairs.

Every pair is scored 0-100. Individual sub-scores are weighted and summed.
All thresholds come from config.py (ultimately .env).
"""

import logging
import time
from typing import Any

import config

logger = logging.getLogger(__name__)


# -- Helpers --

def _safe(d: dict | None, *keys: str, default: Any = None) -> Any:
    """Nested safe-get."""
    obj: Any = d
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, default)
    return obj


def _pair_age_hours(pair: dict) -> float | None:
    """Return pair age in hours, or None if unknown."""
    created = pair.get("pairCreatedAt")
    if created is None:
        return None
    try:
        return (time.time() * 1000 - float(created)) / 3_600_000
    except (ValueError, TypeError):
        return None


# -- Sub-score functions (each returns 0-1) --

def _score_liquidity(pair: dict) -> float:
    """Higher when liquidity sits in the sweet-spot range."""
    liq = _safe(pair, "liquidity", "usd", default=0)
    if liq < config.MIN_LIQUIDITY_USD or liq > config.MAX_LIQUIDITY_USD:
        return 0.0
    mid = (config.MIN_LIQUIDITY_USD + config.MAX_LIQUIDITY_USD) / 2
    distance = abs(liq - mid) / (config.MAX_LIQUIDITY_USD - config.MIN_LIQUIDITY_USD)
    return max(0.0, 1.0 - distance)


def _score_market_cap(pair: dict) -> float:
    """Lower market cap -> higher score (room to grow)."""
    mc = pair.get("marketCap") or pair.get("fdv") or 0
    if mc <= 0 or mc > config.MAX_MARKET_CAP:
        return 0.0
    return 1.0 - (mc / config.MAX_MARKET_CAP)


def _score_pair_age(pair: dict) -> float:
    """Newer pairs score higher (within the allowed window)."""
    age_h = _pair_age_hours(pair)
    if age_h is None or age_h > config.MAX_PAIR_AGE_HOURS:
        return 0.0
    return 1.0 - (age_h / config.MAX_PAIR_AGE_HOURS)


def _score_volume_liquidity(pair: dict) -> float:
    """Volume-to-liquidity ratio -- higher means more active trading."""
    vol = _safe(pair, "volume", "h24", default=0)
    liq = _safe(pair, "liquidity", "usd", default=0)
    if liq <= 0:
        return 0.0
    ratio = vol / liq
    if ratio < config.MIN_VOLUME_LIQUIDITY_RATIO:
        return 0.0
    # Cap the score at ratio = 5x for normalization
    return min(ratio / 5.0, 1.0)


def _score_price_change(pair: dict) -> float:
    """Positive momentum on 1h and 6h windows."""
    pc = pair.get("priceChange") or {}
    h1 = pc.get("h1", 0) or 0
    h6 = pc.get("h6", 0) or 0
    if h1 < config.MIN_PRICE_CHANGE_1H or h6 < config.MIN_PRICE_CHANGE_6H:
        return 0.0
    # Normalize: +50% maps to 1.0
    s1 = min(max(h1, 0) / 50.0, 1.0)
    s6 = min(max(h6, 0) / 50.0, 1.0)
    return (s1 + s6) / 2.0


def _score_buy_sell_ratio(pair: dict) -> float:
    """More buys than sells in the last hour -> bullish."""
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    buys = h1.get("buys", 0) or 0
    sells = h1.get("sells", 0) or 0
    total = buys + sells
    if total < config.MIN_TX_COUNT_1H:
        return 0.0  # Too few transactions -- suspicious / dead
    ratio = buys / total
    # 0.5 -> neutral (0.0 score), 1.0 -> all buys (1.0 score)
    return max(0.0, (ratio - 0.5) * 2.0)


# -- Weights --
# Each tuple: (name, weight-out-of-100, scoring-function)

_WEIGHTS: list[tuple[str, float, Any]] = [
    ("liquidity",      15, _score_liquidity),
    ("market_cap",     15, _score_market_cap),
    ("pair_age",       10, _score_pair_age),
    ("vol_liq_ratio",  20, _score_volume_liquidity),
    ("price_change",   20, _score_price_change),
    ("buy_sell_ratio", 20, _score_buy_sell_ratio),
]


def score_pair(pair: dict) -> dict:
    """Score a single pair dict. Returns a result dict with per-component
    scores, the total weighted score (0-100), and the original pair data."""
    breakdown: dict[str, float] = {}
    total = 0.0
    for name, weight, fn in _WEIGHTS:
        raw = fn(pair)
        breakdown[name] = round(raw, 3)
        total += raw * weight

    total = round(min(total, 100.0), 1)

    return {
        "score": total,
        "breakdown": breakdown,
        "pair": pair,
    }


# -- Hard-reject gate (fast pre-filter before scoring) --

def passes_hard_filters(pair: dict) -> bool:
    """Return False for pairs that should never be scored at all."""
    liq = _safe(pair, "liquidity", "usd", default=0)
    if liq < config.MIN_LIQUIDITY_USD or liq > config.MAX_LIQUIDITY_USD:
        return False

    mc = pair.get("marketCap") or pair.get("fdv") or 0
    if mc <= 0 or mc > config.MAX_MARKET_CAP:
        return False

    age = _pair_age_hours(pair)
    if age is not None and age > config.MAX_PAIR_AGE_HOURS:
        return False

    txns = _safe(pair, "txns", "h1", default={})
    total_tx = (txns.get("buys", 0) or 0) + (txns.get("sells", 0) or 0)
    if total_tx < config.MIN_TX_COUNT_1H:
        return False

    return True


def filter_and_score(pairs: list[dict], min_score: float | None = None) -> list[dict]:
    """Filter a list of pair dicts and return scored results sorted
    descending by score. Only pairs above min_score are returned."""
    if min_score is None:
        min_score = config.MIN_ALERT_SCORE

    results = []
    for p in pairs:
        if not passes_hard_filters(p):
            continue
        result = score_pair(p)
        if result["score"] >= min_score:
            results.append(result)

    results.sort(key=lambda r: r["score"], reverse=True)
    logger.info(
        "Scored %d pairs -> %d passed hard filters -> %d above min score %.1f",
        len(pairs), sum(1 for p in pairs if passes_hard_filters(p)),
        len(results), min_score,
    )
    return results
