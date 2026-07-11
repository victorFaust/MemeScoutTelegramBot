"""Real-time Solana new pool listener via QuickNode websocket.

Monitors Pump.fun and Raydium for new pool creation events.
Alerts within seconds of pool creation, bypassing DexScreener latency.
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable

import websockets
import requests

import config
import storage

logger = logging.getLogger(__name__)

# Program IDs to monitor for new pools
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_AMM_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# Minimum initial liquidity to consider (in SOL lamports -> rough USD estimate)
_MIN_INITIAL_LIQUIDITY_SOL = 3.0  # ~$450 at current prices, filters dust pools

# Reconnection settings
_RECONNECT_DELAY = 5  # seconds
_MAX_RECONNECT_DELAY = 60
_PING_INTERVAL = 30


def _get_wss_url() -> str:
    """Get QuickNode websocket URL from config."""
    url = config.QUICKNODE_WSS_URL
    if not url:
        raise RuntimeError("QUICKNODE_WSS_URL is not set in .env")
    return url


async def _subscribe(ws, program_id: str, sub_id: int) -> None:
    """Send a logsSubscribe request for a program."""
    request = {
        "jsonrpc": "2.0",
        "id": sub_id,
        "method": "logsSubscribe",
        "params": [
            {"mentions": [program_id]},
            {"commitment": "confirmed"}
        ]
    }
    await ws.send(json.dumps(request))
    logger.info("[WS] Subscribed to program %s (id=%d)", program_id[:8], sub_id)


def _parse_pool_creation(log_data: dict) -> dict | None:
    """Parse a log notification to detect new pool creation.
    
    Returns token info dict if this is a new pool, None otherwise.
    """
    value = log_data.get("value", {})
    logs = value.get("logs", [])
    signature = value.get("signature", "")

    # Pump.fun: look for "Program log: Initialize" or pool creation patterns
    is_new_pool = False
    for log_line in logs:
        if any(kw in log_line for kw in [
            "InitializePool",
            "Initialize",
            "init_pc_amount",
            "initialize2",
            "create",
        ]):
            is_new_pool = True
            break

    if not is_new_pool:
        return None

    # Extract account keys from the transaction
    err = value.get("err")
    if err is not None:
        return None  # Failed transaction

    return {
        "signature": signature,
        "logs": logs,
        "detected_at": time.time(),
    }


async def _fetch_transaction_details(signature: str) -> dict | None:
    """Fetch full transaction details via QuickNode REST to get token addresses."""
    url = config.QUICKNODE_WSS_URL.replace("wss://", "https://").rstrip("/")
    try:
        resp = requests.post(url, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
        }, timeout=10)
        data = resp.json()
        result = data.get("result")
        if not result:
            return None
        return result
    except Exception as e:
        logger.error("[WS] Failed to fetch tx details for %s: %s", signature[:16], e)
        return None


def _extract_token_from_tx(tx_data: dict) -> dict | None:
    """Extract the new token mint address and initial liquidity from a parsed transaction."""
    meta = tx_data.get("meta", {})
    if meta.get("err") is not None:
        return None

    message = tx_data.get("transaction", {}).get("message", {})
    instructions = message.get("instructions", [])
    inner_instructions = meta.get("innerInstructions", [])

    # Look for token mint in account keys
    account_keys = message.get("accountKeys", [])

    # Find SPL token mints involved (look for initializeMint or token creation)
    token_mint = None
    for ix_group in inner_instructions:
        for ix in ix_group.get("instructions", []):
            parsed = ix.get("parsed", {})
            if isinstance(parsed, dict):
                ix_type = parsed.get("type", "")
                info = parsed.get("info", {})
                if ix_type in ("initializeMint", "initializeMint2"):
                    token_mint = info.get("mint")
                elif ix_type == "transfer" and not token_mint:
                    # Track SOL transfers to estimate liquidity
                    pass

    # Also check top-level instructions
    for ix in instructions:
        parsed = ix.get("parsed", {})
        if isinstance(parsed, dict):
            if parsed.get("type") in ("initializeMint", "initializeMint2"):
                token_mint = parsed.get("info", {}).get("mint")

    # Estimate initial liquidity from SOL balance changes
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    sol_deposited = 0
    if pre_balances and post_balances:
        # Find the largest SOL deposit (likely the pool's initial liquidity)
        for pre, post in zip(pre_balances, post_balances):
            diff = (post - pre) / 1e9  # lamports to SOL
            if diff > sol_deposited:
                sol_deposited = diff

    if not token_mint:
        # Try to find mint from account keys (heuristic: non-system, non-program addresses)
        system_programs = {
            "11111111111111111111111111111111",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            PUMP_FUN_PROGRAM,
            RAYDIUM_AMM_PROGRAM,
            "SysvarRent111111111111111111111111111111111",
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
        }
        for key in account_keys:
            addr = key.get("pubkey", key) if isinstance(key, dict) else key
            if addr not in system_programs and len(addr) > 30:
                # Heuristic: first non-system key that looks like a mint
                token_mint = addr
                break

    if not token_mint:
        return None

    return {
        "token_address": token_mint,
        "sol_deposited": sol_deposited,
        "chain_id": "solana",
    }


class PoolListener:
    """Async websocket listener for new Solana pool creation events."""

    def __init__(self, on_new_pool: Callable):
        """
        Args:
            on_new_pool: async callback(token_info: dict) called when a new pool is detected.
        """
        self._on_new_pool = on_new_pool
        self._running = False
        self._seen_signatures: set = set()  # dedup within session
        self._max_seen = 10000

    async def start(self) -> None:
        """Start listening. Reconnects automatically on disconnect."""
        self._running = True
        delay = _RECONNECT_DELAY

        while self._running:
            try:
                await self._listen()
                delay = _RECONNECT_DELAY  # reset on clean connection
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning("[WS] Connection closed: %s. Reconnecting in %ds...", e, delay)
            except Exception as e:
                logger.error("[WS] Unexpected error: %s. Reconnecting in %ds...", e, delay)

            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, _MAX_RECONNECT_DELAY)

    def stop(self) -> None:
        self._running = False

    async def _listen(self) -> None:
        """Connect to websocket and process messages."""
        url = _get_wss_url()
        logger.info("[WS] Connecting to QuickNode websocket...")

        async with websockets.connect(url, ping_interval=_PING_INTERVAL, ping_timeout=10) as ws:
            logger.info("[WS] Connected. Subscribing to Pump.fun + Raydium...")

            # Subscribe to both programs
            await _subscribe(ws, PUMP_FUN_PROGRAM, 1)
            await _subscribe(ws, RAYDIUM_AMM_PROGRAM, 2)

            async for message in ws:
                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.error("[WS] Error processing message: %s", e)

    async def _handle_message(self, data: dict) -> None:
        """Process a websocket message."""
        # Subscription confirmations
        if "result" in data and "id" in data:
            logger.debug("[WS] Subscription confirmed: id=%d", data["id"])
            return

        # Log notifications
        method = data.get("method")
        if method != "logsNotification":
            return

        params = data.get("params", {})
        result = params.get("result", {})
        pool_event = _parse_pool_creation(result)

        if pool_event is None:
            return

        sig = pool_event["signature"]
        if sig in self._seen_signatures:
            return
        self._seen_signatures.add(sig)

        # Trim seen set if too large
        if len(self._seen_signatures) > self._max_seen:
            self._seen_signatures = set(list(self._seen_signatures)[-5000:])

        logger.info("[WS] New pool detected! sig=%s", sig[:24])

        # Fetch full transaction to get token address
        tx_data = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_transaction_details, sig
        )

        if tx_data is None:
            logger.debug("[WS] Could not fetch tx details for %s", sig[:16])
            return

        token_info = _extract_token_from_tx(tx_data)
        if token_info is None:
            logger.debug("[WS] Could not extract token from tx %s", sig[:16])
            return

        # Filter: minimum SOL deposited
        if token_info["sol_deposited"] < _MIN_INITIAL_LIQUIDITY_SOL:
            logger.debug("[WS] Skipping %s -- low liquidity (%.2f SOL)",
                         token_info["token_address"][:16], token_info["sol_deposited"])
            return

        token_info["signature"] = sig
        token_info["detected_at"] = pool_event["detected_at"]

        logger.info("[WS] New pool: token=%s, liquidity=%.1f SOL",
                    token_info["token_address"][:16], token_info["sol_deposited"])

        # Call the handler
        try:
            await self._on_new_pool(token_info)
        except Exception as e:
            logger.error("[WS] Error in new pool handler: %s", e)
