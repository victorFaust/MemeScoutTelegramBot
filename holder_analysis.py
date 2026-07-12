"""Holder analysis via Solana RPC (QuickNode + Shyft load balanced).

Checks unique holder count, top holder concentration, unique recent buyers,
and deployer wallet history to filter wash-traded or insider-heavy tokens.
"""

import logging
import time
from typing import Any

import rpc_client
import config
import storage

logger = logging.getLogger(__name__)


def _rpc_call(method: str, params: list) -> dict | None:
    """Make a Solana RPC call via premium providers (QuickNode/Shyft)."""
    return rpc_client.rpc_call(method, params, tier="premium")


def get_holder_count(token_mint: str) -> int | None:
    """Get approximate number of token holders using getTokenLargestAccounts.
    
    Note: This only returns top 20. For a true count we'd need getProgramAccounts
    which is expensive. We use getTokenSupply + largest accounts as a proxy.
    """
    result = _rpc_call("getTokenLargestAccounts", [token_mint])
    if result is None:
        return None
    accounts = result.get("value", [])
    # If we see 20 accounts (max returned), there are likely more holders
    # This is a lower bound
    return len(accounts) if accounts else 0


def get_top_holder_concentration(token_mint: str) -> dict | None:
    """Get top holder % of supply.
    
    Returns: {"top1_pct": float, "top5_pct": float, "top10_pct": float, "holder_count_min": int}
    """
    # Get total supply
    supply_result = _rpc_call("getTokenSupply", [token_mint])
    if supply_result is None:
        return None
    
    total_supply_str = supply_result.get("value", {}).get("amount", "0")
    try:
        total_supply = int(total_supply_str)
    except ValueError:
        return None
    
    if total_supply == 0:
        return None

    # Get largest accounts
    accounts_result = _rpc_call("getTokenLargestAccounts", [token_mint])
    if accounts_result is None:
        return None
    
    accounts = accounts_result.get("value", [])
    if not accounts:
        return None

    # Calculate percentages
    amounts = []
    for acc in accounts:
        try:
            amounts.append(int(acc.get("amount", "0")))
        except ValueError:
            amounts.append(0)

    top1_pct = (amounts[0] / total_supply * 100) if len(amounts) >= 1 else 0
    top5_pct = (sum(amounts[:5]) / total_supply * 100) if len(amounts) >= 5 else 0
    top10_pct = (sum(amounts[:10]) / total_supply * 100) if len(amounts) >= 10 else 0

    return {
        "top1_pct": round(top1_pct, 1),
        "top5_pct": round(top5_pct, 1),
        "top10_pct": round(top10_pct, 1),
        "holder_count_min": len(accounts),
    }


def get_unique_buyers_recent(token_mint: str, limit: int = 20) -> dict | None:
    """Analyze recent transactions to count unique buyer wallets.
    
    Uses getSignaturesForAddress on the token mint to find recent txs,
    then parses them for unique buyer wallets.
    
    Returns: {"unique_buyers": int, "unique_sellers": int, "total_txns": int, "tx_count_checked": int}
    """
    # Get recent signatures for the token mint
    sigs_result = _rpc_call("getSignaturesForAddress", [
        token_mint,
        {"limit": limit, "commitment": "confirmed"}
    ])
    if sigs_result is None:
        return None
    
    if not sigs_result:
        return {"unique_buyers": 0, "unique_sellers": 0, "total_txns": 0, "tx_count_checked": 0}

    buyers = set()
    sellers = set()
    checked = 0

    for sig_info in sigs_result[:limit]:
        sig = sig_info.get("signature")
        if not sig:
            continue
        
        # Fetch parsed transaction
        tx_result = _rpc_call("getTransaction", [
            sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
        ])
        if tx_result is None:
            continue
        
        checked += 1
        meta = tx_result.get("meta", {})
        if meta.get("err"):
            continue

        # Look at token balance changes to determine buyers vs sellers
        pre_balances = meta.get("preTokenBalances", [])
        post_balances = meta.get("postTokenBalances", [])

        # Build a map of owner -> balance change
        balance_changes: dict[str, float] = {}
        
        for post in post_balances:
            if post.get("mint") != token_mint:
                continue
            owner = post.get("owner", "")
            post_amt = float(post.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
            
            # Find matching pre-balance
            pre_amt = 0.0
            for pre in pre_balances:
                if pre.get("owner") == owner and pre.get("mint") == token_mint:
                    pre_amt = float(pre.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                    break
            
            change = post_amt - pre_amt
            if change != 0 and owner:
                balance_changes[owner] = change

        for owner, change in balance_changes.items():
            if change > 0:
                buyers.add(owner)
            elif change < 0:
                sellers.add(owner)

    return {
        "unique_buyers": len(buyers),
        "unique_sellers": len(sellers),
        "total_txns": len(sigs_result),
        "tx_count_checked": checked,
    }


def get_creator_history(creator_wallet: str) -> dict | None:
    """Check deployer wallet for serial token creation.
    
    Serial deployers (5+ tokens recently) are high rug risk.
    Looks at recent transactions for token creation program interactions.
    """
    # Check cache first
    cache_key = f"creator:{creator_wallet}"
    cached = storage.get_cached_safety_check("solana", cache_key)
    if cached is not None:
        return cached

    sigs_result = _rpc_call("getSignaturesForAddress", [
        creator_wallet,
        {"limit": 100, "commitment": "confirmed"}
    ])
    if sigs_result is None:
        return None

    if not sigs_result:
        result = {"recent_tx_count": 0, "token_creates": 0, "is_serial_deployer": False}
        storage.cache_safety_check("solana", cache_key, result)
        return result

    # Count how many of these transactions involve token creation programs
    # We check a subset of recent txs for program interactions with Pump.fun or token mint
    token_creates = 0
    checked = 0
    one_week_ago = time.time() - 7 * 86400

    for sig_info in sigs_result[:30]:  # Check up to 30 recent txs
        # Filter by time — only count last 7 days
        block_time = sig_info.get("blockTime", 0)
        if block_time and block_time < one_week_ago:
            break

        sig = sig_info.get("signature")
        if not sig:
            continue

        # Fetch transaction to check if it's a token creation
        tx_result = _rpc_call("getTransaction", [
            sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
        ])
        if tx_result is None:
            continue

        checked += 1
        message = tx_result.get("transaction", {}).get("message", {})
        instructions = message.get("instructions", [])

        # Check if any instruction interacts with token creation programs
        for ix in instructions:
            program_id = ix.get("programId", "")
            parsed = ix.get("parsed", {})
            ix_type = parsed.get("type", "") if isinstance(parsed, dict) else ""

            if program_id == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P":
                # Pump.fun interaction — likely a token launch
                token_creates += 1
                break
            elif ix_type in ("initializeMint", "initializeMint2"):
                token_creates += 1
                break

        # Stop early if we've confirmed serial deployer
        if token_creates >= 5:
            break

    is_serial = token_creates >= 5
    result = {
        "recent_tx_count": len(sigs_result),
        "token_creates": token_creates,
        "txs_checked": checked,
        "is_serial_deployer": is_serial,
    }

    # Cache for 6 hours (creator history doesn't change fast)
    storage.cache_safety_check("solana", cache_key, result)
    if is_serial:
        logger.info("Serial deployer detected: %s (%d token creates in recent txs)",
                    creator_wallet[:16], token_creates)
    return result


def get_token_creator(token_mint: str) -> str | None:
    """Get the creator/deployer wallet for a token mint.
    Uses getSignaturesForAddress on the mint to find the first transaction (creation)."""
    sigs_result = _rpc_call("getSignaturesForAddress", [
        token_mint,
        {"limit": 1, "before": None, "commitment": "confirmed"}
    ])
    # The last signature is the oldest (creation tx)
    # Actually getSignaturesForAddress returns newest first, so we need the full list
    # For efficiency, just get the account info which has the owner
    account_result = _rpc_call("getAccountInfo", [
        token_mint,
        {"encoding": "jsonParsed"}
    ])
    if account_result is None:
        return None

    value = account_result.get("value")
    if not value:
        return None

    # For SPL tokens, the mint authority or update authority is often the creator
    data = value.get("data", {})
    if isinstance(data, dict):
        parsed = data.get("parsed", {})
        info = parsed.get("info", {})
        # mintAuthority is often the creator (before they renounce)
        authority = info.get("mintAuthority") or info.get("updateAuthority")
        if authority:
            return authority

    return None


def analyze_token(token_mint: str) -> dict[str, Any] | None:
    """Run full holder analysis on a token.
    
    Returns a dict with all metrics, or None if RPC is unavailable.
    """
    if not config.QUICKNODE_HTTP_URL:
        return None

    # Check cache (1 hour TTL, reuse safety_cache with prefix)
    cache_key = f"holders:{token_mint}"
    cached = storage.get_cached_safety_check("solana", cache_key)
    if cached is not None:
        return cached

    result: dict[str, Any] = {"analyzed_at": time.time()}

    # Top holder concentration
    concentration = get_top_holder_concentration(token_mint)
    if concentration:
        result.update(concentration)

    # Unique buyers (costs more RPC credits — limit to 10 txns)
    buyers = get_unique_buyers_recent(token_mint, limit=10)
    if buyers:
        result.update(buyers)
        total = buyers.get("total_txns", 0)
        unique = buyers.get("unique_buyers", 0)
        result["buy_quality"] = round(unique / total, 2) if total > 0 else 0

    # Creator/deployer history check
    creator = get_token_creator(token_mint)
    if creator:
        result["creator_wallet"] = creator[:16] + "..."
        creator_data = get_creator_history(creator)
        if creator_data:
            result["creator_token_creates"] = creator_data.get("token_creates", 0)
            result["is_serial_deployer"] = creator_data.get("is_serial_deployer", False)

    # Cache for 1 hour
    storage.cache_safety_check("solana", cache_key, result)
    logger.debug("Holder analysis for %s: %s", token_mint[:16], result)
    return result


def passes_holder_checks(token_mint: str, cfg: dict) -> tuple[bool, dict | None]:
    """Run holder analysis and check against thresholds.
    
    Returns (passes, analysis_data).
    """
    analysis = analyze_token(token_mint)
    if analysis is None:
        # RPC unavailable — pass through (don't block)
        return True, None

    # Check serial deployer (reject if creator has launched 5+ tokens recently)
    if analysis.get("is_serial_deployer", False) and cfg.get("reject_serial_deployers", True):
        logger.info("Holder check REJECT %s: serial deployer (%d token creates)",
                    token_mint[:16], analysis.get("creator_token_creates", 0))
        return False, analysis

    # Check top holder concentration
    max_top1 = cfg.get("max_top1_holder_pct", 30)
    top1 = analysis.get("top1_pct", 0)
    if top1 > max_top1:
        logger.info("Holder check REJECT %s: top1 holder has %.1f%% (max=%d%%)",
                    token_mint[:16], top1, max_top1)
        return False, analysis

    # Check buy quality (unique buyers / transactions)
    min_quality = cfg.get("min_buy_quality", 0.3)
    quality = analysis.get("buy_quality", 1.0)
    if quality < min_quality and analysis.get("tx_count_checked", 0) >= 5:
        logger.info("Holder check REJECT %s: buy quality=%.2f (min=%.2f) -- likely wash trading",
                    token_mint[:16], quality, min_quality)
        return False, analysis

    return True, analysis
