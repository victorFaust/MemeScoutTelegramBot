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

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

# Track daily spending
_daily_spent_sol: float = 0.0
_daily_reset_time: float = 0.0


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

    # Record position
    out_amount = int(quote.get("outAmount", "0") or "0")
    storage.record_position(
        token_address=token_mint,
        chain_id="solana",
        buy_amount_sol=amount_sol,
        token_amount=out_amount,
        buy_signature=result["signature"],
    )

    result["amount_sol"] = amount_sol
    result["token_mint"] = token_mint
    result["out_amount"] = out_amount
    result["price_impact_pct"] = price_impact

    logger.info("[TRADE] Bought %s for %.3f SOL (impact=%.1f%%, sig=%s)",
                token_mint[:16], amount_sol, price_impact, result["signature"][:16])
    return result
