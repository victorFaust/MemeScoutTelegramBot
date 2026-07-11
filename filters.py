"""Scoring and filtering logic for memecoin pairs.

Every pair is scored 0-100. Sub-scores are weighted per the chain profile.
Thresholds are loaded per-chain from chain_config.yaml via config.get_chain_profile().
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
# All accept the chain profile dict (cfg) so they use per-chain thresholds.

def _score_liquidity(pair: dict, cfg: dict) -> float:
    liq = _safe(pair, "liquidity", "usd", default=0)
    min_liq = cfg.get("min_liquidity_usd", 5000)
    max_liq = cfg.get("max_liquidity_usd", 50000)
    if liq < min_liq or liq > max_liq:
        return 0.0
    mid = (min_liq + max_liq) / 2
    distance = abs(liq - mid) / (max_liq - min_liq)
    return max(0.0, 1.0 - distance)


def _score_market_cap(pair: dict, cfg: dict) -> float:
    """When both min and max MC are set, score based on position within band.
    Otherwise, lower MC = higher score (original logic)."""
    mc = pair.get("marketCap") or pair.get("fdv") or 0
    min_mc = cfg.get("min_market_cap", 0)
    max_mc = cfg.get("max_market_cap", 500000)
    if mc <= 0 or mc > max_mc:
        return 0.0
    if min_mc > 0 and mc < min_mc:
        return 0.0
    if min_mc > 0:
        # Band mode: center of band scores highest
        mid = (min_mc + max_mc) / 2
        half_range = (max_mc - min_mc) / 2
        distance = abs(mc - mid) / half_range
        return max(0.0, 1.0 - distance)
    # Default: lower = better
    return 1.0 - (mc / max_mc)


def _score_pair_age(pair: dict, cfg: dict) -> float:
    age_h = _pair_age_hours(pair)
    max_age = cfg.get("max_pair_age_hours", 168)
    if age_h is None or age_h > max_age:
        return 0.0
    return 1.0 - (age_h / max_age)


def _score_volume_liquidity(pair: dict, cfg: dict) -> float:
    vol = _safe(pair, "volume", "h24", default=0)
    liq = _safe(pair, "liquidity", "usd", default=0)
    if liq <= 0:
        return 0.0
    ratio = vol / liq
    min_ratio = cfg.get("min_volume_liquidity_ratio", 0.5)
    if ratio < min_ratio:
        return 0.0
    return min(ratio / 5.0, 1.0)


def _score_price_change(pair: dict, cfg: dict) -> float:
    pc = pair.get("priceChange") or {}
    h1 = pc.get("h1", 0) or 0
    h6 = pc.get("h6", 0) or 0
    min_h1 = cfg.get("min_price_change_1h", 0.0)
    min_h6 = cfg.get("min_price_change_6h", 0.0)
    if h1 < min_h1 or h6 < min_h6:
        return 0.0
    s1 = min(max(h1, 0) / 50.0, 1.0)
    s6 = min(max(h6, 0) / 50.0, 1.0)
    return (s1 + s6) / 2.0


def _score_buy_sell_ratio(pair: dict, cfg: dict) -> float:
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    buys = h1.get("buys", 0) or 0
    sells = h1.get("sells", 0) or 0
    total = buys + sells
    min_txns = cfg.get("min_txns_1h", 10)
    if total < min_txns:
        return 0.0
    ratio = buys / total
    return max(0.0, (ratio - 0.5) * 2.0)


# -- Scoring functions list (name -> function) --

_SCORE_FNS: dict[str, Any] = {
    "liquidity": _score_liquidity,
    "market_cap": _score_market_cap,
    "pair_age": _score_pair_age,
    "vol_liq_ratio": _score_volume_liquidity,
    "price_change": _score_price_change,
    "buy_sell_ratio": _score_buy_sell_ratio,
}


def score_pair(pair: dict, cfg: dict | None = None) -> dict:
    """Score a single pair using the given chain profile.
    Returns {"score": float, "breakdown": dict, "pair": dict}."""
    if cfg is None:
        chain_id = (pair.get("chainId") or "").lower()
        cfg = config.get_chain_profile(chain_id)

    weights = cfg.get("weights", {})
    breakdown: dict[str, float] = {}
    total = 0.0

    for name, fn in _SCORE_FNS.items():
        raw = fn(pair, cfg)
        breakdown[name] = round(raw, 3)
        weight = weights.get(name, 0)
        total += raw * weight

    total = round(min(total, 100.0), 1)
    return {"score": total, "breakdown": breakdown, "pair": pair}


# -- Hard-reject gate --

def passes_hard_filters(pair: dict, cfg: dict | None = None) -> bool:
    """Return False for pairs that should never be scored."""
    if cfg is None:
        chain_id = (pair.get("chainId") or "").lower()
        cfg = config.get_chain_profile(chain_id)

    liq = _safe(pair, "liquidity", "usd", default=0)
    if liq < cfg.get("min_liquidity_usd", 5000) or liq > cfg.get("max_liquidity_usd", 50000):
        return False

    mc = pair.get("marketCap") or pair.get("fdv") or 0
    min_mc = cfg.get("min_market_cap", 0)
    max_mc = cfg.get("max_market_cap", 500000)
    if mc <= 0 or mc > max_mc:
        return False
    if min_mc > 0 and mc < min_mc:
        return False

    age = _pair_age_hours(pair)
    if age is not None and age > cfg.get("max_pair_age_hours", 168):
        return False

    txns = _safe(pair, "txns", "h1", default={})
    total_tx = (txns.get("buys", 0) or 0) + (txns.get("sells", 0) or 0)
    if total_tx < cfg.get("min_txns_1h", 10):
        return False

    # Buy/sell ratio hard filter
    buys = (txns.get("buys", 0) or 0)
    sells = (txns.get("sells", 0) or 0)
    min_ratio = cfg.get("min_buy_sell_ratio", 1.0)
    if sells > 0 and (buys / sells) < min_ratio:
        return False
    elif sells == 0 and buys == 0:
        return False

    return True


def filter_and_score(pairs: list[dict], min_score: float | None = None) -> list[dict]:
    """Filter and score pairs using per-chain profiles.
    Returns scored results sorted descending by score."""
    results = []
    for p in pairs:
        chain_id = (p.get("chainId") or "").lower()
        cfg = config.get_chain_profile(chain_id)

        if not passes_hard_filters(p, cfg):
            continue

        result = score_pair(p, cfg)
        threshold = min_score if min_score is not None else cfg.get("min_alert_score", 50)
        if result["score"] >= threshold:
            results.append(result)

    results.sort(key=lambda r: r["score"], reverse=True)
    logger.info("Scored %d pairs -> %d passed filters", len(pairs), len(results))
    return results
