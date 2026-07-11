"""Honeypot / contract safety check using the GoPlus Security API.

Runs AFTER scoring but BEFORE sending a Telegram alert.
Results are cached in SQLite to respect GoPlus rate limits.
"""

import logging
import time
from typing import Any

import requests

import config
import storage

logger = logging.getLogger(__name__)

GOPLUS_BASE = "https://api.gopluslabs.io/api/v1"

# Map DexScreener chain IDs to GoPlus chain IDs (for EVM chains)
_GOPLUS_CHAIN_MAP: dict[str, str] = {
    "ethereum": "1",
    "bsc": "56",
    "base": "8453",
    "arbitrum": "42161",
    "polygon": "137",
    "avalanche": "43114",
    "optimism": "10",
}


def _is_solana(chain_id: str) -> bool:
    return chain_id.lower() == "solana"


def _fetch_goplus_solana(token_address: str) -> dict | None:
    """Fetch security info for a Solana token from GoPlus."""
    url = f"{GOPLUS_BASE}/solana/token_security"
    try:
        resp = requests.get(url, params={"contract_addresses": token_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        # GoPlus returns results keyed by address
        return result.get(token_address) or result.get(token_address.lower()) or {}
    except requests.RequestException as e:
        logger.error("GoPlus Solana request failed for %s: %s", token_address, e)
        return None


def _fetch_goplus_evm(chain_id: str, token_address: str) -> dict | None:
    """Fetch security info for an EVM token from GoPlus."""
    goplus_chain = _GOPLUS_CHAIN_MAP.get(chain_id.lower())
    if not goplus_chain:
        logger.warning("No GoPlus chain mapping for '%s' -- skipping safety check", chain_id)
        return None

    url = f"{GOPLUS_BASE}/token_security/{goplus_chain}"
    try:
        resp = requests.get(url, params={"contract_addresses": token_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        return result.get(token_address) or result.get(token_address.lower()) or {}
    except requests.RequestException as e:
        logger.error("GoPlus EVM request failed for %s on chain %s: %s", token_address, chain_id, e)
        return None


def _parse_safety_result(raw: dict, chain_id: str) -> dict[str, Any]:
    """Parse raw GoPlus response into a standardized safety result dict."""

    def _to_float(val: Any) -> float | None:
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _to_bool(val: Any) -> bool | None:
        if val is None or val == "":
            return None
        if isinstance(val, bool):
            return val
        return str(val) == "1"

    is_honeypot = _to_bool(raw.get("is_honeypot"))
    buy_tax = _to_float(raw.get("buy_tax"))
    sell_tax = _to_float(raw.get("sell_tax"))
    has_blacklist = _to_bool(raw.get("is_blacklisted")) or _to_bool(raw.get("is_in_dex"))
    can_take_back_ownership = _to_bool(raw.get("can_take_back_ownership"))

    # Mint authority (Solana-specific, but GoPlus may report for EVM too)
    mint_authority = None
    if _is_solana(chain_id):
        # Solana: check if mutable or if owner can mint
        mint_authority = _to_bool(raw.get("is_open_source"))  # inverse: if not open source, risky
        # More specific: check 'mintable' or 'owner_change_balance'
        mintable = _to_bool(raw.get("mintable"))
        if mintable is not None:
            mint_authority = mintable
    else:
        mintable = _to_bool(raw.get("owner_change_balance"))
        if mintable is not None:
            mint_authority = mintable

    # Blacklist/whitelist
    has_whitelist = _to_bool(raw.get("is_whitelisted"))
    transfer_pausable = _to_bool(raw.get("transfer_pausable"))
    if has_blacklist is None:
        has_blacklist = _to_bool(raw.get("is_blacklisted"))

    # Top holder concentration
    holders = raw.get("holders") or []
    top10_pct = None
    if isinstance(holders, list) and len(holders) > 0:
        top10 = holders[:10]
        top10_pct = sum(float(h.get("percent", 0)) * 100 for h in top10 if h.get("percent"))

    # Convert tax to percentage (GoPlus returns as decimal like 0.05 = 5%)
    if buy_tax is not None and buy_tax <= 1.0:
        buy_tax = buy_tax * 100
    if sell_tax is not None and sell_tax <= 1.0:
        sell_tax = sell_tax * 100

    return {
        "is_honeypot": is_honeypot,
        "buy_tax_pct": buy_tax,
        "sell_tax_pct": sell_tax,
        "mint_authority_active": mint_authority,
        "has_blacklist": has_blacklist or False,
        "has_whitelist": has_whitelist or False,
        "transfer_pausable": transfer_pausable or False,
        "top10_holder_pct": top10_pct,
        "checked_at": time.time(),
    }


def _compute_risk_label(safety: dict, cfg_safety: dict) -> str:
    """Compute LOW / MEDIUM / HIGH risk label based on safety results and thresholds."""
    issues = 0

    if safety.get("is_honeypot"):
        return "HIGH"

    if safety.get("mint_authority_active") and cfg_safety.get("reject_mint_authority", True):
        issues += 2

    if (safety.get("has_blacklist") or safety.get("transfer_pausable")) and cfg_safety.get("reject_blacklist", True):
        issues += 1

    buy_tax = safety.get("buy_tax_pct")
    sell_tax = safety.get("sell_tax_pct")
    if buy_tax is not None and buy_tax > cfg_safety.get("max_buy_tax_pct", 10):
        issues += 2
    if sell_tax is not None and sell_tax > cfg_safety.get("max_sell_tax_pct", 10):
        issues += 2

    top10 = safety.get("top10_holder_pct")
    if top10 is not None and top10 > cfg_safety.get("max_top10_holder_pct", 70):
        issues += 1

    if issues >= 3:
        return "HIGH"
    elif issues >= 1:
        return "MEDIUM"
    return "LOW"


def _should_reject(safety: dict, cfg_safety: dict) -> bool:
    """Return True if the token should be rejected based on safety results."""
    if safety.get("is_honeypot") and cfg_safety.get("reject_honeypot", True):
        return True

    if safety.get("mint_authority_active") and cfg_safety.get("reject_mint_authority", True):
        return True

    if (safety.get("has_blacklist") or safety.get("transfer_pausable")) and cfg_safety.get("reject_blacklist", True):
        return True

    buy_tax = safety.get("buy_tax_pct")
    if buy_tax is not None and buy_tax > cfg_safety.get("max_buy_tax_pct", 10):
        return True

    sell_tax = safety.get("sell_tax_pct")
    if sell_tax is not None and sell_tax > cfg_safety.get("max_sell_tax_pct", 10):
        return True

    top10 = safety.get("top10_holder_pct")
    if top10 is not None and top10 > cfg_safety.get("max_top10_holder_pct", 70):
        return True

    return False


def check_token_safety(chain_id: str, token_address: str) -> dict[str, Any] | None:
    """Run safety check for a token. Returns a safety result dict or None on failure.

    Uses SQLite cache to avoid re-checking within SAFETY_CHECK_CACHE_HOURS.
    """
    # Check cache first
    cached = storage.get_cached_safety_check(chain_id, token_address)
    if cached is not None:
        logger.debug("Using cached safety result for %s on %s", token_address, chain_id)
        return cached

    # Fetch from GoPlus
    if _is_solana(chain_id):
        raw = _fetch_goplus_solana(token_address)
    else:
        raw = _fetch_goplus_evm(chain_id, token_address)

    if raw is None:
        return None

    safety = _parse_safety_result(raw, chain_id)

    # Get chain safety config for risk labeling
    chain_cfg = config.get_chain_profile(chain_id)
    cfg_safety = chain_cfg.get("safety", {})
    safety["risk_label"] = _compute_risk_label(safety, cfg_safety)

    # Cache the result
    storage.cache_safety_check(chain_id, token_address, safety)

    return safety


def evaluate_safety(chain_id: str, token_address: str) -> tuple[bool, dict[str, Any] | None]:
    """Evaluate token safety. Returns (should_alert, safety_data).

    should_alert: True if the token is safe enough to alert (or if we should
                  send with a warning on failure, depending on config).
    safety_data: The safety result dict, or None if check failed.
    """
    # Skip GoPlus entirely for chains it doesn't support (not a failure, just unsupported)
    chain_lower = chain_id.lower()
    if chain_lower != "solana" and chain_lower not in _GOPLUS_CHAIN_MAP:
        logger.debug("GoPlus not available for chain '%s' -- passing through", chain_id)
        return True, None

    safety = check_token_safety(chain_id, token_address)

    if safety is None:
        # Actual API failure (not just unsupported chain)
        chain_cfg = config.get_chain_profile(chain_id)
        skip = chain_cfg.get("safety_skip_on_failure", config.SKIP_ON_SAFETY_CHECK_FAILURE)
        if skip:
            logger.warning("Safety check failed for %s on %s -- skipping (skip_on_failure=true)", token_address, chain_id)
            return False, None
        else:
            logger.warning("Safety check failed for %s on %s -- alerting with warning", token_address, chain_id)
            return True, {"risk_label": "UNKNOWN", "check_failed": True}

    # Check rejection criteria
    chain_cfg = config.get_chain_profile(chain_id)
    cfg_safety = chain_cfg.get("safety", {})

    if _should_reject(safety, cfg_safety):
        logger.info("Token %s on %s REJECTED by safety check (risk=%s)", token_address, chain_id, safety.get("risk_label"))
        return False, safety

    return True, safety
