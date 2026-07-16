"""Smart Money Wallet Tracker -- copy-trade proven winners.

Monitors a curated list of high-win-rate wallets via Helius API.
When a tracked wallet buys a new token:
  1. Checks if it passes safety filters
  2. Calculates confidence (how many tracked wallets are in)
  3. Triggers auto-buy if confidence threshold met

Wallet list stored in SQLite for persistence and stats tracking.
"""

import asyncio
import logging
import sqlite3
import time
from typing import Any

import requests

import config
import storage

logger = logging.getLogger(__name__)

# Helius Enhanced Transactions API
HELIUS_TX_URL = f"https://api.helius.xyz/v0/addresses/{{address}}/transactions?api-key={config.HELIUS_API_KEY}"
HELIUS_PARSE_URL = f"https://api.helius.xyz/v0/transactions?api-key={config.HELIUS_API_KEY}"

# Known DEX program IDs (to identify swaps)
JUPITER_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter v6
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",  # Jupiter v4
}
RAYDIUM_PROGRAMS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CPMM
}
SOL_MINT = "So11111111111111111111111111111111111111112"

# SQLite table for tracked wallets
_WALLETS_TABLE = """
CREATE TABLE IF NOT EXISTS tracked_wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,
    added_at        REAL NOT NULL,
    win_rate        REAL,
    total_trades    INTEGER DEFAULT 0,
    winning_trades  INTEGER DEFAULT 0,
    avg_return_pct  REAL,
    last_checked    REAL,
    active          INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS wallet_buys (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address  TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    detected_at     REAL NOT NULL,
    signature       TEXT,
    acted_on        INTEGER DEFAULT 0,
    confidence      INTEGER DEFAULT 1
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(storage.DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_WALLETS_TABLE)
    return conn


# -- Wallet Management --

def add_wallet(address: str, label: str = "", win_rate: float = 0) -> None:
    """Add a wallet to the tracked list."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO tracked_wallets (address, label, added_at, win_rate, active)
               VALUES (?, ?, ?, ?, 1)""",
            (address, label, time.time(), win_rate),
        )
        conn.commit()
        logger.info("[WALLET] Added tracked wallet: %s (%s)", address[:12], label)
    finally:
        conn.close()


def remove_wallet(address: str) -> None:
    """Remove a wallet from tracking."""
    conn = _connect()
    try:
        conn.execute("UPDATE tracked_wallets SET active = 0 WHERE address = ?", (address,))
        conn.commit()
    finally:
        conn.close()


def get_tracked_wallets() -> list[dict]:
    """Get all active tracked wallets."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM tracked_wallets WHERE active = 1 ORDER BY win_rate DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_wallet_count() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM tracked_wallets WHERE active = 1").fetchone()[0]
    finally:
        conn.close()


def was_buy_already_seen(wallet: str, token: str) -> bool:
    """Check if we already processed this wallet+token buy."""
    conn = _connect()
    try:
        # Only consider recent (last 24h)
        cutoff = time.time() - 86400
        row = conn.execute(
            "SELECT 1 FROM wallet_buys WHERE wallet_address = ? AND token_address = ? AND detected_at > ?",
            (wallet, token, cutoff),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def record_wallet_buy(wallet: str, token: str, signature: str, confidence: int) -> None:
    """Record that a tracked wallet bought a token."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO wallet_buys (wallet_address, token_address, detected_at, signature, confidence) VALUES (?, ?, ?, ?, ?)",
            (wallet, token, time.time(), signature, confidence),
        )
        conn.commit()
    finally:
        conn.close()


def get_confidence_for_token(token: str) -> int:
    """How many tracked wallets bought this token in the last 24h."""
    conn = _connect()
    try:
        cutoff = time.time() - 86400
        row = conn.execute(
            "SELECT COUNT(DISTINCT wallet_address) FROM wallet_buys WHERE token_address = ? AND detected_at > ?",
            (token, cutoff),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# -- Helius Transaction Fetching --

def fetch_recent_swaps(wallet_address: str, limit: int = 10) -> list[dict]:
    """Fetch recent swap transactions for a wallet using Helius parsed transactions API.

    Returns list of {token_bought, token_sold, signature, timestamp} dicts.
    """
    if not config.HELIUS_API_KEY:
        return []

    url = HELIUS_TX_URL.format(address=wallet_address)
    try:
        resp = requests.get(url, params={"limit": limit, "type": "SWAP"}, timeout=15)
        resp.raise_for_status()
        txs = resp.json()
    except Exception as e:
        logger.warning("[WALLET] Failed to fetch txs for %s: %s", wallet_address[:12], e)
        return []

    swaps = []
    for tx in txs:
        try:
            swap = _parse_helius_swap(tx, wallet_address)
            if swap:
                swaps.append(swap)
        except Exception:
            continue

    return swaps


def _parse_helius_swap(tx: dict, wallet_address: str) -> dict | None:
    """Parse a Helius enhanced transaction into a simple swap record.

    Returns {token_bought, token_sold, amount_sol, signature, timestamp} or None.
    """
    sig = tx.get("signature", "")
    timestamp = tx.get("timestamp", 0)
    tx_type = tx.get("type", "")

    if tx_type != "SWAP":
        return None

    # Look at token transfers to determine what was bought/sold
    token_transfers = tx.get("tokenTransfers") or []
    native_transfers = tx.get("nativeTransfers") or []

    tokens_in = []  # tokens received by the wallet
    tokens_out = []  # tokens sent from the wallet

    for t in token_transfers:
        mint = t.get("mint", "")
        if mint == SOL_MINT:
            continue  # skip wrapped SOL transfers, handled separately
        if t.get("toUserAccount") == wallet_address:
            tokens_in.append(mint)
        elif t.get("fromUserAccount") == wallet_address:
            tokens_out.append(mint)

    # Check native SOL transfers
    sol_spent = 0
    for nt in native_transfers:
        if nt.get("fromUserAccount") == wallet_address:
            sol_spent += nt.get("amount", 0)

    # A buy = SOL out, token in
    if tokens_in and sol_spent > 0:
        return {
            "token_bought": tokens_in[0],
            "token_sold": SOL_MINT,
            "amount_sol": sol_spent / 1e9,
            "signature": sig,
            "timestamp": timestamp,
        }

    # A sell = token out, SOL in (or another token in)
    if tokens_out and not tokens_in:
        return {
            "token_bought": SOL_MINT,
            "token_sold": tokens_out[0],
            "amount_sol": 0,
            "signature": sig,
            "timestamp": timestamp,
        }

    # Token-to-token swap
    if tokens_in and tokens_out:
        return {
            "token_bought": tokens_in[0],
            "token_sold": tokens_out[0],
            "amount_sol": 0,
            "signature": sig,
            "timestamp": timestamp,
        }

    return None


# -- Main Polling Loop --

async def poll_tracked_wallets(on_new_buy) -> None:
    """Continuously poll tracked wallets for new buys.

    Args:
        on_new_buy: async callback(wallet_address, token_address, confidence, signature)
    """
    if not config.HELIUS_API_KEY:
        logger.warning("[WALLET] No HELIUS_API_KEY -- wallet tracker disabled")
        return

    logger.info("[WALLET] Tracker started with %d wallets", get_wallet_count())

    while True:
        try:
            wallets = get_tracked_wallets()
            if not wallets:
                await asyncio.sleep(30)
                continue

            for wallet in wallets:
                address = wallet["address"]

                swaps = await asyncio.to_thread(fetch_recent_swaps, address, 5)

                for swap in swaps:
                    token = swap.get("token_bought", "")
                    sig = swap.get("signature", "")

                    # Skip sells (token_bought == SOL) and skip SOL itself
                    if not token or token == SOL_MINT:
                        continue

                    # Skip if we already saw this
                    if was_buy_already_seen(address, token):
                        continue

                    # Skip if token was already alerted by our normal flow
                    if storage.was_recently_alerted("solana", token):
                        # Still record for confidence tracking
                        record_wallet_buy(address, token, sig, 1)
                        continue

                    # Calculate confidence: how many tracked wallets bought this
                    confidence = get_confidence_for_token(token) + 1
                    record_wallet_buy(address, token, sig, confidence)

                    logger.info(
                        "[WALLET] New buy detected: %s bought %s (confidence=%d, sig=%s)",
                        address[:12], token[:16], confidence, sig[:16]
                    )

                    # Fire callback
                    try:
                        await on_new_buy(address, token, confidence, sig)
                    except Exception:
                        logger.exception("[WALLET] Callback error for %s", token[:16])

                # Rate limit: ~200ms between wallets
                await asyncio.sleep(0.2)

            # Full cycle delay: 8 seconds between complete sweeps
            await asyncio.sleep(8)

        except Exception:
            logger.exception("[WALLET] Error in polling loop")
            await asyncio.sleep(15)


# -- Seed wallets (call once to populate initial list) --

def seed_default_wallets() -> None:
    """Add a starter set of well-known profitable Solana memecoin wallets.

    These are commonly referenced smart money addresses from GMGN leaderboards.
    Users should verify and update this list via /addwallet command.
    """
    # These are placeholder addresses -- user should replace with actual
    # alpha wallets from gmgn.ai/sol/leaderboard
    starters = [
        # Format: (address, label, estimated_win_rate)
        # User can add their own via /addwallet <address> <label>
    ]
    for addr, label, wr in starters:
        add_wallet(addr, label, wr)
    if starters:
        logger.info("[WALLET] Seeded %d starter wallets", len(starters))
