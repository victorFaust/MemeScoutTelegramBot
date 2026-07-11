"""SQLite-backed dedup tracking and safety check caching."""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "alerts.db")))

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS alerted_tokens (
    token_address TEXT NOT NULL,
    chain_id      TEXT NOT NULL,
    alerted_at    REAL NOT NULL,
    score         REAL,
    PRIMARY KEY (token_address, chain_id)
);

CREATE TABLE IF NOT EXISTS safety_cache (
    token_address TEXT NOT NULL,
    chain_id      TEXT NOT NULL,
    checked_at    REAL NOT NULL,
    result_json   TEXT NOT NULL,
    PRIMARY KEY (token_address, chain_id)
);

CREATE TABLE IF NOT EXISTS alert_outcomes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address       TEXT NOT NULL,
    chain_id            TEXT NOT NULL,
    pair_address        TEXT NOT NULL,
    token_symbol        TEXT,
    alerted_at          REAL NOT NULL,
    score_at_alert      REAL NOT NULL,
    price_at_alert      REAL,
    liquidity_at_alert  REAL,
    market_cap_at_alert REAL,
    price_15m           REAL,
    price_1h            REAL,
    price_6h            REAL,
    price_24h           REAL,
    max_price_24h       REAL,
    checked_15m         INTEGER DEFAULT 0,
    checked_1h          INTEGER DEFAULT 0,
    checked_6h          INTEGER DEFAULT 0,
    checked_24h         INTEGER DEFAULT 0,
    rugged              INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS token_metrics (
    token_address   TEXT NOT NULL,
    chain_id        TEXT NOT NULL,
    pair_address    TEXT NOT NULL,
    recorded_at     REAL NOT NULL,
    vol_liq_ratio   REAL,
    buy_sell_ratio  REAL,
    score           REAL,
    PRIMARY KEY (token_address, chain_id)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_CREATE_TABLES)
    return conn


def was_recently_alerted(chain_id: str, token_address: str) -> bool:
    """Return True if the token was alerted within the cooldown window."""
    cutoff = time.time() - config.DEDUP_COOLDOWN_HOURS * 3600
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM alerted_tokens WHERE token_address = ? AND chain_id = ? AND alerted_at > ?",
            (token_address, chain_id, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_alert(chain_id: str, token_address: str, score: float) -> None:
    """Upsert an alert record for the token."""
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO alerted_tokens (token_address, chain_id, alerted_at, score)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(token_address, chain_id)
               DO UPDATE SET alerted_at = excluded.alerted_at, score = excluded.score""",
            (token_address, chain_id, now, score),
        )
        conn.commit()
        logger.debug("Recorded alert for %s on %s (score %.1f)", token_address, chain_id, score)
    finally:
        conn.close()


def cleanup_old_records(days: int = 30) -> int:
    """Delete records older than *days*. Returns count deleted."""
    cutoff = time.time() - days * 86400
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM alerted_tokens WHERE alerted_at < ?", (cutoff,))
        conn.execute("DELETE FROM safety_cache WHERE checked_at < ?", (cutoff,))
        conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info("Cleaned up %d old alert records", deleted)
        return deleted
    finally:
        conn.close()


# -- Safety check cache --

def get_cached_safety_check(chain_id: str, token_address: str) -> dict[str, Any] | None:
    """Return cached safety result if still fresh, else None."""
    cutoff = time.time() - config.SAFETY_CHECK_CACHE_HOURS * 3600
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT result_json FROM safety_cache WHERE token_address = ? AND chain_id = ? AND checked_at > ?",
            (token_address, chain_id, cutoff),
        ).fetchone()
        if row:
            return json.loads(row[0])
        return None
    finally:
        conn.close()


def cache_safety_check(chain_id: str, token_address: str, result: dict[str, Any]) -> None:
    """Cache a safety check result."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO safety_cache (token_address, chain_id, checked_at, result_json)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(token_address, chain_id)
               DO UPDATE SET checked_at = excluded.checked_at, result_json = excluded.result_json""",
            (token_address, chain_id, time.time(), json.dumps(result)),
        )
        conn.commit()
    finally:
        conn.close()


# -- Alert outcomes (performance tracking) --

def record_outcome(
    token_address: str,
    chain_id: str,
    pair_address: str,
    token_symbol: str,
    score: float,
    price: float | None,
    liquidity: float | None,
    market_cap: float | None,
) -> None:
    """Record an alert outcome row when an alert is sent."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO alert_outcomes
               (token_address, chain_id, pair_address, token_symbol, alerted_at,
                score_at_alert, price_at_alert, liquidity_at_alert, market_cap_at_alert)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token_address, chain_id, pair_address, token_symbol, time.time(),
             score, price, liquidity, market_cap),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_snapshots() -> list[dict]:
    """Get outcome rows that have unchecked snapshots due."""
    now = time.time()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT * FROM alert_outcomes
               WHERE (checked_24h = 0 AND rugged = 0)
               ORDER BY alerted_at ASC""",
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_snapshot(
    row_id: int,
    window: str,
    price: float | None,
    rugged: bool = False,
    max_price_24h: float | None = None,
) -> None:
    """Update a specific snapshot window for an outcome row."""
    conn = _connect()
    try:
        if rugged:
            conn.execute(
                "UPDATE alert_outcomes SET rugged = 1 WHERE id = ?", (row_id,)
            )
        elif price is not None:
            price_col = f"price_{window}"
            checked_col = f"checked_{window}"
            # Update price and checked flag
            conn.execute(
                f"UPDATE alert_outcomes SET {price_col} = ?, {checked_col} = 1 WHERE id = ?",
                (price, row_id),
            )
            # Update max_price_24h if applicable
            if max_price_24h is not None:
                conn.execute(
                    """UPDATE alert_outcomes SET max_price_24h = ?
                       WHERE id = ? AND (max_price_24h IS NULL OR max_price_24h < ?)""",
                    (max_price_24h, row_id, max_price_24h),
                )
        conn.commit()
    finally:
        conn.close()


def get_outcomes_for_report(days: int = 7) -> list[dict]:
    """Get all outcome rows from the last N days for reporting."""
    cutoff = time.time() - days * 86400
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM alert_outcomes WHERE alerted_at > ? ORDER BY alerted_at DESC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# -- Token metrics (velocity tracking) --

def get_previous_metrics(token_address: str, chain_id: str) -> dict | None:
    """Get the previously recorded metrics for a token."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM token_metrics WHERE token_address = ? AND chain_id = ?",
            (token_address, chain_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_metrics(
    token_address: str, chain_id: str, pair_address: str,
    vol_liq_ratio: float, buy_sell_ratio: float, score: float,
) -> None:
    """Store/update current cycle metrics for velocity comparison."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO token_metrics (token_address, chain_id, pair_address, recorded_at, vol_liq_ratio, buy_sell_ratio, score)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_address, chain_id)
               DO UPDATE SET pair_address = excluded.pair_address, recorded_at = excluded.recorded_at,
                   vol_liq_ratio = excluded.vol_liq_ratio, buy_sell_ratio = excluded.buy_sell_ratio, score = excluded.score""",
            (token_address, chain_id, pair_address, time.time(), vol_liq_ratio, buy_sell_ratio, score),
        )
        conn.commit()
    finally:
        conn.close()


def get_previous_alert_score(chain_id: str, token_address: str) -> float | None:
    """Get the score from the last alert for this token (for momentum re-alert)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT score FROM alerted_tokens WHERE token_address = ? AND chain_id = ?",
            (token_address, chain_id),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def cleanup_stale_metrics(hours: int = 24) -> None:
    """Remove metrics older than N hours (tokens no longer showing up)."""
    cutoff = time.time() - hours * 3600
    conn = _connect()
    try:
        conn.execute("DELETE FROM token_metrics WHERE recorded_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()
