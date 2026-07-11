"""SQLite-backed dedup tracking for alerted tokens."""

import logging
import sqlite3
import time
from pathlib import Path

import config

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "alerts.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS alerted_tokens (
    token_address TEXT NOT NULL,
    chain_id      TEXT NOT NULL,
    alerted_at    REAL NOT NULL,
    score         REAL,
    PRIMARY KEY (token_address, chain_id)
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(_CREATE_TABLE)
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
        conn.commit()
        deleted = cur.rowcount
        if deleted:
            logger.info("Cleaned up %d old alert records", deleted)
        return deleted
    finally:
        conn.close()
