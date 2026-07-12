"""Shared Solana RPC client with multi-provider load balancing.

Distributes calls across QuickNode, Shyft, and public RPCs.
Auto-rotates on rate limit (429) errors.
"""

import logging
import time
from typing import Any

import requests

import config

logger = logging.getLogger(__name__)

# Provider tracking
_providers: list[dict] = []
_current_index: int = 0
_initialized: bool = False

# Rate limit tracking per provider
_provider_cooldowns: dict[str, float] = {}
_COOLDOWN_SECONDS = 30


def _init_providers() -> None:
    """Initialize the provider list from config."""
    global _providers, _initialized
    _providers = []

    # Premium providers (QuickNode, Shyft) — used for heavy calls
    if config.QUICKNODE_HTTP_URL:
        _providers.append({
            "name": "QuickNode",
            "url": config.QUICKNODE_HTTP_URL,
            "tier": "premium",
        })
    if config.SHYFT_HTTP_URL:
        _providers.append({
            "name": "Shyft",
            "url": config.SHYFT_HTTP_URL,
            "tier": "premium",
        })

    # Public RPCs — used for lightweight polling
    _providers.append({
        "name": "Solana Public",
        "url": "https://api.mainnet-beta.solana.com",
        "tier": "public",
    })

    _initialized = True
    names = [p["name"] for p in _providers]
    logger.info("[RPC] Initialized %d providers: %s", len(_providers), ", ".join(names))


def _get_provider(tier: str | None = None) -> dict | None:
    """Get the next available provider, skipping rate-limited ones."""
    global _current_index

    if not _initialized:
        _init_providers()

    now = time.time()
    tried = 0

    while tried < len(_providers):
        idx = _current_index % len(_providers)
        provider = _providers[idx]
        _current_index += 1
        tried += 1

        # Skip if on cooldown
        cooldown_until = _provider_cooldowns.get(provider["name"], 0)
        if now < cooldown_until:
            continue

        # Skip if tier doesn't match (when specified)
        if tier and provider["tier"] != tier:
            continue

        return provider

    # All providers on cooldown — return the first matching tier anyway
    for p in _providers:
        if tier is None or p["tier"] == tier:
            return p

    return _providers[0] if _providers else None


def _mark_rate_limited(provider_name: str) -> None:
    """Put a provider on cooldown after a 429."""
    _provider_cooldowns[provider_name] = time.time() + _COOLDOWN_SECONDS
    logger.warning("[RPC] %s rate-limited — cooldown %ds", provider_name, _COOLDOWN_SECONDS)


def rpc_call(method: str, params: list, tier: str | None = None) -> Any:
    """Make a Solana RPC call with automatic provider rotation.
    
    Args:
        method: RPC method name
        params: RPC params list
        tier: "premium" for QuickNode/Shyft, "public" for free RPCs, None for any
    
    Returns: RPC result or None on failure
    """
    if not _initialized:
        _init_providers()

    # Try up to 3 providers
    for attempt in range(min(3, len(_providers))):
        provider = _get_provider(tier)
        if provider is None:
            logger.error("[RPC] No available providers")
            return None

        try:
            resp = requests.post(provider["url"], json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params,
            }, timeout=15)

            if resp.status_code == 429:
                _mark_rate_limited(provider["name"])
                continue

            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                err_msg = data.get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", str(err_msg))
                logger.debug("[RPC] %s error (%s): %s", provider["name"], method, err_msg)
                return None

            return data.get("result")

        except requests.RequestException as e:
            logger.warning("[RPC] %s request failed (%s): %s", provider["name"], method, e)
            continue

    logger.error("[RPC] All providers failed for %s", method)
    return None


def get_provider_status() -> list[dict]:
    """Get status of all providers (for /status command)."""
    if not _initialized:
        _init_providers()

    now = time.time()
    status = []
    for p in _providers:
        cooldown = _provider_cooldowns.get(p["name"], 0)
        status.append({
            "name": p["name"],
            "tier": p["tier"],
            "available": now >= cooldown,
            "cooldown_remaining": max(0, cooldown - now),
        })
    return status
