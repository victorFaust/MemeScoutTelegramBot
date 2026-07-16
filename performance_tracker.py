"""Background job that tracks price snapshots for alerted tokens.

Runs independently of the main scanning loop. Checks alert_outcomes rows
for due snapshots (15m, 1h, 6h, 24h) and fetches current prices from DexScreener.
"""

import logging
import time
from typing import Any

import dexscreener_client as dex
import storage

logger = logging.getLogger(__name__)

# Snapshot windows: (name, seconds_after_alert)
WINDOWS = [
    ("15m", 15 * 60),
    ("1h", 60 * 60),
    ("6h", 6 * 3600),
    ("24h", 24 * 3600),
]

# Grace period: don't check until at least this many seconds after the due time
_GRACE_SECONDS = 60


def _get_current_price(chain_id: str, pair_address: str) -> float | None:
    """Fetch current price for a pair from DexScreener. Returns None if unavailable."""
    pairs = dex.get_token_pairs(chain_id, pair_address)
    if not pairs:
        # Try fetching by pair address directly
        pairs = dex.fetch_pair_details(chain_id, pair_address)

    for p in pairs:
        if p.get("pairAddress", "").lower() == pair_address.lower():
            price_usd = p.get("priceUsd")
            if price_usd:
                try:
                    return float(price_usd)
                except (ValueError, TypeError):
                    pass
            # Fallback: check priceNative or calculate from liquidity
            break

    # If we got any pair back, try its priceUsd
    if pairs:
        price_usd = pairs[0].get("priceUsd")
        if price_usd:
            try:
                return float(price_usd)
            except (ValueError, TypeError):
                pass

    return None


def _is_rugged(chain_id: str, pair_address: str) -> bool:
    """Check if a token appears to be rugged (no data or zero liquidity)."""
    pairs = dex.get_token_pairs(chain_id, pair_address)
    if not pairs:
        pairs = dex.fetch_pair_details(chain_id, pair_address)

    if not pairs:
        return True  # No data at all

    for p in pairs:
        if p.get("pairAddress", "").lower() == pair_address.lower():
            liq = (p.get("liquidity") or {}).get("usd", 0)
            if liq is not None and float(liq) > 0:
                return False
            return True

    # Check first pair
    liq = (pairs[0].get("liquidity") or {}).get("usd", 0)
    return liq is None or float(liq) <= 0


def run_snapshot_check() -> dict[str, int]:
    """Check all pending snapshots and update prices.

    Returns a summary dict with counts of updates, rugs, and errors.
    """
    now = time.time()
    pending = storage.get_pending_snapshots()
    stats = {"checked": 0, "updated": 0, "rugged": 0, "errors": 0}

    logger.info("Snapshot check: %d pending outcomes to evaluate", len(pending))

    for row in pending:
        row_id = row["id"]
        alerted_at = row["alerted_at"]
        chain_id = row["chain_id"]
        pair_address = row["pair_address"]
        price_at_alert = row["price_at_alert"]

        # Determine which windows are due
        windows_due = []
        for window_name, delay in WINDOWS:
            checked_col = f"checked_{window_name}"
            if not row.get(checked_col) and (now - alerted_at) >= (delay + _GRACE_SECONDS):
                windows_due.append(window_name)

        if not windows_due:
            continue

        stats["checked"] += 1

        try:
            # Check for rug first
            if _is_rugged(chain_id, pair_address):
                logger.info("Token %s on %s appears rugged", row.get("token_symbol", "?"), chain_id)
                storage.update_snapshot(row_id, "", None, rugged=True)
                try:
                    import feature_logger
                    feature_logger.mark_rugged(row.get("token_address", ""), chain_id)
                except Exception:
                    pass
                stats["rugged"] += 1
                continue

            # Fetch current price
            current_price = _get_current_price(chain_id, pair_address)
            if current_price is None:
                logger.warning("Could not fetch price for %s on %s (pair: %s)",
                               row.get("token_symbol", "?"), chain_id, pair_address)
                stats["errors"] += 1
                continue

            # Update max_price_24h
            max_price = row.get("max_price_24h")
            new_max = max(current_price, max_price) if max_price else current_price

            # Update each due window
            for window_name in windows_due:
                storage.update_snapshot(row_id, window_name, current_price, max_price_24h=new_max)
                stats["updated"] += 1
                logger.debug("Updated %s snapshot for %s: $%.8f",
                             window_name, row.get("token_symbol", "?"), current_price)

            # Sync ML feature outcomes
            try:
                import feature_logger
                token_addr = row.get("token_address", "")
                for window_name in windows_due:
                    feature_logger.update_outcome(token_addr, chain_id, window_name, current_price)
                feature_logger.update_max_price(token_addr, chain_id, new_max)
            except Exception:
                pass  # non-critical

            # Small delay between API calls
            time.sleep(0.5)

        except Exception as e:
            logger.error("Error processing outcome id=%d: %s", row_id, e)
            stats["errors"] += 1

    logger.info(
        "Snapshot check complete: checked=%d, updated=%d, rugged=%d, errors=%d",
        stats["checked"], stats["updated"], stats["rugged"], stats["errors"],
    )
    return stats
