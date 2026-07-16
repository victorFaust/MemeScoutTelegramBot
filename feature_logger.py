"""ML Feature Logger -- observe mode for learning system.

Captures detailed token features at alert time and links to outcomes.
After enough samples (~200+), this data can train an XGBoost model
to predict which alerts will actually pump.

Features captured:
- Scoring breakdown (all 7 sub-scores)
- Market data (liq, MC, vol, price changes, txn counts)
- Safety data (rugcheck score, holder concentration, LP lock)
- Time context (hour of day, day of week)
- Token age
- Outcome labels added later by performance_tracker snapshots
"""

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import storage

logger = logging.getLogger(__name__)


_FEATURES_TABLE = """
CREATE TABLE IF NOT EXISTS ml_features (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address       TEXT NOT NULL,
    chain_id            TEXT NOT NULL,
    token_symbol        TEXT,
    alerted_at          REAL NOT NULL,

    -- Scoring breakdown (0-1 each)
    score_total         REAL,
    score_liquidity     REAL,
    score_market_cap    REAL,
    score_pair_age      REAL,
    score_vol_liq       REAL,
    score_price_change  REAL,
    score_buy_sell      REAL,
    score_velocity      REAL,

    -- Raw market data
    liquidity_usd       REAL,
    market_cap          REAL,
    volume_24h          REAL,
    volume_6h           REAL,
    volume_1h           REAL,
    price_usd           REAL,
    price_change_5m     REAL,
    price_change_1h     REAL,
    price_change_6h     REAL,
    price_change_24h    REAL,
    txns_1h_buys        INTEGER,
    txns_1h_sells       INTEGER,
    txns_6h_buys        INTEGER,
    txns_6h_sells       INTEGER,
    pair_age_hours      REAL,
    vol_liq_ratio       REAL,
    buy_sell_ratio_1h   REAL,

    -- Safety features
    rugcheck_score      REAL,
    lp_locked_pct       REAL,
    top_holder_pct      REAL,
    unique_buyers       INTEGER,
    risk_count          INTEGER,
    is_mint_authority   INTEGER,

    -- Context
    hour_utc            INTEGER,
    day_of_week         INTEGER,
    is_us_hours         INTEGER,

    -- Outcome (filled later by snapshot tracker)
    price_15m           REAL,
    price_1h            REAL,
    price_6h            REAL,
    price_24h           REAL,
    max_price_24h       REAL,
    rugged              INTEGER DEFAULT 0,
    outcome_label       TEXT,

    UNIQUE(token_address, chain_id, alerted_at)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(storage.DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_FEATURES_TABLE)
    return conn


def log_features(
    token_address: str,
    chain_id: str,
    token_symbol: str,
    score_result: dict,
    pair: dict,
    safety_data: dict | None = None,
) -> None:
    """Capture all features for a token at alert time.

    Args:
        score_result: output from filters.score_pair() with 'score' and 'breakdown'
        pair: raw DexScreener pair dict
        safety_data: merged safety/rugcheck/holder data dict
    """
    now = time.time()
    utc_now = datetime.now(timezone.utc)

    breakdown = score_result.get("breakdown", {})
    safety = safety_data or {}

    # Extract raw market data from pair
    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
    mc = pair.get("marketCap") or pair.get("fdv") or 0
    vol = pair.get("volume") or {}
    pc = pair.get("priceChange") or {}
    txns = pair.get("txns") or {}
    txns_1h = txns.get("h1") or {}
    txns_6h = txns.get("h6") or {}

    # Pair age
    created = pair.get("pairCreatedAt")
    pair_age_h = None
    if created:
        try:
            pair_age_h = (time.time() * 1000 - float(created)) / 3_600_000
        except (ValueError, TypeError):
            pass

    # Derived ratios
    vol_24h = vol.get("h24", 0) or 0
    vlr = vol_24h / liq if liq > 0 else 0
    buys_1h = txns_1h.get("buys", 0) or 0
    sells_1h = txns_1h.get("sells", 0) or 0
    bsr = buys_1h / sells_1h if sells_1h > 0 else float(buys_1h)

    # Time context
    hour_utc = utc_now.hour
    day_of_week = utc_now.weekday()  # 0=Monday
    is_us_hours = 1 if 14 <= hour_utc <= 21 else 0

    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO ml_features (
                token_address, chain_id, token_symbol, alerted_at,
                score_total, score_liquidity, score_market_cap, score_pair_age,
                score_vol_liq, score_price_change, score_buy_sell, score_velocity,
                liquidity_usd, market_cap, volume_24h, volume_6h, volume_1h,
                price_usd, price_change_5m, price_change_1h, price_change_6h, price_change_24h,
                txns_1h_buys, txns_1h_sells, txns_6h_buys, txns_6h_sells,
                pair_age_hours, vol_liq_ratio, buy_sell_ratio_1h,
                rugcheck_score, lp_locked_pct, top_holder_pct, unique_buyers, risk_count, is_mint_authority,
                hour_utc, day_of_week, is_us_hours
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_address, chain_id, token_symbol, now,
                score_result.get("score", 0),
                breakdown.get("liquidity", 0),
                breakdown.get("market_cap", 0),
                breakdown.get("pair_age", 0),
                breakdown.get("vol_liq_ratio", 0),
                breakdown.get("price_change", 0),
                breakdown.get("buy_sell_ratio", 0),
                breakdown.get("velocity", 0),
                liq, mc,
                vol_24h,
                vol.get("h6", 0) or 0,
                vol.get("h1", 0) or 0,
                float(pair.get("priceUsd", 0) or 0),
                pc.get("m5", 0) or 0,
                pc.get("h1", 0) or 0,
                pc.get("h6", 0) or 0,
                pc.get("h24", 0) or 0,
                buys_1h, sells_1h,
                txns_6h.get("buys", 0) or 0,
                txns_6h.get("sells", 0) or 0,
                pair_age_h, vlr, bsr,
                safety.get("rugcheck_score"),
                safety.get("lp_locked_pct"),
                safety.get("top_holder_pct"),
                safety.get("unique_buyers"),
                safety.get("risk_count"),
                1 if safety.get("is_mint_authority") else 0,
                hour_utc, day_of_week, is_us_hours,
            ),
        )
        conn.commit()
        logger.debug("[ML] Logged features for %s ($%s)", token_address[:16], token_symbol)
    except Exception:
        logger.exception("[ML] Failed to log features for %s", token_address[:16])
    finally:
        conn.close()


def update_outcome(token_address: str, chain_id: str, window: str, price: float) -> None:
    """Update the outcome price for a specific timeframe window."""
    col = f"price_{window}"
    if col not in ("price_15m", "price_1h", "price_6h", "price_24h"):
        return
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE ml_features SET {col} = ? WHERE token_address = ? AND chain_id = ? AND {col} IS NULL",
            (price, token_address, chain_id),
        )
        conn.commit()
    except Exception:
        logger.exception("[ML] Failed to update outcome")
    finally:
        conn.close()


def update_max_price(token_address: str, chain_id: str, max_price: float) -> None:
    """Update the max price seen in 24h."""
    conn = _connect()
    try:
        conn.execute(
            """UPDATE ml_features SET max_price_24h = ?
               WHERE token_address = ? AND chain_id = ?
               AND (max_price_24h IS NULL OR max_price_24h < ?)""",
            (max_price, token_address, chain_id, max_price),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def mark_rugged(token_address: str, chain_id: str) -> None:
    """Mark a token as rugged."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE ml_features SET rugged = 1, outcome_label = 'rug' WHERE token_address = ? AND chain_id = ?",
            (token_address, chain_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def label_outcomes() -> int:
    """Auto-label outcomes based on price data. Call periodically.

    Labels:
      - 'moon': max_price_24h >= 2x alert price (100%+ gain)
      - 'pump': price_1h >= +30%
      - 'neutral': -10% to +30% at 1h
      - 'dump': price_1h < -10%
      - 'rug': already marked

    Returns count of newly labeled rows.
    """
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, price_usd, price_1h, max_price_24h FROM ml_features WHERE outcome_label IS NULL AND price_1h IS NOT NULL"
        ).fetchall()

        labeled = 0
        for row in rows:
            entry = row["price_usd"] or 0
            p1h = row["price_1h"] or 0
            max_24h = row["max_price_24h"] or 0

            if entry <= 0:
                continue

            pct_1h = ((p1h - entry) / entry) * 100 if entry > 0 else 0
            pct_max = ((max_24h - entry) / entry) * 100 if entry > 0 and max_24h > 0 else 0

            if pct_max >= 100:
                label = "moon"
            elif pct_1h >= 30:
                label = "pump"
            elif pct_1h >= -10:
                label = "neutral"
            else:
                label = "dump"

            conn.execute("UPDATE ml_features SET outcome_label = ? WHERE id = ?", (label, row["id"]))
            labeled += 1

        conn.commit()
        return labeled
    finally:
        conn.close()


def get_feature_stats() -> dict:
    """Get summary stats for the ML dataset — useful for /report."""
    conn = _connect()
    try:
        total = conn.execute("SELECT COUNT(*) FROM ml_features").fetchone()[0]
        labeled = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome_label IS NOT NULL").fetchone()[0]
        moons = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome_label = 'moon'").fetchone()[0]
        pumps = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome_label = 'pump'").fetchone()[0]
        neutrals = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome_label = 'neutral'").fetchone()[0]
        dumps = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome_label = 'dump'").fetchone()[0]
        rugs = conn.execute("SELECT COUNT(*) FROM ml_features WHERE outcome_label = 'rug'").fetchone()[0]
        return {
            "total": total,
            "labeled": labeled,
            "moons": moons,
            "pumps": pumps,
            "neutrals": neutrals,
            "dumps": dumps,
            "rugs": rugs,
            "ready_for_training": labeled >= 200,
        }
    finally:
        conn.close()


def export_training_data() -> list[dict]:
    """Export labeled features as list of dicts for model training."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM ml_features WHERE outcome_label IS NOT NULL ORDER BY alerted_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
