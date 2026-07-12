"""Solana new pool listener via HTTP RPC polling.

Uses shared RPC client (QuickNode + Shyft + public) for load balanced polling.
Polls every 30s to detect new Pump.fun pool creation transactions.
"""

import asyncio
import logging
import time
from typing import Any, Callable

import rpc_client
import config
import storage

logger = logging.getLogger(__name__)

# Program IDs to monitor for new pools
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Polling interval (seconds)
_POLL_INTERVAL = 30
_BACKOFF_INTERVAL = 60

# Minimum initial liquidity to consider (in SOL)
_MIN_INITIAL_LIQUIDITY_SOL = 3.0


def _get_recent_signatures(program_id: str, limit: int = 10, before: str | None = None) -> list[dict]:
    """Get recent transaction signatures for a program."""
    params: list = [program_id, {"limit": limit, "commitment": "confirmed"}]
    if before:
        params[1]["before"] = before
    result = rpc_client.rpc_call("getSignaturesForAddress", params, tier="public")
    return result if result else []


def _fetch_transaction(signature: str) -> dict | None:
    """Fetch a parsed transaction by signature. Uses any available provider."""
    result = rpc_client.rpc_call("getTransaction", [
        signature,
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    ])
    return result


def _is_pool_creation(tx_data: dict) -> bool:
    """Check if a transaction is a pool/token creation (not just a swap)."""
    if not tx_data:
        return False
    meta = tx_data.get("meta", {})
    if meta.get("err"):
        return False

    logs = meta.get("logMessages", [])
    for log_line in logs:
        if any(kw in log_line for kw in [
            "Program log: Instruction: Create",
            "Program log: Instruction: Initialize",
            "InitializeMint",
        ]):
            return True
    return False


def _extract_token_from_tx(tx_data: dict) -> dict | None:
    """Extract the new token mint address and initial liquidity from a parsed transaction."""
    meta = tx_data.get("meta", {})
    message = tx_data.get("transaction", {}).get("message", {})
    inner_instructions = meta.get("innerInstructions", [])
    instructions = message.get("instructions", [])
    account_keys = message.get("accountKeys", [])

    token_mint = None

    # Look for initializeMint in inner instructions
    for ix_group in inner_instructions:
        for ix in ix_group.get("instructions", []):
            parsed = ix.get("parsed", {})
            if isinstance(parsed, dict):
                ix_type = parsed.get("type", "")
                if ix_type in ("initializeMint", "initializeMint2"):
                    token_mint = parsed.get("info", {}).get("mint")
                    break
        if token_mint:
            break

    # Also check top-level instructions
    if not token_mint:
        for ix in instructions:
            parsed = ix.get("parsed", {})
            if isinstance(parsed, dict):
                if parsed.get("type") in ("initializeMint", "initializeMint2"):
                    token_mint = parsed.get("info", {}).get("mint")
                    break

    # Fallback: find non-system addresses in account keys
    if not token_mint:
        system_programs = {
            "11111111111111111111111111111111",
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            PUMP_FUN_PROGRAM,
            "SysvarRent111111111111111111111111111111111",
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
            "ComputeBudget111111111111111111111111111111",
        }
        for key in account_keys:
            addr = key.get("pubkey", key) if isinstance(key, dict) else str(key)
            if addr not in system_programs and len(addr) > 30 and addr.endswith("pump"):
                token_mint = addr
                break

    if not token_mint:
        return None

    # Estimate SOL deposited from balance changes
    pre_balances = meta.get("preBalances", [])
    post_balances = meta.get("postBalances", [])
    sol_deposited = 0.0
    if pre_balances and post_balances:
        for pre, post in zip(pre_balances, post_balances):
            diff = (post - pre) / 1e9
            if diff > sol_deposited:
                sol_deposited = diff

    return {
        "token_address": token_mint,
        "sol_deposited": sol_deposited,
        "chain_id": "solana",
    }


class PoolListener:
    """HTTP polling-based listener for new Solana pool creation events."""

    def __init__(self, on_new_pool: Callable):
        self._on_new_pool = on_new_pool
        self._running = False
        self._last_signature: str | None = None
        self._seen_signatures: set = set()
        self._current_interval = _POLL_INTERVAL
        self._consecutive_errors = 0

    async def start(self) -> None:
        """Start polling loop."""
        self._running = True
        logger.info("[POOL] Starting Pump.fun pool poller (interval=%ds)", _POLL_INTERVAL)

        # Initialize: get the latest signature so we only process NEW ones
        sigs = _get_recent_signatures(PUMP_FUN_PROGRAM, limit=1)
        if sigs:
            self._last_signature = sigs[0].get("signature")
            logger.info("[POOL] Initialized -- latest sig: %s",
                        self._last_signature[:16] if self._last_signature else "none")
        else:
            logger.warning("[POOL] Could not initialize -- will retry on next cycle")

        while self._running:
            try:
                await self._poll_cycle()
            except Exception as e:
                logger.error("[POOL] Error in poll cycle: %s", e)
                self._consecutive_errors += 1

            # Back off on repeated errors
            if self._consecutive_errors >= 3:
                self._current_interval = _BACKOFF_INTERVAL
            else:
                self._current_interval = _POLL_INTERVAL

            await asyncio.sleep(self._current_interval)

    def stop(self) -> None:
        self._running = False

    async def _poll_cycle(self) -> None:
        """Check for new Pump.fun transactions since last poll."""
        sigs = await asyncio.to_thread(
            _get_recent_signatures, PUMP_FUN_PROGRAM, 5
        )

        if not sigs:
            self._consecutive_errors += 1
            return

        # Success -- reset error counter
        self._consecutive_errors = 0

        # Process new signatures (newest first, stop at last seen)
        new_sigs = []
        for sig_info in sigs:
            sig = sig_info.get("signature", "")
            if sig == self._last_signature or sig in self._seen_signatures:
                break
            new_sigs.append(sig)

        if not new_sigs:
            return

        # Update last seen
        self._last_signature = sigs[0].get("signature")

        logger.info("[POOL] Found %d new Pump.fun transactions", len(new_sigs))

        # Process each new signature (limit to 2 per cycle to conserve QuickNode credits)
        for sig in new_sigs[:2]:
            self._seen_signatures.add(sig)
            await self._process_signature(sig)
            await asyncio.sleep(1)  # Small delay between tx fetches

        # Trim seen set
        if len(self._seen_signatures) > 5000:
            self._seen_signatures = set(list(self._seen_signatures)[-2500:])

    async def _process_signature(self, signature: str) -> None:
        """Fetch and process a single transaction."""
        tx_data = await asyncio.to_thread(_fetch_transaction, signature)
        if tx_data is None:
            return

        if not _is_pool_creation(tx_data):
            return

        token_info = _extract_token_from_tx(tx_data)
        if token_info is None:
            return

        # Filter: minimum SOL deposited
        if token_info["sol_deposited"] < _MIN_INITIAL_LIQUIDITY_SOL:
            logger.debug("[POOL] Skipping %s -- low liquidity (%.2f SOL)",
                         token_info["token_address"][:16], token_info["sol_deposited"])
            return

        token_info["signature"] = signature
        token_info["detected_at"] = time.time()

        logger.info("[POOL] New pool: token=%s, liquidity=%.1f SOL",
                    token_info["token_address"][:16], token_info["sol_deposited"])

        try:
            await self._on_new_pool(token_info)
        except Exception as e:
            logger.error("[POOL] Error in new pool handler: %s", e)
