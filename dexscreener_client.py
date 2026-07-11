"""Wrapper around the free DexScreener API."""

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
_session = requests.Session()
_session.headers.update({"Accept": "application/json"})

# Simple rate-limit / back-off state
_last_request_ts: float = 0.0
_MIN_INTERVAL: float = 1.0  # seconds between requests


def _get(url: str, params: dict | None = None, retries: int = 3) -> Any:
    """GET with retry + exponential back-off."""
    global _last_request_ts
    for attempt in range(1, retries + 1):
        elapsed = time.time() - _last_request_ts
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        try:
            _last_request_ts = time.time()
            resp = _session.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("Rate-limited (429). Backing off %ss...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.error("Request failed (%s/%s): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    return None


# -- Discovery endpoints --

def get_latest_token_boosts() -> list[dict]:
    """Return the latest boosted tokens (recently promoted / trending)."""
    data = _get(f"{BASE}/token-boosts/latest/v1")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("tokens", data.get("data", []))
    return []


def get_latest_token_profiles() -> list[dict]:
    """Return the latest token profiles."""
    data = _get(f"{BASE}/token-profiles/latest/v1")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("tokens", data.get("data", []))
    return []


# -- Detail endpoints --

def get_token_pairs(chain_id: str, token_address: str) -> list[dict]:
    """Get all pairs for a specific token on a chain."""
    data = _get(f"{BASE}/token-pairs/v1/{chain_id}/{token_address}")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("pairs", data.get("data", []))
    return []


def search_pairs(query: str) -> list[dict]:
    """Search for pairs by token name/symbol/address."""
    data = _get(f"{BASE}/latest/dex/search", params={"q": query})
    if isinstance(data, dict):
        return data.get("pairs", [])
    return []


# -- Aggregated discovery --

def discover_tokens(chains: list[str]) -> list[dict]:
    """Discover new/trending tokens across multiple chains.

    Returns a list of {"chainId": ..., "tokenAddress": ...} dicts
    from the boost + profile endpoints, filtered to requested chains.
    """
    chain_set = {c.lower() for c in chains}
    seen: set[str] = set()
    tokens: list[dict] = []

    for item in get_latest_token_boosts() + get_latest_token_profiles():
        chain = (item.get("chainId") or "").lower()
        addr = item.get("tokenAddress") or ""
        if not chain or not addr:
            continue
        if chain not in chain_set:
            continue
        key = f"{chain}:{addr}"
        if key in seen:
            continue
        seen.add(key)
        tokens.append({"chainId": chain, "tokenAddress": addr})

    logger.info("Discovered %d tokens on chains %s", len(tokens), chains)
    return tokens


def fetch_pair_details(chain_id: str, token_address: str) -> list[dict]:
    """Fetch full pair data for a token. Returns list of pair dicts."""
    pairs = get_token_pairs(chain_id, token_address)
    if not pairs:
        logger.debug("No pairs found for %s on %s", token_address, chain_id)
    return pairs
