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
    starters = [
        # Format: (address, label, estimated_win_rate)
    ]
    for addr, label, wr in starters:
        add_wallet(addr, label, wr)
    if starters:
        logger.info("[WALLET] Seeded %d starter wallets", len(starters))


# -- Auto-Discovery: find alpha wallets from tokens that pumped --

HELIUS_TOKEN_ACCOUNTS_URL = f"https://api.helius.xyz/v0/token-metadata?api-key={config.HELIUS_API_KEY}"
HELIUS_SIGNATURES_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"


def _fetch_early_buyers(token_address: str, limit: int = 30) -> list[str]:
    """Fetch the earliest buyer wallets for a token using Helius parsed transactions.

    Looks at the first swap transactions involving this token and extracts
    buyer wallet addresses.
    """
    if not config.HELIUS_API_KEY:
        return []

    # Use Helius enhanced transaction history for the token mint
    url = HELIUS_SIGNATURES_URL.format(address=token_address)
    try:
        resp = requests.get(url, params={
            "api-key": config.HELIUS_API_KEY,
            "limit": 100,
            "type": "SWAP",
        }, timeout=15)
        resp.raise_for_status()
        txs = resp.json()
    except Exception as e:
        logger.warning("[DISCOVERY] Failed to fetch txs for token %s: %s", token_address[:16], e)
        return []

    buyers = []
    seen = set()

    for tx in txs:
        # Look at token transfers to find who received this token (= buyers)
        token_transfers = tx.get("tokenTransfers") or []
        for t in token_transfers:
            mint = t.get("mint", "")
            if mint != token_address:
                continue
            to_addr = t.get("toUserAccount", "")
            if to_addr and to_addr not in seen and to_addr != token_address:
                seen.add(to_addr)
                buyers.append(to_addr)
                if len(buyers) >= limit:
                    return buyers

    return buyers


def _score_wallet(wallet_address: str) -> dict | None:
    """Evaluate a wallet's recent trading performance using Helius.

    Returns {win_rate, total_trades, winning_trades, avg_return} or None.
    """
    if not config.HELIUS_API_KEY:
        return None

    swaps = fetch_recent_swaps(wallet_address, limit=50)
    if len(swaps) < 5:
        return None  # Too few trades to evaluate

    # Track buy→sell pairs per token
    buys = {}  # token -> list of buy timestamps
    sells = {}  # token -> list of sell timestamps

    for swap in swaps:
        token = swap.get("token_bought", "")
        sold = swap.get("token_sold", "")

        if token and token != SOL_MINT:
            buys.setdefault(token, []).append(swap)
        if sold and sold != SOL_MINT:
            sells.setdefault(sold, []).append(swap)

    # For each token bought, check if it's still on DexScreener and if price went up
    import dexscreener_client as dex

    total = 0
    wins = 0
    returns = []

    tokens_to_check = list(buys.keys())[:20]  # Cap API calls

    for token in tokens_to_check:
        try:
            pairs = dex.fetch_pair_details("solana", token)
            if not pairs:
                continue

            pair = pairs[0]
            current_price = float(pair.get("priceUsd", 0) or 0)
            pc = pair.get("priceChange") or {}
            change_24h = pc.get("h24", 0) or 0

            total += 1
            if change_24h > 0:
                wins += 1
                returns.append(change_24h)
            else:
                returns.append(change_24h)

            time.sleep(0.3)  # Rate limit DexScreener
        except Exception:
            continue

    if total < 3:
        return None

    win_rate = (wins / total) * 100
    avg_return = sum(returns) / len(returns) if returns else 0

    return {
        "win_rate": round(win_rate, 1),
        "total_trades": total,
        "winning_trades": wins,
        "avg_return": round(avg_return, 1),
    }


def discover_alpha_wallets() -> list[dict]:
    """Auto-discover profitable wallets from tokens that pumped.

    Flow:
    1. Find tokens from alert_outcomes that gained 50%+ at 1h or 100%+ max_24h
    2. Fetch their early buyers via Helius
    3. Count how many winning tokens each wallet appeared in
    4. Score wallets that appear in 2+ winners
    5. Add qualifying wallets (WR >50%) to tracked list

    Returns list of newly added wallets with their stats.
    """
    conn = sqlite3.connect(str(storage.DB_PATH))
    conn.row_factory = sqlite3.Row

    # Step 1: Find tokens that pumped significantly
    try:
        winners = conn.execute(
            """SELECT DISTINCT token_address, token_symbol, price_at_alert, price_1h, max_price_24h
               FROM alert_outcomes
               WHERE (
                   (price_1h IS NOT NULL AND price_at_alert > 0 AND (price_1h - price_at_alert) / price_at_alert > 0.3)
                   OR
                   (max_price_24h IS NOT NULL AND price_at_alert > 0 AND (max_price_24h - price_at_alert) / price_at_alert > 0.5)
               )
               AND rugged = 0
               ORDER BY alerted_at DESC
               LIMIT 20"""
        ).fetchall()
    finally:
        conn.close()

    if not winners:
        logger.info("[DISCOVERY] No winning tokens found in outcomes yet")
        return []

    logger.info("[DISCOVERY] Found %d winning tokens to analyze", len(winners))

    # Step 2: Fetch early buyers for each winner
    wallet_hits: dict[str, list[str]] = {}  # wallet -> [token_symbols they bought early]

    for w in winners:
        token_addr = w["token_address"]
        symbol = w["token_symbol"] or token_addr[:8]

        buyers = _fetch_early_buyers(token_addr, limit=20)
        logger.debug("[DISCOVERY] $%s: found %d early buyers", symbol, len(buyers))

        for buyer in buyers:
            wallet_hits.setdefault(buyer, []).append(symbol)

        time.sleep(0.5)  # Rate limit

    # Step 3: Filter wallets that appear in 2+ winning tokens
    candidates = {
        addr: tokens for addr, tokens in wallet_hits.items()
        if len(tokens) >= 2
    }

    if not candidates:
        logger.info("[DISCOVERY] No wallets found in 2+ winners")
        return []

    logger.info("[DISCOVERY] %d candidate wallets found in 2+ winners", len(candidates))

    # Step 4: Score top candidates and add qualifying ones
    # Already tracked addresses
    existing = {w["address"] for w in get_tracked_wallets()}

    newly_added = []
    # Sort by most appearances first, cap at 10 to avoid API spam
    sorted_candidates = sorted(candidates.items(), key=lambda x: len(x[1]), reverse=True)[:10]

    for addr, tokens in sorted_candidates:
        if addr in existing:
            continue

        # Quick score via Helius
        stats = _score_wallet(addr)
        if stats is None:
            continue

        win_rate = stats["win_rate"]
        total = stats["total_trades"]

        # Qualify: >50% win rate and appeared in 2+ winners
        if win_rate >= 50 and total >= 3:
            label = f"Auto-{len(tokens)}wins"
            add_wallet(addr, label, win_rate)

            wallet_info = {
                "address": addr,
                "label": label,
                "win_rate": win_rate,
                "total_trades": total,
                "winning_trades": stats["winning_trades"],
                "avg_return": stats["avg_return"],
                "appeared_in": tokens,
            }
            newly_added.append(wallet_info)
            logger.info(
                "[DISCOVERY] Added alpha wallet: %s (WR=%.0f%%, %d trades, in %d winners: %s)",
                addr[:12], win_rate, total, len(tokens), ", ".join(tokens[:3])
            )

        time.sleep(1)  # Rate limit between wallet scores

    logger.info("[DISCOVERY] Discovery complete: %d new wallets added", len(newly_added))
    return newly_added
