"""Smart Money Wallet Tracker -- copy-trade proven winners.

Monitors curated Solana wallets through the shared multi-provider RPC client.
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
import rpc_client
import storage

logger = logging.getLogger(__name__)

_fetch_warning_at: dict[str, float] = {}
_FETCH_WARNING_INTERVAL = 300
_last_wallet_signature: dict[str, str] = {}

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


def get_wallet_buy_count_recent(wallet: str, minutes: int = 60) -> int:
    """Count how many distinct tokens a wallet bought in the last N minutes."""
    conn = _connect()
    try:
        cutoff = time.time() - (minutes * 60)
        row = conn.execute(
            "SELECT COUNT(DISTINCT token_address) FROM wallet_buys WHERE wallet_address = ? AND detected_at > ?",
            (wallet, cutoff),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_total_copy_buys_recent(minutes: int = 60) -> int:
    """Count total distinct tokens copy-bought across all wallets in the last N minutes."""
    conn = _connect()
    try:
        cutoff = time.time() - (minutes * 60)
        row = conn.execute(
            "SELECT COUNT(DISTINCT token_address) FROM wallet_buys WHERE acted_on = 1 AND detected_at > ?",
            (cutoff,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def mark_buy_acted(wallet: str, token: str) -> None:
    """Mark a wallet buy as acted on (copy-traded)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE wallet_buys SET acted_on = 1 WHERE wallet_address = ? AND token_address = ?",
            (wallet, token),
        )
        conn.commit()
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


# -- Multi-provider RPC transaction fetching --

def fetch_recent_swaps(wallet_address: str, limit: int = 10) -> list[dict]:
    """Fetch new wallet swaps through standard Solana JSON-RPC."""
    options: dict[str, Any] = {"limit": limit, "commitment": "confirmed"}
    previous = _last_wallet_signature.get(wallet_address)
    if previous:
        options["until"] = previous

    signatures = rpc_client.rpc_call("getSignaturesForAddress", [wallet_address, options])
    if signatures is None:
        now = time.time()
        if now - _fetch_warning_at.get(wallet_address, 0) >= _FETCH_WARNING_INTERVAL:
            logger.warning("[WALLET] All RPC providers failed for wallet %s", wallet_address[:12])
            _fetch_warning_at[wallet_address] = now
        return []

    swaps = []
    all_fetched = True
    for sig_info in reversed(signatures):
        signature = sig_info.get("signature", "")
        if not signature or sig_info.get("err"):
            continue
        tx = rpc_client.rpc_call("getTransaction", [
            signature,
            {"encoding": "jsonParsed", "commitment": "confirmed",
             "maxSupportedTransactionVersion": 0},
        ])
        if tx is None:
            all_fetched = False
            continue
        try:
            swap = _parse_rpc_swap(tx, wallet_address, signature)
            if swap:
                swaps.append(swap)
        except (KeyError, TypeError, ValueError):
            logger.debug("[WALLET] Could not parse transaction %s", signature[:16])
    if signatures and all_fetched:
        _last_wallet_signature[wallet_address] = signatures[0].get("signature", previous or "")
    return swaps


def _token_amount(balance: dict) -> float:
    amount = balance.get("uiTokenAmount") or {}
    if amount.get("uiAmount") is not None:
        return float(amount["uiAmount"] or 0)
    raw = float(amount.get("amount", 0) or 0)
    return raw / (10 ** int(amount.get("decimals", 0) or 0))


def _parse_rpc_swap(tx: dict, wallet_address: str, signature: str = "") -> dict | None:
    """Infer a swap from parsed token and SOL balance changes."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    message = ((tx.get("transaction") or {}).get("message") or {})
    account_keys = message.get("accountKeys") or []
    keys = [k.get("pubkey", "") if isinstance(k, dict) else str(k) for k in account_keys]
    try:
        wallet_index = keys.index(wallet_address)
    except ValueError:
        return None

    pre_sol = meta.get("preBalances") or []
    post_sol = meta.get("postBalances") or []
    sol_delta = 0.0
    if wallet_index < len(pre_sol) and wallet_index < len(post_sol):
        sol_delta = (float(post_sol[wallet_index]) - float(pre_sol[wallet_index])) / 1e9

    pre_tokens = {b.get("mint", ""): _token_amount(b)
                  for b in (meta.get("preTokenBalances") or [])
                  if b.get("owner") == wallet_address and b.get("mint")}
    post_tokens = {b.get("mint", ""): _token_amount(b)
                   for b in (meta.get("postTokenBalances") or [])
                   if b.get("owner") == wallet_address and b.get("mint")}
    deltas = {mint: post_tokens.get(mint, 0) - pre_tokens.get(mint, 0)
              for mint in set(pre_tokens) | set(post_tokens)}
    positives = [(mint, delta) for mint, delta in deltas.items() if delta > 0]
    negatives = [(mint, delta) for mint, delta in deltas.items() if delta < 0]
    token_in = max(positives, key=lambda item: item[1])[0] if positives else ""
    token_out = min(negatives, key=lambda item: item[1])[0] if negatives else ""
    timestamp = tx.get("blockTime", 0) or 0

    if token_in and token_out:
        return {"token_bought": token_in, "token_sold": token_out,
                "amount_sol": 0, "signature": signature, "timestamp": timestamp}
    if token_in and sol_delta < 0:
        return {"token_bought": token_in, "token_sold": SOL_MINT,
                "amount_sol": abs(sol_delta), "signature": signature, "timestamp": timestamp}
    if token_out and sol_delta > 0:
        return {"token_bought": SOL_MINT, "token_sold": token_out,
                "amount_sol": sol_delta, "signature": signature, "timestamp": timestamp}
    return None


# -- Main Polling Loop --

async def poll_tracked_wallets(on_new_buy, on_wallet_sell=None) -> None:
    """Poll tracked wallets for buys and sells.

    on_new_buy: async callback(wallet_address, token_address, confidence, signature)
    on_wallet_sell: async callback(wallet_address, token_address, signature) or None
    """
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
                    token_bought = swap.get("token_bought", "")
                    token_sold = swap.get("token_sold", "")
                    sig = swap.get("signature", "")

                    # Detect sells: wallet sold a non-SOL token back to SOL
                    if on_wallet_sell and token_sold and token_sold != SOL_MINT and token_bought == SOL_MINT:
                        sell_dedup_key = f"sell_{token_sold}"
                        if not was_buy_already_seen(address, sell_dedup_key):
                            record_wallet_buy(address, sell_dedup_key, sig, 0)
                            logger.info("[WALLET] Sell detected: %s exited %s (sig=%s)",
                                        address[:12], token_sold[:16], sig[:16])
                            try:
                                await on_wallet_sell(address, token_sold, sig)
                            except Exception:
                                logger.exception("[WALLET] Sell callback error for %s", token_sold[:16])

                    # Detect buys: wallet received a non-SOL token
                    if not token_bought or token_bought == SOL_MINT:
                        continue

                    # Skip if we already saw this buy
                    if was_buy_already_seen(address, token_bought):
                        continue

                    # Record for confidence tracking even if already alerted normally
                    if storage.was_recently_alerted("solana", token_bought):
                        record_wallet_buy(address, token_bought, sig, 1)
                        continue

                    confidence = get_confidence_for_token(token_bought) + 1
                    record_wallet_buy(address, token_bought, sig, confidence)

                    logger.info(
                        "[WALLET] New buy detected: %s bought %s (confidence=%d, sig=%s)",
                        address[:12], token_bought[:16], confidence, sig[:16]
                    )

                    try:
                        await on_new_buy(address, token_bought, confidence, sig)
                    except Exception:
                        logger.exception("[WALLET] Buy callback error for %s", token_bought[:16])

                await asyncio.sleep(0.2)

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

BIRDEYE_TXS_URL = "https://public-api.birdeye.so/defi/txs/token"
SOLSCAN_TRANSFERS_URL = "https://api.solscan.io/v2/account/transfer"


def _get_from_url(url: str, params: dict, headers: dict | None = None, timeout: int = 10) -> requests.Response | None:
    """GET with 429 awareness — returns None if rate-limited or failed."""
    try:
        resp = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
        if resp.status_code == 429:
            return None
        resp.raise_for_status()
        return resp
    except Exception:
        return None


def _fetch_early_buyers_birdeye(token_address: str, limit: int) -> list[str] | None:
    """Fetch early buyers via Birdeye public API. Returns None if rate-limited."""
    headers = {"x-chain": "solana"}
    if config.BIRDEYE_API_KEY:
        headers["X-API-KEY"] = config.BIRDEYE_API_KEY
    resp = _get_from_url(
        BIRDEYE_TXS_URL,
        params={"address": token_address, "tx_type": "swap", "sort_type": "asc", "limit": min(limit * 2, 50)},
        headers=headers,
    )
    if resp is None:
        return None
    try:
        items = resp.json().get("data", {}).get("items") or []
    except Exception:
        return None
    buyers, seen = [], set()
    for item in items:
        addr = item.get("owner", "")
        if addr and addr not in seen:
            seen.add(addr)
            buyers.append(addr)
            if len(buyers) >= limit:
                break
    return buyers if buyers else None


def _fetch_early_buyers_solscan(token_address: str, limit: int) -> list[str] | None:
    """Fetch early buyers via Solscan public API. Returns None if rate-limited."""
    headers = {}
    if config.SOLSCAN_API_KEY:
        headers["token"] = config.SOLSCAN_API_KEY
    resp = _get_from_url(
        SOLSCAN_TRANSFERS_URL,
        params={"address": token_address, "activity_type": "ACTIVITY_SPL_TRANSFER", "page": 1, "page_size": min(limit * 2, 40), "sort_by": "block_time", "sort_order": "asc"},
        headers=headers,
    )
    if resp is None:
        return None
    try:
        items = resp.json().get("data") or []
    except Exception:
        return None
    buyers, seen = [], set()
    for item in items:
        addr = item.get("to_address", "") or item.get("toAddress", "")
        if addr and addr not in seen and addr != token_address:
            seen.add(addr)
            buyers.append(addr)
            if len(buyers) >= limit:
                break
    return buyers if buyers else None


def _fetch_early_buyers(token_address: str, limit: int = 30) -> list[str]:
    """Fetch earliest buyer wallets using Birdeye → Solscan fallbacks."""
    for provider_fn, name in [
        (_fetch_early_buyers_birdeye, "Birdeye"),
        (_fetch_early_buyers_solscan, "Solscan"),
    ]:
        result = provider_fn(token_address, limit)
        if result is not None:
            if result:
                logger.debug("[DISCOVERY] %s: got %d buyers for %s", name, len(result), token_address[:16])
            return result
        logger.debug("[DISCOVERY] %s rate-limited for %s, trying next provider", name, token_address[:16])

    logger.warning("[DISCOVERY] All providers failed for token %s", token_address[:16])
    return []


def _score_wallet(wallet_address: str) -> dict | None:
    """Evaluate a wallet's recent trading performance using Solana RPC.

    Returns {win_rate, total_trades, winning_trades, avg_return} or None.
    """
    swaps = fetch_recent_swaps(wallet_address, limit=50)
    if len(swaps) < 5:
        return None  # Too few trades to evaluate

    # Track buyâ†’sell pairs per token
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
    2. Fetch their early buyers via Birdeye/Solscan
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

        time.sleep(1.5)  # Rate limit (increased from 0.5s)

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

        # Quick score via multi-provider Solana RPC
        stats = _score_wallet(addr)
        if stats is None:
            continue

        win_rate = stats["win_rate"]
        total = stats["total_trades"]

        # Qualify: >50% win rate and appeared in 2+ winners
        if win_rate >= 60 and total >= 10:
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


# -- Auto-Prune: drop wallets whose performance decayed --

def prune_underperforming_wallets(min_win_rate: float = 40, min_trades: int = 3) -> list[dict]:
    """Re-evaluate all tracked wallets and deactivate those with degraded stats.

    A wallet is dropped if:
    - Win rate dropped below min_win_rate (default 40%)
    - OR avg return is negative
    - OR too few recent trades to evaluate (went inactive)

    Returns list of pruned wallets with reasons.
    """
    wallets = get_tracked_wallets()
    if not wallets:
        return []

    pruned = []

    for wallet in wallets:
        addr = wallet["address"]
        label = wallet.get("label") or addr[:12]
        old_wr = wallet.get("win_rate", 0) or 0

        stats = _score_wallet(addr)

        reason = None
        new_wr = None

        if stats is None:
            # Can't evaluate â€” might be inactive. Only prune if wallet is old (>7 days)
            age_days = (time.time() - wallet.get("added_at", time.time())) / 86400
            if age_days > 7:
                reason = "inactive (no recent trades)"
        else:
            new_wr = stats["win_rate"]
            avg_ret = stats["avg_return"]
            total = stats["total_trades"]

            # Update stored stats regardless
            _update_wallet_stats(addr, new_wr, total, stats["winning_trades"], avg_ret)

            if new_wr < min_win_rate and total >= min_trades:
                reason = f"win rate dropped to {new_wr:.0f}% (was {old_wr:.0f}%)"
            elif avg_ret < -15 and total >= min_trades:
                reason = f"avg return {avg_ret:+.1f}% (negative)"

        if reason:
            remove_wallet(addr)
            pruned.append({
                "address": addr,
                "label": label,
                "old_win_rate": old_wr,
                "new_win_rate": new_wr,
                "reason": reason,
            })
            logger.info("[PRUNE] Dropped wallet %s (%s): %s", addr[:12], label, reason)

        time.sleep(1)  # Rate limit

    logger.info("[PRUNE] Pruned %d/%d wallets", len(pruned), len(wallets))
    return pruned


def _update_wallet_stats(address: str, win_rate: float, total_trades: int,
                         winning_trades: int, avg_return: float) -> None:
    """Update a wallet's performance stats in DB."""
    conn = _connect()
    try:
        conn.execute(
            """UPDATE tracked_wallets
               SET win_rate = ?, total_trades = ?, winning_trades = ?,
                   avg_return_pct = ?, last_checked = ?
               WHERE address = ?""",
            (win_rate, total_trades, winning_trades, avg_return, time.time(), address),
        )
        conn.commit()
    finally:
        conn.close()


# -- Bootstrap Discovery: find alpha wallets from DexScreener top gainers --

def discover_from_trending() -> list[dict]:
    """Find alpha wallets by analyzing top gaining Solana memecoins right now.

    Unlike discover_alpha_wallets() which needs our own alert history,
    this bootstraps from DexScreener's current top gainers.

    Flow:
    1. Fetch top gaining Solana tokens from DexScreener
    2. Filter for memecoin characteristics (low MC, recent, high volume)
    3. Fetch early buyers of each via Birdeye/Solscan
    4. Score wallets appearing in 2+ different top gainers
    5. Add qualifying wallets

    Returns list of newly added wallets.
    """
    import dexscreener_client as dex

    # Step 1: Fetch trending Solana tokens from DexScreener
    logger.info("[DISCOVERY] Fetching trending Solana tokens from DexScreener...")
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            timeout=10,
        )
        resp.raise_for_status()
        boosted = resp.json()
    except Exception as e:
        logger.warning("[DISCOVERY] Failed to fetch boosted tokens: %s", e)
        boosted = []

    # Also fetch top gainers via search
    try:
        resp2 = requests.get(
            "https://api.dexscreener.com/latest/dex/search",
            params={"q": "solana"},
            timeout=10,
        )
        resp2.raise_for_status()
        search_data = resp2.json()
        search_pairs = search_data.get("pairs") or []
    except Exception:
        search_pairs = []

    # Combine and filter for Solana memecoins that pumped
    winning_tokens = []

    # From boosted tokens
    for item in (boosted if isinstance(boosted, list) else []):
        if (item.get("chainId") or "").lower() != "solana":
            continue
        addr = item.get("tokenAddress", "")
        if addr:
            winning_tokens.append(addr)

    # From search â€” pick tokens with strong recent gains
    for pair in search_pairs[:50]:
        if (pair.get("chainId") or "").lower() != "solana":
            continue
        mc = pair.get("marketCap") or pair.get("fdv") or 0
        if mc <= 0 or mc > 5_000_000:
            continue
        pc = pair.get("priceChange") or {}
        h1 = pc.get("h1", 0) or 0
        h24 = pc.get("h24", 0) or 0
        if h1 > 30 or h24 > 50:
            base = pair.get("baseToken") or {}
            addr = base.get("address", "")
            if addr and addr not in winning_tokens:
                winning_tokens.append(addr)

    winning_tokens = list(dict.fromkeys(winning_tokens))[:15]

    if not winning_tokens:
        logger.info("[DISCOVERY] No trending tokens found to analyze")
        return []

    logger.info("[DISCOVERY] Analyzing %d trending tokens for early buyers...", len(winning_tokens))

    # Step 2: Fetch early buyers for each
    wallet_hits: dict[str, list[str]] = {}

    for token_addr in winning_tokens:
        buyers = _fetch_early_buyers(token_addr, limit=15)

        symbol = token_addr[:8]
        try:
            pairs = dex.fetch_pair_details("solana", token_addr)
            if pairs:
                base = pairs[0].get("baseToken") or {}
                symbol = base.get("symbol", symbol)
        except Exception:
            pass

        for buyer in buyers:
            wallet_hits.setdefault(buyer, []).append(symbol)

        logger.debug("[DISCOVERY] $%s: %d early buyers", symbol, len(buyers))
        time.sleep(0.5)

    # Step 3: Find wallets in 2+ different pumping tokens
    candidates = {
        addr: tokens for addr, tokens in wallet_hits.items()
        if len(tokens) >= 2
    }

    if not candidates:
        logger.info("[DISCOVERY] No wallets found in 2+ trending tokens")
        return []

    logger.info("[DISCOVERY] %d candidate wallets in 2+ trending tokens", len(candidates))

    # Step 4: Score and add
    existing = {w["address"] for w in get_tracked_wallets()}
    newly_added = []
    sorted_candidates = sorted(candidates.items(), key=lambda x: len(x[1]), reverse=True)[:10]

    for addr, tokens in sorted_candidates:
        if addr in existing:
            continue

        stats = _score_wallet(addr)
        if stats is None:
            continue

        win_rate = stats["win_rate"]
        total = stats["total_trades"]

        if win_rate >= 60 and total >= 10:
            label = f"Trend-{len(tokens)}hits"
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
                "[DISCOVERY] Added trending wallet: %s (WR=%.0f%%, in: %s)",
                addr[:12], win_rate, ", ".join(tokens[:3])
            )

        time.sleep(1)

    logger.info("[DISCOVERY] Trending discovery: %d new wallets added", len(newly_added))
    return newly_added
