"""Entry point -- per-chain scheduled polling with independent intervals."""

import asyncio
import logging
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import config
import dexscreener_client as dex
import filters
import holder_analysis
import performance_tracker
import pool_listener
import rugcheck
import safety_check
import startup_check
import storage
import telegram_notifier as tg


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


logger = logging.getLogger(__name__)


# -- Rate limit guard --
# Rolling window of API call timestamps (shared across all chain jobs)
_api_call_log: deque = deque()
_RATE_LIMIT_WINDOW = 60       # seconds
_MAX_CALLS_PER_WINDOW = 250   # headroom under DexScreener 300/min limit

# Priority: lower = higher priority (runs first when throttled)
_CHAIN_PRIORITY: dict[str, int] = {
    "robinhood": 1,
    "solana": 2,
}

# Per-chain last-run tracking
_chain_last_run: dict[str, float] = {}


def _purge_old_calls() -> None:
    """Remove API call timestamps older than the rolling window."""
    cutoff = time.time() - _RATE_LIMIT_WINDOW
    while _api_call_log and _api_call_log[0] < cutoff:
        _api_call_log.popleft()


def _calls_in_window() -> int:
    """Count API calls in the current rolling window."""
    _purge_old_calls()
    return len(_api_call_log)


def _can_make_calls(needed: int = 1) -> bool:
    """Return True if we have budget for N more calls."""
    return _calls_in_window() + needed <= _MAX_CALLS_PER_WINDOW


def _record_calls(count: int = 1) -> None:
    """Record that we made API call(s)."""
    now = time.time()
    for _ in range(count):
        _api_call_log.append(now)


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# -- Per-chain scan cycle --

async def _run_chain_cycle(chain_id: str) -> None:
    """Single polling cycle scoped to one chain."""
    priority = _CHAIN_PRIORITY.get(chain_id, 99)

    # Rate limit check -- lower-priority chains yield when throttled
    if not _can_make_calls(5):
        logger.warning("[%s] Rate limit approaching (%d/%d calls in window) -- skipping cycle (priority=%d)",
                       chain_id, _calls_in_window(), _MAX_CALLS_PER_WINDOW, priority)
        return

    # Discover tokens for this chain only
    tokens = dex.discover_tokens([chain_id])
    _record_calls(2)  # boosts + profiles endpoints
    logger.info("[%s] Cycle start -- %d tokens discovered", chain_id, len(tokens))

    all_pairs: list[dict] = []
    for tok in tokens:
        if not _can_make_calls(1):
            logger.warning("[%s] Rate limit reached mid-cycle (%d calls), stopping fetch",
                           chain_id, _calls_in_window())
            break
        pairs = dex.fetch_pair_details(tok["chainId"], tok["tokenAddress"])
        _record_calls(1)
        all_pairs.extend(pairs)

    logger.info("[%s] Fetched %d pairs", chain_id, len(all_pairs))

    scored = filters.filter_and_score(all_pairs)
    logger.info("[%s] %d pairs passed scoring", chain_id, len(scored))

    # Store metrics for velocity tracking (all pairs that passed hard filters)
    for result in scored:
        pair = result["pair"]
        base = pair.get("baseToken") or {}
        addr = base.get("address") or ""
        if addr:
            vol = (pair.get("volume") or {}).get("h24", 0) or 0
            liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
            vlr = vol / liq if liq > 0 else 0
            txns = (pair.get("txns") or {}).get("h1") or {}
            buys = txns.get("buys", 0) or 0
            sells = txns.get("sells", 0) or 0
            bsr = buys / sells if sells > 0 else buys
            storage.upsert_metrics(addr, chain_id, pair.get("pairAddress", ""), vlr, bsr, result["score"])

    sent = 0
    cfg = config.get_chain_profile(chain_id)
    momentum_threshold = cfg.get("momentum_realert_threshold", 15)

    for result in scored:
        pair = result["pair"]
        chain = (pair.get("chainId") or "").lower()
        base = pair.get("baseToken") or {}
        addr = base.get("address") or pair.get("tokenAddress", "")

        # Momentum-only mode: first sighting records silently, alert only on acceleration
        prev_score = storage.get_previous_alert_score(chain, addr)

        if prev_score is None:
            # First time seeing this token -- record score silently, no alert
            storage.record_alert(chain, addr, result["score"])
            logger.debug("[%s] %s first seen (score=%.1f) -- tracking, no alert yet",
                         chain_id, base.get("symbol", "?"), result["score"])
            continue

        # Already tracked -- check if momentum confirmed
        score_delta = result["score"] - prev_score
        if score_delta < momentum_threshold:
            # Update stored score if it's higher (track the peak)
            if result["score"] > prev_score:
                storage.record_alert(chain, addr, result["score"])
            continue

        # Check dedup cooldown (don't re-alert same token within cooldown)
        if storage.was_recently_alerted(chain, addr):
            # Only re-alert if the jump is from the LAST alerted score
            continue

        # Momentum confirmed -- proceed with safety checks and alert
        logger.info("[%s] MOMENTUM for %s: %.1f -> %.1f (+%.1f)",
                    chain_id, base.get("symbol", "?"), prev_score, result["score"], score_delta)
        result["momentum_realert"] = True
        result["prev_score"] = prev_score

        should_alert, safety_data = safety_check.evaluate_safety(chain, addr)
        if not should_alert:
            logger.info("[%s] %s failed GoPlus safety check", chain_id, addr)
            continue

        # RugCheck (Solana-specific, second layer)
        rc_pass, rc_data = rugcheck.evaluate_rugcheck(addr, chain)
        if not rc_pass:
            logger.info("[%s] %s failed RugCheck", chain_id, addr)
            continue

        # Holder analysis (unique buyers, whale concentration)
        holder_pass, holder_data = holder_analysis.passes_holder_checks(addr, cfg)
        if not holder_pass:
            logger.info("[%s] %s failed holder analysis", chain_id, addr)
            continue

        # Merge all safety data for the Telegram message
        if rc_data and safety_data:
            safety_data.update(rc_data)
        elif rc_data:
            safety_data = rc_data
        if holder_data and safety_data:
            safety_data.update(holder_data)
        elif holder_data:
            safety_data = holder_data

        ok = await tg.send_alert(result, safety=safety_data)
        if ok:
            storage.record_alert(chain, addr, result["score"])
            storage.record_outcome(
                token_address=addr,
                chain_id=chain,
                pair_address=pair.get("pairAddress", ""),
                token_symbol=base.get("symbol", "?"),
                score=result["score"],
                price=_safe_float(pair.get("priceUsd")),
                liquidity=(pair.get("liquidity") or {}).get("usd"),
                market_cap=pair.get("marketCap") or pair.get("fdv"),
            )
            sent += 1

    _chain_last_run[chain_id] = time.time()
    logger.info("[%s] Cycle done -- %d alerts sent", chain_id, sent)


# -- Real-time pool listener (QuickNode websocket) --

async def _handle_new_pool(token_info: dict) -> None:
    """Handle a newly detected pool from the websocket listener.
    
    Fast-path: minimal checks, alert immediately for brand-new pools.
    """
    token_address = token_info["token_address"]
    chain_id = token_info["chain_id"]
    sol_deposited = token_info.get("sol_deposited", 0)

    # Dedup: don't alert if we've seen this token before
    if storage.was_recently_alerted(chain_id, token_address):
        return

    # Also skip if already tracked in metrics (DexScreener scanner saw it)
    prev = storage.get_previous_metrics(token_address, chain_id)
    if prev is not None:
        return

    # RugCheck (quick summary call)
    rc_pass, rc_data = rugcheck.evaluate_rugcheck(token_address, chain_id)
    if not rc_pass:
        logger.info("[WS] %s failed RugCheck -- skipping", token_address[:16])
        return

    # Build a minimal alert result
    latency = time.time() - token_info.get("detected_at", time.time())
    result = {
        "score": 0,  # No score yet -- this is a speed play
        "momentum_realert": False,
        "prev_score": 0,
        "pair": {
            "chainId": "solana",
            "baseToken": {"address": token_address, "name": "New Pool", "symbol": "???"},
            "pairAddress": "",
            "liquidity": {"usd": sol_deposited * 170},  # rough SOL->USD estimate
            "marketCap": 0,
            "volume": {"h24": 0},
            "priceChange": {},
            "txns": {"h1": {"buys": 0, "sells": 0}},
            "priceUsd": "0",
        },
        "is_new_pool": True,
        "sol_deposited": sol_deposited,
        "detection_latency_s": round(latency, 1),
    }

    # Send alert
    ok = await tg.send_new_pool_alert(token_info, rc_data)
    if ok:
        storage.record_alert(chain_id, token_address, 0)
        logger.info("[WS] Alert sent for new pool: %s (%.1f SOL, latency=%.1fs)",
                    token_address[:16], sol_deposited, latency)


# -- Background tasks (independent of per-chain scheduling) --

async def _snapshot_loop() -> None:
    """Performance snapshot tracker -- every 5 minutes, shared across all chains."""
    while True:
        await asyncio.sleep(300)
        try:
            stats = performance_tracker.run_snapshot_check()
            if stats.get("updated") or stats.get("rugged"):
                logger.info("[SNAPSHOT] %s", stats)
        except Exception:
            logger.exception("[SNAPSHOT] Error in snapshot tracker")


async def _status_log_loop() -> None:
    """Periodic status log showing per-chain timing and rate limit state."""
    while True:
        await asyncio.sleep(600)  # every 10 minutes
        calls = _calls_in_window()
        for chain in config.CHAINS:
            cfg = config.get_chain_profile(chain)
            interval = cfg.get("poll_interval_seconds", 180)
            last = _chain_last_run.get(chain)
            ago = f"{time.time() - last:.0f}s ago" if last else "never"
            logger.info("[STATUS] %s: interval=%ds, last_run=%s, api_budget=%d/%d",
                        chain, interval, ago, _MAX_CALLS_PER_WINDOW - calls, _MAX_CALLS_PER_WINDOW)


async def _cleanup_loop() -> None:
    """Daily old-record cleanup."""
    while True:
        await asyncio.sleep(86400)
        try:
            storage.cleanup_old_records()
        except Exception:
            logger.exception("Cleanup error")


# -- Entry point --

async def main() -> None:
    _setup_logging()

    # Run startup self-checks (hard failures will exit the process)
    startup_check.run_startup_checks()

    logger.info("=" * 60)
    logger.info("MemeScout Bot starting schedulers")
    logger.info("=" * 60)

    # Log per-chain config at startup
    for chain in config.CHAINS:
        cfg = config.get_chain_profile(chain)
        interval = cfg.get("poll_interval_seconds", 180)
        priority = _CHAIN_PRIORITY.get(chain, 99)
        logger.info("  [%s] interval=%ds, priority=%d, min_score=%s",
                    chain, interval, priority, cfg.get("min_alert_score", 50))

    logger.info("Rate limit: max %d calls/%ds across all chains", _MAX_CALLS_PER_WINDOW, _RATE_LIMIT_WINDOW)
    logger.info("=" * 60)

    # Start APScheduler with per-chain jobs
    scheduler = AsyncIOScheduler()
    for chain in config.CHAINS:
        cfg = config.get_chain_profile(chain)
        interval = cfg.get("poll_interval_seconds", 180)
        scheduler.add_job(
            _run_chain_cycle,
            IntervalTrigger(seconds=interval),
            args=[chain],
            id=f"scan_{chain}",
            name=f"Scan {chain}",
            max_instances=1,
        )

    scheduler.start()

    # Start independent background tasks
    asyncio.create_task(_snapshot_loop())
    asyncio.create_task(_status_log_loop())
    asyncio.create_task(_cleanup_loop())

    # Start real-time pool listener (QuickNode HTTP polling) if configured
    if config.QUICKNODE_HTTP_URL:
        listener = pool_listener.PoolListener(on_new_pool=_handle_new_pool)
        asyncio.create_task(listener.start())
        logger.info("[POOL] Pump.fun pool poller started")
    else:
        logger.warning("[POOL] QUICKNODE_HTTP_URL not set -- pool detection disabled")

    # Keep the event loop alive
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
