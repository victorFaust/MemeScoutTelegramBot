"""RugCheck.xyz integration for Solana token safety verification.

Provides a second layer of rug detection beyond GoPlus, specifically
optimized for Solana pump.fun tokens. Free API, no key required.

Checks: rugcheck score, LP locked %, mint/freeze authority, insider holders, risks.
"""

import logging
import time
from typing import Any

import requests

import config
import storage

logger = logging.getLogger(__name__)

RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"


def get_rugcheck_report(token_address: str) -> dict | None:
    """Fetch the full RugCheck report for a Solana token.
    Returns None on failure."""
    # Check cache first (reuse safety_cache with a rugcheck prefix)
    cache_key = f"rugcheck:{token_address}"
    cached = storage.get_cached_safety_check("solana", cache_key)
    if cached is not None:
        return cached

    try:
        resp = requests.get(
            f"{RUGCHECK_BASE}/tokens/{token_address}/report/summary",
            timeout=10,
        )
        if resp.status_code == 404:
            logger.debug("RugCheck: token %s not found", token_address)
            return None
        resp.raise_for_status()
        data = resp.json()

        result = {
            "score": data.get("score"),
            "score_normalised": data.get("score_normalised"),
            "risks": data.get("risks", []),
            "lp_locked_pct": data.get("lpLockedPct", 0),
            "checked_at": time.time(),
        }

        # Cache for 1 hour
        storage.cache_safety_check("solana", cache_key, result)
        return result

    except requests.RequestException as e:
        logger.error("RugCheck request failed for %s: %s", token_address, e)
        return None


def get_full_report(token_address: str) -> dict | None:
    """Fetch full RugCheck report (more detailed, heavier call)."""
    try:
        resp = requests.get(
            f"{RUGCHECK_BASE}/tokens/{token_address}/report",
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error("RugCheck full report failed for %s: %s", token_address, e)
        return None


def evaluate_rugcheck(token_address: str, chain_id: str) -> tuple[bool, dict[str, Any] | None]:
    """Evaluate a token using RugCheck.

    Only works for Solana tokens. For other chains, returns (True, None) to skip.

    Returns:
        (should_alert, rugcheck_data)
        should_alert: False if token fails rug check
        rugcheck_data: dict with score/risks/lp_locked for the Telegram message
    """
    if chain_id.lower() != "solana":
        # RugCheck is Solana-only; other chains pass through
        return True, None

    report = get_rugcheck_report(token_address)
    if report is None:
        # API failure -- respect per-chain safety_skip_on_failure setting
        chain_cfg = config.get_chain_profile(chain_id)
        skip = chain_cfg.get("safety_skip_on_failure", config.SKIP_ON_SAFETY_CHECK_FAILURE)
        if skip:
            logger.warning("RugCheck unavailable for %s -- skipping (safety_skip=true)", token_address)
            return False, None
        return True, {"rugcheck_score": None, "rugcheck_unavailable": True}

    score = report.get("score_normalised", 0)
    lp_locked = report.get("lp_locked_pct", 0)
    risks = report.get("risks", [])

    # Get thresholds from config
    chain_cfg = config.get_chain_profile(chain_id)
    min_rugcheck_score = chain_cfg.get("min_rugcheck_score", 0.5)
    min_lp_locked_pct = chain_cfg.get("min_lp_locked_pct", 50)

    rugcheck_data = {
        "rugcheck_score": score,
        "lp_locked_pct": lp_locked,
        "risks": [r.get("name", r) if isinstance(r, dict) else str(r) for r in risks],
        "risk_count": len(risks),
    }

    # Reject if score is too low
    if score is not None and score < min_rugcheck_score:
        logger.info("RugCheck REJECT %s: score=%.2f (min=%.2f), risks=%d",
                    token_address, score, min_rugcheck_score, len(risks))
        return False, rugcheck_data

    # Reject if LP not sufficiently locked
    if lp_locked < min_lp_locked_pct:
        logger.info("RugCheck REJECT %s: LP locked=%.1f%% (min=%.1f%%)",
                    token_address, lp_locked, min_lp_locked_pct)
        return False, rugcheck_data

    logger.debug("RugCheck PASS %s: score=%.2f, LP=%.1f%%, risks=%d",
                 token_address, score or 0, lp_locked, len(risks))
    return True, rugcheck_data
