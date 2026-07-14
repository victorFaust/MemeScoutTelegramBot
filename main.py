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
import bot_handler
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

    # Score ALL pairs (including below threshold) for momentum tracking
    scored = filters.filter_and_score(all_pairs, min_score=0)
    above_threshold = [r for r in scored if r["score"] >= (config.get_chain_profile(chain_id).get("min_alert_score", 40))]
    logger.info("[%s] %d scored, %d above threshold", chain_id, len(scored), len(above_threshold))

    # Store metrics for ALL scored pairs (enables momentum detection)
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

    # Only process tokens above the alert threshold for actual alerts
    for result in above_threshold:
        pair = result["pair"]
        chain = (pair.get("chainId") or "").lower()
        base = pair.get("baseToken") or {}
        addr = base.get("address") or pair.get("tokenAddress", "")

        # Dedup: skip if alerted recently
        if storage.was_recently_alerted(chain, addr):
            continue

        result["momentum_realert"] = False
        result["prev_score"] = 0

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

            # Auto-buy if enabled (DexScreener momentum alerts)
            if config.AUTO_BUY_ENABLED and config.TRADING_ENABLED:
                import executor
                amount_sol = executor.usd_to_sol(config.AUTO_BUY_AMOUNT_USD)
                allowed, reason = executor.can_trade()
                if allowed:
                    buy_result = await asyncio.to_thread(executor.buy_token, addr, amount_sol)
                    if buy_result:
                        sol_price = executor.get_sol_price()
                        await tg.send_trade_notification(
                            f"AUTO-BUY: ${config.AUTO_BUY_AMOUNT_USD:.0f} ({amount_sol:.3f} SOL) | "
                            f"impact: {buy_result.get('price_impact_pct', 0):.1f}%",
                            addr
                        )
                        logger.info("[AUTO] Bought %s for $%.0f (%.3f SOL)",
                                    base.get("symbol", "?"), config.AUTO_BUY_AMOUNT_USD, amount_sol)
                    else:
                        logger.warning("[AUTO] Buy failed for %s", addr[:16])
                else:
                    logger.info("[AUTO] Buy skipped for %s: %s", addr[:16], reason)

    _chain_last_run[chain_id] = time.time()
    logger.info("[%s] Cycle done -- %d alerts sent", chain_id, sent)


# -- Real-time pool listener --

async def _handle_new_pool(token_info: dict) -> None:
    """Handle a newly discovered token from RugCheck new_tokens feed.
    
    RugCheck data (score, LP lock, risks) is already included in token_info.
    Waits 60 seconds, then checks DexScreener for traction before alerting.
    """
    token_address = token_info["token_address"]
    chain_id = token_info["chain_id"]
    symbol = token_info.get("symbol", "???")

    # Dedup
    if storage.was_recently_alerted(chain_id, token_address):
        return

    # RugCheck data already included from pool_listener
    rc_data = {
        "rugcheck_score": token_info.get("rugcheck_score"),
        "lp_locked_pct": token_info.get("lp_locked_pct", 0),
        "risks": token_info.get("risks", []),
        "risk_count": token_info.get("risk_count", 0),
    }

    # Wait 60 seconds for initial activity
    logger.info("[POOL] $%s (%s) -- waiting 60s for traction...", symbol, token_address[:16])
    await asyncio.sleep(60)

    # Re-check dedup
    if storage.was_recently_alerted(chain_id, token_address):
        return

    # Check DexScreener for activity
    pairs = await asyncio.to_thread(dex.fetch_pair_details, chain_id, token_address)
    if not pairs:
        logger.debug("[POOL] $%s -- not on DexScreener yet, skipping", symbol)
        return

    pair = pairs[0]
    txns = (pair.get("txns") or {}).get("h1") or {}
    buys = txns.get("buys", 0) or 0
    total_txns = buys + (txns.get("sells", 0) or 0)

    if total_txns < 3:
        logger.debug("[POOL] $%s -- only %d txns after 60s, skipping", symbol, total_txns)
        return

    # Build alert data
    token_info["pair_data"] = pair
    token_info["total_txns_90s"] = total_txns
    token_info["buys_90s"] = buys

    # Update symbol from DexScreener if available
    base_token = pair.get("baseToken", {})
    if base_token.get("symbol"):
        token_info["symbol"] = base_token["symbol"]

    # Holder analysis + serial deployer check
    cfg = config.get_chain_profile(chain_id)
    holder_pass, holder_data = await asyncio.to_thread(
        holder_analysis.passes_holder_checks, token_address, cfg
    )
    if not holder_pass:
        logger.info("[POOL] $%s failed holder analysis -- skipping", token_info.get("symbol", "?"))
        return

    # Merge holder data into rc_data for the message
    if holder_data and rc_data:
        rc_data.update(holder_data)
    elif holder_data:
        rc_data = holder_data

    # Send alert
    ok = await tg.send_new_pool_alert(token_info, rc_data)
    if ok:
        storage.record_alert(chain_id, token_address, 0)
        logger.info("[POOL] Alert sent: $%s (%s) | %d txns",
                    token_info["symbol"], token_address[:16], total_txns)

        # Auto-buy if enabled
        if config.AUTO_BUY_NEW_POOLS and config.AUTO_BUY_ENABLED and config.TRADING_ENABLED:
            import executor
            amount_sol = executor.usd_to_sol(config.AUTO_BUY_AMOUNT_USD)
            allowed, reason = executor.can_trade()
            if allowed:
                buy_result = await asyncio.to_thread(executor.buy_token, token_address, amount_sol)
                if buy_result:
                    await tg.send_trade_notification(
                        f"AUTO-BUY: ${config.AUTO_BUY_AMOUNT_USD:.0f} ({amount_sol:.3f} SOL) | "
                        f"${token_info['symbol']}",
                        token_address
                    )


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


async def _exit_monitor_loop() -> None:
    """Auto-exit monitor with trailing stop-loss.
    
    Trailing SL: tracks the peak PnL for each position.
    If price drops TRAIL_STOP_PCT from the peak, sells.
    E.g., peak was +80%, trail is 15% → sells if drops to +65%.
    """
    import executor

    trail_pct = abs(config.STOP_LOSS_PCT)  # reuse SL value as trail distance
    logger.info("[EXIT] Auto-exit started (TP=+%.0f%%, Trail=%.0f%% from peak, interval=%ds)",
                config.TAKE_PROFIT_PCT, trail_pct, config.EXIT_CHECK_INTERVAL)

    # Track peak PnL per position ID
    peak_pnl: dict[int, float] = {}

    while True:
        await asyncio.sleep(config.EXIT_CHECK_INTERVAL)

        if not config.TRADING_ENABLED:
            continue

        positions = storage.get_open_positions()
        if not positions:
            peak_pnl.clear()
            continue

        # Clean up peaks for closed positions
        open_ids = {p["id"] for p in positions}
        peak_pnl = {k: v for k, v in peak_pnl.items() if k in open_ids}

        for pos in positions:
            try:
                pnl = await asyncio.to_thread(executor.check_position_pnl, pos)
                if pnl is None:
                    continue

                pnl_pct = pnl["pnl_pct"]
                pos_id = pos["id"]
                token = pos.get("token_symbol") or pos.get("token_address", "?")[:12]

                # Update peak
                prev_peak = peak_pnl.get(pos_id, pnl_pct)
                if pnl_pct > prev_peak:
                    peak_pnl[pos_id] = pnl_pct
                    prev_peak = pnl_pct

                # Take profit — partial sell (sell PARTIAL_SELL_PCT, let rest ride with trail)
                if pnl_pct >= config.TAKE_PROFIT_PCT:
                    sell_pct = config.PARTIAL_SELL_PCT
                    logger.info("[EXIT] TAKE PROFIT: $%s at +%.0f%% (selling %.0f%%)", token, pnl_pct, sell_pct)
                    result = await asyncio.to_thread(
                        executor.sell_partial, pos_id, pos["token_address"], pos["token_amount"], sell_pct
                    )
                    if result:
                        await tg.send_trade_notification(
                            f"PARTIAL SELL (TP +{pnl_pct:.0f}%) ${token} | Sold {sell_pct:.0f}% | {result['sol_received']:.4f} SOL | {result['remaining']} tokens left",
                            pos["token_address"]
                        )
                        # Set peak for trailing the remaining position
                        peak_pnl[pos_id] = pnl_pct
                    continue

                # Trailing stop-loss: sell if dropped trail_pct from peak
                # Only activate trailing after position is in profit (peak > 0)
                if prev_peak > 10 and (prev_peak - pnl_pct) >= trail_pct:
                    logger.info("[EXIT] TRAIL STOP: $%s peak=+%.0f%% now=+%.0f%% (dropped %.0f%%)",
                                token, prev_peak, pnl_pct, prev_peak - pnl_pct)
                    result = await asyncio.to_thread(
                        executor.sell_token, pos_id, pos["token_address"], pos["token_amount"]
                    )
                    if result:
                        await tg.send_trade_notification(
                            f"SOLD (Trail) ${token} | Peak +{prev_peak:.0f}% -> +{pnl_pct:.0f}% | {result['sol_received']:.4f} SOL",
                            pos["token_address"]
                        )
                    continue

                # Hard stop-loss (below entry, no trailing)
                if pnl_pct <= config.STOP_LOSS_PCT:
                    logger.info("[EXIT] STOP LOSS: $%s at %.0f%%", token, pnl_pct)
                    result = await asyncio.to_thread(
                        executor.sell_token, pos_id, pos["token_address"], pos["token_amount"]
                    )
                    if result:
                        await tg.send_trade_notification(
                            f"SOLD (SL {pnl_pct:.0f}%) ${token} | {result['sol_received']:.4f} SOL",
                            pos["token_address"]
                        )

                await asyncio.sleep(2)

            except Exception as e:
                logger.error("[EXIT] Error checking position #%d: %s", pos.get("id", 0), e)


async def _pnl_notification_loop() -> None:
    """Send periodic PnL updates for open positions (every 15 min)."""
    import executor

    while True:
        await asyncio.sleep(900)  # 15 minutes

        if not config.TRADING_ENABLED:
            continue

        positions = storage.get_open_positions()
        if not positions:
            continue

        lines = ["PORTFOLIO UPDATE\n"]
        total_invested = 0.0
        total_current = 0.0

        for pos in positions:
            try:
                pnl = await asyncio.to_thread(executor.check_position_pnl, pos)
                symbol = pos.get("token_symbol") or pos.get("token_address", "?")[:8]
                amount_sol = pos.get("buy_amount_sol", 0)
                total_invested += amount_sol

                if pnl:
                    current_val = pnl["current_value_sol"]
                    pnl_pct = pnl["pnl_pct"]
                    total_current += current_val
                    emoji = "🟢" if pnl_pct >= 0 else "🔴"
                    sign = "+" if pnl_pct >= 0 else ""
                    lines.append(f"{emoji} ${symbol}: {sign}{pnl_pct:.0f}% ({current_val:.4f} SOL)")
                else:
                    lines.append(f"⚪ ${symbol}: N/A")
                    total_current += amount_sol
            except Exception:
                continue

        if total_invested > 0:
            total_pnl = (total_current - total_invested) / total_invested * 100
            sign = "+" if total_pnl >= 0 else ""
            lines.append(f"\nTotal: {sign}{total_pnl:.0f}% | {total_current:.4f} SOL")
            await tg.send_trade_notification("\n".join(lines))


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
    asyncio.create_task(_exit_monitor_loop())
    asyncio.create_task(_pnl_notification_loop())

    # Start new token discovery (RugCheck feed)
    listener = pool_listener.PoolListener(on_new_pool=_handle_new_pool)
    asyncio.create_task(listener.start())

    # Start Telegram bot handler (for buy buttons + commands)
    asyncio.create_task(bot_handler.start_bot_handler())

    # Keep the event loop alive
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
