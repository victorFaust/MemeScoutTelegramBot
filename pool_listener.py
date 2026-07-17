"""New Solana token discovery via RugCheck new_tokens API.

Polls RugCheck every 10 seconds for brand-new Pump.fun tokens.
Gets instant RugCheck score + LP lock status with each token.
This catches tokens BEFORE they appear on DexScreener.
"""

import asyncio
import logging
import time
from typing import Callable

import requests

import config
import storage

logger = logging.getLogger(__name__)

RUGCHECK_NEW_TOKENS_URL = "https://api.rugcheck.xyz/v1/stats/new_tokens"
RUGCHECK_SUMMARY_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"

_POLL_INTERVAL = 10  # seconds


def _fetch_new_tokens() -> list[dict]:
    """Fetch latest new tokens from RugCheck."""
    try:
        resp = requests.get(RUGCHECK_NEW_TOKENS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except requests.RequestException as e:
        logger.warning("[POOL] Failed to fetch new tokens: %s", e)
        return []


def _get_rugcheck_summary(mint: str) -> dict | None:
    """Get RugCheck safety summary for a token."""
    try:
        resp = requests.get(
            RUGCHECK_SUMMARY_URL.format(mint=mint),
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.debug("[POOL] RugCheck summary failed for %s: %s", mint[:16], e)
        return None


class PoolListener:
    """Polls RugCheck for newly created Solana tokens."""

    def __init__(self, on_new_pool: Callable):
        self._on_new_pool = on_new_pool
        self._running = False
        self._seen_mints: set = set()

    async def start(self) -> None:
        """Start polling loop."""
        self._running = True
        logger.info("[POOL] Starting new token discovery (RugCheck, interval=%ds)", _POLL_INTERVAL)

        while self._running:
            try:
                await self._poll_cycle()
            except Exception as e:
                logger.error("[POOL] Error in poll cycle: %s", e)

            await asyncio.sleep(_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def _poll_cycle(self) -> None:
        """Fetch new tokens and process unseen ones."""
        tokens = await asyncio.to_thread(_fetch_new_tokens)
        if not tokens:
            return

        new_count = 0
        for token in tokens:
            mint = token.get("mint", "")
            if not mint or mint in self._seen_mints:
                continue

            self._seen_mints.add(mint)
            new_count += 1

            # Skip if already alerted
            if storage.was_recently_alerted("solana", mint):
                continue

            symbol = token.get("symbol", "???")
            creator = token.get("creator", "")
            mint_authority = token.get("mintAuthority", "")
            freeze_authority = token.get("freezeAuthority", "")

            # Quick safety checks from token metadata
            if mint_authority:
                logger.info("[POOL] %s ($%s) -- mint authority active, skipping", mint[:16], symbol)
                continue
            if freeze_authority:
                logger.info("[POOL] %s ($%s) -- freeze authority active, skipping", mint[:16], symbol)
                continue

            # Get RugCheck score
            summary = await asyncio.to_thread(_get_rugcheck_summary, mint)
            if summary is None:
                continue

            score = summary.get("score_normalised", 0)
            if isinstance(score, (int, float)) and score > 1:
                score = score / 1000  # normalize if raw score
            lp_locked = summary.get("lpLockedPct", 0)
            risks = summary.get("risks", [])

            # Filter: minimum RugCheck score
            cfg = config.get_chain_profile("solana")
            min_score = cfg.get("min_rugcheck_score", 0.5)
            if score < min_score:
                logger.info("[POOL] %s ($%s) -- RugCheck score %.2f < %.2f, skipping",
                             mint[:16], symbol, score, min_score)
                continue

            token_info = {
                "token_address": mint,
                "chain_id": "solana",
                "symbol": symbol,
                "sol_deposited": 0,
                "detected_at": time.time(),
                "rugcheck_score": score,
                "lp_locked_pct": lp_locked,
                "risk_count": len(risks),
                "risks": [r.get("name", str(r)) if isinstance(r, dict) else str(r) for r in risks],
                "creator": creator,
            }

            logger.info("[POOL] New token: $%s (%s) | RugCheck: %.0f%% | LP: %.0f%% | risks: %d",
                        symbol, mint[:16], score * 100 if score <= 1 else score, lp_locked, len(risks))

            try:
                await self._on_new_pool(token_info)
            except Exception as e:
                logger.error("[POOL] Error in handler for %s: %s", mint[:16], e)

        if new_count > 0:
            logger.info("[POOL] Processed %d new tokens", new_count)

        # Trim seen set
        if len(self._seen_mints) > 5000:
            self._seen_mints = set(list(self._seen_mints)[-2500:])
