"""Jupiter V6 swap executor for Solana token purchases.

Handles quoting, transaction building, signing, and submission.
Safety rails: max position size, max open positions, daily loss limit.
"""

import base64
import logging
import time
from typing import Any

import requests

import config
import storage

logger = logging.getLogger(__name__)

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# Cached SOL price
_sol_price_usd: float = 0.0
_sol_price_updated: float = 0.0

# Track daily spending
_daily_spent_sol: float = 0.0
_daily_reset_time: float = 0.0


def get_sol_price() -> float:
    """Get current SOL price in USD. Cached for 60 seconds."""
    global _sol_price_usd, _sol_price_updated
    if time.time() - _sol_price_updated < 60 and _sol_price_usd > 0:
        return _sol_price_usd
    try:
        # Use DexScreener's SOL/USDC pair price (reliable, no key needed)
        resp = requests.get(
            "https://api.dexscreener.com/latest/dex/pairs/solana/8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj",
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        pair = data.get("pair") or data.get("pairs", [{}])[0] if isinstance(data, dict) else {}
        price = float(pair.get("priceUsd", 0) or 0)
        if price > 0:
            _sol_price_usd = price
            _sol_price_updated = time.time()
        return _sol_price_usd or 170.0
    except Exception as e:
        logger.warning("[TRADE] Failed to fetch SOL price: %s", e)
        return _sol_price_usd or 170.0  # fallback


def usd_to_sol(usd_amount: float) -> float:
    """Convert USD amount to SOL."""
    price = get_sol_price()
    return usd_amount / price if price > 0 else 0.0


def get_wallet_balance() -> dict | None:
    """Get SOL balance of the trading wallet."""
    wallet = get_wallet_address()
    if not wallet:
        return None
    rpc_url = config.QUICKNODE_HTTP_URL or "https://api.mainnet-beta.solana.com"
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [wallet],
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        lamports = data.get("result", {}).get("value", 0)
        sol = lamports / LAMPORTS_PER_SOL
        usd = sol * get_sol_price()
        return {"sol": round(sol, 4), "usd": round(usd, 2)}
    except Exception as e:
        logger.warning("[TRADE] Failed to fetch wallet balance: %s", e)
        return None


def _reset_daily_if_needed() -> None:
    """Reset daily spending counter at midnight UTC."""
    global _daily_spent_sol, _daily_reset_time
    now = time.time()
    if now - _daily_reset_time > 86400:
        _daily_spent_sol = 0.0
        _daily_reset_time = now


def _get_keypair():
    """Load the trading wallet keypair from env."""
    from solders.keypair import Keypair  # type: ignore

    key_str = config.TRADING_WALLET_PRIVATE_KEY
    if not key_str:
        return None
    try:
        # Support both base58 and byte array formats
        return Keypair.from_base58_string(key_str)
    except Exception:
        try:
            import json
            key_bytes = bytes(json.loads(key_str))
            return Keypair.from_bytes(key_bytes)
        except Exception as e:
            logger.error("Failed to load trading wallet keypair: %s", e)
            return None


def get_wallet_address() -> str | None:
    """Get the public address of the trading wallet."""
    kp = _get_keypair()
    if kp is None:
        return None
    return str(kp.pubkey())


def get_quote(token_mint: str, amount_sol: float | None = None) -> dict | None:
    """Get a Jupiter swap quote for SOL -> token.
    
    Returns quote dict with expected output, price impact, route info.
    """
    if amount_sol is None:
        amount_sol = config.TRADE_AMOUNT_SOL

    amount_lamports = int(amount_sol * LAMPORTS_PER_SOL)

    try:
        resp = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": SOL_MINT,
            "outputMint": token_mint,
            "amount": str(amount_lamports),
            "slippageBps": config.TRADE_SLIPPAGE_BPS,
            "onlyDirectRoutes": "false",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            logger.error("[TRADE] Jupiter quote error: %s", data["error"])
            return None

        return data
    except requests.RequestException as e:
        logger.error("[TRADE] Jupiter quote request failed: %s", e)
        return None


def get_sell_quote(token_mint: str, token_amount: int) -> dict | None:
    """Get a Jupiter swap quote for token -> SOL (selling)."""
    try:
        resp = requests.get(JUPITER_QUOTE_URL, params={
            "inputMint": token_mint,
            "outputMint": SOL_MINT,
            "amount": str(token_amount),
            "slippageBps": config.TRADE_SLIPPAGE_BPS,
            "onlyDirectRoutes": "false",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            logger.error("[TRADE] Jupiter sell quote error: %s", data["error"])
            return None
        return data
    except requests.RequestException as e:
        logger.error("[TRADE] Jupiter sell quote request failed: %s", e)
        return None


def execute_swap(quote: dict) -> dict | None:
    """Execute a swap using a Jupiter quote.
    
    Signs the transaction with the trading wallet and submits it.
    Returns {"signature": str, "status": str} on success, None on failure.
    """
    from solders.keypair import Keypair  # type: ignore
    from solders.transaction import VersionedTransaction  # type: ignore

    kp = _get_keypair()
    if kp is None:
        logger.error("[TRADE] No trading wallet configured")
        return None

    wallet_address = str(kp.pubkey())

    # Get swap transaction from Jupiter
    try:
        resp = requests.post(JUPITER_SWAP_URL, json={
            "quoteResponse": quote,
            "userPublicKey": wallet_address,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=15)
        resp.raise_for_status()
        swap_data = resp.json()
    except requests.RequestException as e:
        logger.error("[TRADE] Jupiter swap request failed: %s", e)
        return None

    if "error" in swap_data:
        logger.error("[TRADE] Jupiter swap error: %s", swap_data["error"])
        return None

    # Deserialize and sign the transaction
    try:
        swap_tx_base64 = swap_data.get("swapTransaction")
        if not swap_tx_base64:
            logger.error("[TRADE] No swapTransaction in Jupiter response")
            return None

        tx_bytes = base64.b64decode(swap_tx_base64)
        tx = VersionedTransaction.from_bytes(tx_bytes)

        # Sign the transaction
        signed_tx = VersionedTransaction(tx.message, [kp])
        signed_bytes = bytes(signed_tx)

    except Exception as e:
        logger.error("[TRADE] Transaction signing failed: %s", e)
        return None

    # Submit to Solana RPC
    rpc_url = config.QUICKNODE_HTTP_URL or "https://api.mainnet-beta.solana.com"
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(signed_bytes).decode("utf-8"),
                {"encoding": "base64", "skipPreflight": True, "maxRetries": 3}
            ],
        }, timeout=15)
        resp.raise_for_status()
        result = resp.json()

        if "error" in result:
            logger.error("[TRADE] Transaction submit error: %s", result["error"])
            return None

        signature = result.get("result", "")
        logger.info("[TRADE] Transaction submitted: %s", signature)
        return {"signature": signature, "status": "submitted"}

    except requests.RequestException as e:
        logger.error("[TRADE] Transaction submit failed: %s", e)
        return None


def can_trade() -> tuple[bool, str]:
    """Check if trading is allowed given current safety rails.
    
    Returns (allowed, reason).
    """
    if not config.TRADING_ENABLED:
        return False, "Trading disabled (TRADING_ENABLED=false)"

    if not config.TRADING_WALLET_PRIVATE_KEY:
        return False, "No trading wallet configured"

    _reset_daily_if_needed()

    # Daily loss limit
    if _daily_spent_sol >= config.DAILY_LOSS_LIMIT_SOL:
        return False, f"Daily limit reached ({_daily_spent_sol:.2f}/{config.DAILY_LOSS_LIMIT_SOL:.2f} SOL)"

    # Max open positions
    open_positions = storage.get_open_positions_count()
    if open_positions >= config.MAX_OPEN_POSITIONS:
        return False, f"Max positions reached ({open_positions}/{config.MAX_OPEN_POSITIONS})"

    return True, "OK"


def buy_token(token_mint: str, amount_sol: float | None = None) -> dict | None:
    """Execute a buy (SOL -> token) with all safety checks.
    
    Returns trade result dict or None on failure.
    """
    global _daily_spent_sol

    if amount_sol is None:
        amount_sol = config.TRADE_AMOUNT_SOL

    logger.info("[TRADE] Buy requested: token=%s amount_sol=%.6f", token_mint[:16], amount_sol)

    # Safety checks
    allowed, reason = can_trade()
    if not allowed:
        logger.warning("[TRADE] Buy blocked: %s", reason)
        return None

    # Get quote
    quote = get_quote(token_mint, amount_sol)
    if quote is None:
        return None

    # Check price impact
    price_impact = float(quote.get("priceImpactPct", "0") or "0")
    if price_impact > 10.0:  # >10% price impact = too thin liquidity
        logger.warning("[TRADE] Buy blocked: price impact too high (%.1f%%)", price_impact)
        return None

    # Execute
    result = execute_swap(quote)
    if result is None:
        return None

    # Track spending
    _daily_spent_sol += amount_sol

    # Get entry price and MC from DexScreener at buy time
    entry_price_usd = None
    entry_mc = None
    token_symbol = ""
    try:
        import dexscreener_client as dex
        pairs = dex.fetch_pair_details("solana", token_mint)
        if pairs:
            p = pairs[0]
            entry_price_usd = float(p.get("priceUsd", 0) or 0)
            entry_mc = p.get("marketCap") or p.get("fdv") or 0
            token_symbol = (p.get("baseToken") or {}).get("symbol", "")
    except Exception as e:
        logger.debug("[TRADE] Could not fetch entry price: %s", e)

    # Record position
    out_amount = int(quote.get("outAmount", "0") or "0")
    position_recorded = True
    try:
        storage.record_position(
            token_address=token_mint,
            chain_id="solana",
            buy_amount_sol=amount_sol,
            token_amount=out_amount,
            buy_signature=result["signature"],
            entry_price_usd=entry_price_usd,
            entry_mc=entry_mc,
            token_symbol=token_symbol,
        )
    except Exception:
        position_recorded = False
        logger.exception("[TRADE] Position record failed for %s (sig=%s)", token_mint[:16], result.get("signature", "")[:16])

    result["amount_sol"] = amount_sol
    result["token_mint"] = token_mint
    result["out_amount"] = out_amount
    result["price_impact_pct"] = price_impact
    result["position_recorded"] = position_recorded

    logger.info("[TRADE] Bought %s for %.3f SOL (impact=%.1f%%, sig=%s, position_recorded=%s)",
                token_mint[:16], amount_sol, price_impact, result["signature"][:16], position_recorded)
    return result


def sell_token(position_id: int, token_mint: str, token_amount: int) -> dict | None:
    """Execute a sell (token -> SOL) for an open position.
    
    Returns trade result dict or None on failure.
    """
    if not config.TRADING_ENABLED:
        logger.warning("[TRADE] Sell blocked: trading disabled")
        return None

    if token_amount <= 0:
        logger.warning("[TRADE] Sell blocked: no token amount")
        return None

    # Get sell quote
    quote = get_sell_quote(token_mint, token_amount)
    if quote is None:
        return None

    # Execute
    result = execute_swap(quote)
    if result is None:
        return None

    # Calculate SOL received
    sol_received = int(quote.get("outAmount", "0") or "0") / LAMPORTS_PER_SOL

    # Close the position in DB
    storage.close_position(position_id, sol_received, result["signature"])

    result["sol_received"] = sol_received
    result["position_id"] = position_id
    result["token_mint"] = token_mint

    logger.info("[TRADE] Sold position #%d (%s) for %.4f SOL (sig=%s)",
                position_id, token_mint[:16], sol_received, result["signature"][:16])
    return result


def sell_partial(position_id: int, token_mint: str, token_amount: int, sell_pct: float) -> dict | None:
    """Sell a percentage of a position. Updates remaining token_amount in DB.
    
    sell_pct: 0-100, e.g. 50 = sell half
    """
    if not config.TRADING_ENABLED:
        return None

    sell_amount = int(token_amount * (sell_pct / 100))
    if sell_amount <= 0:
        return None

    quote = get_sell_quote(token_mint, sell_amount)
    if quote is None:
        return None

    result = execute_swap(quote)
    if result is None:
        return None

    sol_received = int(quote.get("outAmount", "0") or "0") / LAMPORTS_PER_SOL
    remaining = token_amount - sell_amount

    # Update position: reduce token_amount, don't close
    storage.update_position_tokens(position_id, remaining)

    result["sol_received"] = sol_received
    result["sold_amount"] = sell_amount
    result["remaining"] = remaining
    result["sell_pct"] = sell_pct

    logger.info("[TRADE] Partial sell #%d: %.0f%% sold, %.4f SOL received, %d tokens remaining",
                position_id, sell_pct, sol_received, remaining)
    return result


def check_position_pnl(position: dict) -> dict | None:
    """Check current PnL for an open position by getting a sell quote.
    
    Returns {"current_value_sol": float, "pnl_pct": float, "pnl_sol": float} or None.
    """
    token_mint = position.get("token_address", "")
    token_amount = position.get("token_amount", 0)
    buy_amount = position.get("buy_amount_sol", 0)

    if not token_mint or token_amount <= 0 or buy_amount <= 0:
        return None

    quote = get_sell_quote(token_mint, token_amount)
    if quote is None:
        return None

    current_value_sol = int(quote.get("outAmount", "0") or "0") / LAMPORTS_PER_SOL
    pnl_sol = current_value_sol - buy_amount
    pnl_pct = (pnl_sol / buy_amount) * 100 if buy_amount > 0 else 0

    return {
        "current_value_sol": current_value_sol,
        "pnl_sol": round(pnl_sol, 6),
        "pnl_pct": round(pnl_pct, 1),
    }


def confirm_transaction(signature: str) -> str:
    """Poll Solana RPC for transaction confirmation status.

    Returns one of: 'confirmed', 'finalized', 'failed', 'not_found', 'error'.
    """
    rpc_url = config.QUICKNODE_HTTP_URL or "https://api.mainnet-beta.solana.com"
    try:
        resp = requests.post(rpc_url, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignatureStatuses",
            "params": [[signature], {"searchTransactionHistory": True}],
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        statuses = data.get("result", {}).get("value", [])
        if not statuses or statuses[0] is None:
            return "not_found"
        status = statuses[0]
        if status.get("err"):
            return "failed"
        commitment = status.get("confirmationStatus", "")
        if commitment in ("finalized", "confirmed"):
            return commitment
        return "confirmed"
    except Exception as e:
        logger.warning("[TRADE] Tx confirmation check failed: %s", e)
        return "error"
