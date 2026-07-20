"""Entry point -- per-chain scheduled polling with independent intervals."""

import asyncio
import logging
import sys
import time
from collections import deque
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import bot_handler
import config
import dexscreener_client as dex
import feature_logger
import holder_analysis
import performance_tracker
import pool_listener
import rugcheck
import safety_check
import startup_check
import storage
import telegram_notifier as tg
import wallet_tracker

logger = logging.getLogger(__name__)


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


# -- Rate limit guard --
_api_call_log: deque = deque()
_RATE_LIMIT_WINDOW = 60  # seconds
_MAX_CALLS_PER_WINDOW = 250  # headroom under DexScreener 300/min limit

_CHAIN_PRIORITY: dict[str, int] = {
    "robinhood": 1,
    "solana": 2,
}
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

    if not _can_make_calls(5):
        logger.warning(
            "[%s] Rate limit approaching (%d/%d calls in window) -- skipping cycle (priority=%d)",
            chain_id,
            _calls_in_window(),
            _MAX_CALLS_PER_WINDOW,
            priority,
        )
        return

    tokens = dex.discover_tokens([chain_id])
    _record_calls(2)  # boosts + profiles endpoints
    logger.info("[%s] Cycle start -- %d tokens discovered", chain_id, len(tokens))

    all_pairs: list[dict] = []
    for tok in tokens:
        if not _can_make_calls(1):
            logger.warning(
                "[%s] Rate limit reached mid-cycle (%d calls), stopping fetch",
                chain_id,
                _calls_in_window(),
            )
            break
        pairs = dex.fetch_pair_details(tok["chainId"], tok["tokenAddress"])
        _record_calls(1)
        all_pairs.extend(pairs)

    logger.info("[%s] Fetched %d pairs", chain_id, len(all_pairs))

    scored = filters.filter_and_score(all_pairs, min_score=0)
    above_threshold = [
        r
        for r in scored
        if r["score"] >= config.get_chain_profile(chain_id).get("min_alert_score", 40)
    ]
    logger.info("[%s] %d scored, %d above threshold", chain_id, len(scored), len(above_threshold))

    for result in scored:
        pair = result["pair"]
        base = pair.get("baseToken") or {}
        addr = base.get("address") or ""
        if not addr:
            continue

        vol = (pair.get("volume") or {}).get("h24", 0) or 0
        liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
        vlr = vol / liq if liq > 0 else 0
        txns = (pair.get("txns") or {}).get("h1") or {}
        buys = txns.get("buys", 0) or 0
        sells = txns.get("sells", 0) or 0
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
            # Log ML features for learning system
            feature_logger.log_features(
                token_address=addr,
                chain_id=chain,
                token_symbol=base.get("symbol", "?"),
                score_result=result,
                pair=pair,
                safety_data=safety_data,
            )
            sent += 1

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

        # Log ML features for new pool alerts
        pool_score_result = {"score": 0, "breakdown": {}}
        feature_logger.log_features(
            token_address=token_address,
            chain_id=chain_id,
            token_symbol=token_info.get("symbol", "?"),
            score_result=pool_score_result,
            pair=pair,
            safety_data=rc_data,
        )


# -- Smart Wallet Copy-Trade Handler --

async def _handle_wallet_buy(wallet_address: str, token_address: str, confidence: int, signature: str) -> None:
    """Handle a new buy detected from a tracked smart wallet.

    Flow:
    1. Check confidence (how many tracked wallets bought this)
    2. Fetch token data from DexScreener
    3. Run safety checks (RugCheck + GoPlus + holder analysis)
    4. Send alert with wallet info
    5. Auto-buy if enabled and confidence >= 2 (or if single high-WR wallet)
    """
    chain_id = "solana"


async def _handle_wallet_sell(wallet_address: str, token_address: str, signature: str) -> None:
    """Alpha-exit mirroring: if a tracked wallet sells, exit our position too.

    For Phase A we trigger a safety “bag prevention” exit by marking staged exits
    as fully consumed and forcing the exit monitor to sell remaining with the
    trailing stage immediately on next tick.
    """
    try:
        chain_id = "solana"
        open_positions = storage.get_open_positions_by_token(token_address, chain_id=chain_id)
        if not open_positions:
            return

        for pos in open_positions:
            pos_id = pos["id"]
            state = storage.get_exit_state(pos_id) or {}
            # Force: stage1 & stage2 consumed, enable final trailing.
            # The exit loop will then apply trailing/stop-loss for the remaining.
            storage.update_exit_state(
                pos_id,
                stage1_done=1,
                stage2_done=1,
                final_trailing_active=1,
            )
            logger.info("[ALPHA-EXIT] Marked exit stages for position #%d (%s) due to wallet sell by %s",
                        pos_id, token_address[:16], wallet_address[:12])

    except Exception as e:
        logger.error("[ALPHA-EXIT] Failed to handle wallet sell: %s", e)

    logger.info("[WALLET] Processing buy: wallet=%s token=%s confidence=%d", wallet_address[:12], token_address[:16], confidence)

    # Skip if already alerted by normal flow
    if storage.was_recently_alerted(chain_id, token_address):
        logger.info("[WALLET] %s already alerted recently, skipping", token_address[:16])
        return

    # Fetch pair data from DexScreener
    pairs = await asyncio.to_thread(dex.fetch_pair_details, chain_id, token_address)
    if not pairs:
        logger.info("[WALLET] %s not on DexScreener yet, skipping", token_address[:16])
        return

    pair = pairs[0]
    base = pair.get("baseToken", {})
    symbol = base.get("symbol", "???")
    mc = pair.get("marketCap") or pair.get("fdv") or 0
    liq = (pair.get("liquidity") or {}).get("usd", 0) or 0
    price = float(pair.get("priceUsd", 0) or 0)

    logger.info("[WALLET] $%s: MC=$%.0fK, Liq=$%.0fK, Price=$%.8f", symbol, mc/1000, liq/1000, price)

    # Momentum check: only proceed if token is showing positive price action
    pc = pair.get("priceChange") or {}
    change_5m = pc.get("m5", 0) or 0
    change_1h = pc.get("h1", 0) or 0
    txns = (pair.get("txns") or {}).get("h1") or {}
    buys_1h = txns.get("buys", 0) or 0
    sells_1h = txns.get("sells", 0) or 0

    has_momentum = (
        change_5m > 0           # price going up in last 5 min
        or change_1h > 5        # or up 5%+ in last hour
        or buys_1h > sells_1h   # or more buys than sells
    )

    if not has_momentum:
        logger.info("[WALLET] $%s no momentum (5m=%.1f%%, 1h=%.1f%%, buys=%d sells=%d), skipping alert",
                    symbol, change_5m, change_1h, buys_1h, sells_1h)
        return

    logger.info("[WALLET] $%s has momentum: 5m=%+.1f%%, 1h=%+.1f%%, buys=%d sells=%d",
                symbol, change_5m, change_1h, buys_1h, sells_1h)

    # Basic sanity: skip if MC > $5M or liq < $1K (too big or too illiquid)
    if mc > 5_000_000:
        logger.info("[WALLET] $%s MC too high ($%.0fK), skipping", symbol, mc / 1000)
        return
    if liq < 1000:
        logger.info("[WALLET] $%s liq too low ($%.0f), skipping", symbol, liq)
        return

    # Safety checks
    cfg = config.get_chain_profile(chain_id)
    rc_pass, rc_data = await asyncio.to_thread(rugcheck.evaluate_rugcheck, token_address, chain_id)
    if not rc_pass:
        logger.info("[WALLET] $%s failed RugCheck, skipping", symbol)
        return

    # Holder analysis
    holder_pass, holder_data = await asyncio.to_thread(
        holder_analysis.passes_holder_checks, token_address, cfg
    )

    # Merge safety data
    safety_data = rc_data or {}
    if holder_data:
        safety_data.update(holder_data)

    # Get wallet info for the alert
    wallets = wallet_tracker.get_tracked_wallets()
    wallet_info = next((w for w in wallets if w["address"] == wallet_address), {})
    wallet_label = wallet_info.get("label") or wallet_address[:12]
    wallet_wr = wallet_info.get("win_rate", 0)

    def _mc(v):
        return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"

    # Build alert message
    conf_emoji = "🔥" * min(confidence, 5)
    alert_text = (
        f"🐋 SMART MONEY BUY\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Token: ${symbol}\n"
        f"MC: {_mc(mc)} | Liq: {_mc(liq)}\n"
        f"Price: ${price:.8f}\n"
        f"\n"
        f"👛 Wallet: {wallet_label}\n"
        f"Win Rate: {wallet_wr:.0f}%\n"
        f"Confidence: {confidence} wallet(s) {conf_emoji}\n"
    )

    if safety_data:
        rc_score = safety_data.get("rugcheck_score")
        lp_lock = safety_data.get("lp_locked_pct", 0)
        if rc_score is not None:
            alert_text += f"\nRugCheck: {rc_score:.0f}/100 | LP Lock: {lp_lock:.0f}%"

    alert_text += f"\n━━━━━━━━━━━━━━━━━━\nTx: solscan.io/tx/{signature[:32]}..."

    # Send alert via Telegram
    ok = await tg.send_trade_notification(alert_text, token_address)
    if ok:
        storage.record_alert(chain_id, token_address, 0)
        storage.record_outcome(
            token_address=token_address,
            chain_id=chain_id,
            pair_address=pair.get("pairAddress", ""),
            token_symbol=symbol,
            score=0,
            price=price,
            liquidity=liq,
            market_cap=mc,
        )

    # Rate limits: prevent spam buys
    MAX_BUYS_PER_WALLET_PER_HOUR = 3
    MAX_TOTAL_COPY_BUYS_PER_HOUR = 5

    wallet_recent = wallet_tracker.get_wallet_buy_count_recent(wallet_address, minutes=60)
    total_recent = wallet_tracker.get_total_copy_buys_recent(minutes=60)

    if wallet_recent >= MAX_BUYS_PER_WALLET_PER_HOUR:
        logger.info("[WALLET] $%s rate-limited: wallet %s has %d buys in last hour (max %d)",
                    symbol, wallet_address[:12], wallet_recent, MAX_BUYS_PER_WALLET_PER_HOUR)
        return

    if total_recent >= MAX_TOTAL_COPY_BUYS_PER_HOUR:
        logger.info("[WALLET] $%s rate-limited: %d total copy-buys in last hour (max %d)",
                    symbol, total_recent, MAX_TOTAL_COPY_BUYS_PER_HOUR)
        return

    # Auto-buy decision
    should_buy = (
        config.AUTO_BUY_ENABLED
        and config.TRADING_ENABLED
        and (confidence >= 2 or wallet_wr >= 60)  # 2+ wallets OR single wallet with 60%+ WR
    )
    # Holder check is advisory for copy-trades (log but don't block)
    if not holder_pass:
        logger.info("[WALLET] $%s holder analysis failed -- proceeding anyway (copy-trade)", symbol)

    if not should_buy:
        reasons = []
        if not config.AUTO_BUY_ENABLED:
            reasons.append("AUTO_BUY_ENABLED=false")
        if not config.TRADING_ENABLED:
            reasons.append("TRADING_ENABLED=false")
        if not (confidence >= 2 or wallet_wr >= 50):
            reasons.append(f"low confidence ({confidence}) and WR ({wallet_wr:.0f}%)")
        logger.info("[WALLET] $%s auto-buy skipped: %s", symbol, ", ".join(reasons))
        return

    if should_buy:
        import executor
        # Scale buy amount by confidence
        base_amount = config.AUTO_BUY_AMOUNT_USD
        if confidence >= 3:
            buy_usd = base_amount * 2  # Double size for 3+ wallets
        elif confidence >= 2:
            buy_usd = base_amount * 1.5
        else:
            buy_usd = base_amount

        amount_sol = executor.usd_to_sol(buy_usd)
        logger.info("[WALLET] Attempting copy-buy: $%s for $%.0f (%.4f SOL), confidence=%d", symbol, buy_usd, amount_sol, confidence)
        allowed, reason = executor.can_trade()
        if allowed:
            buy_result = await asyncio.to_thread(executor.buy_token, token_address, amount_sol)
            if buy_result:
                await tg.send_trade_notification(
                    f"🐋 COPY-BUY: ${buy_usd:.0f} ({amount_sol:.3f} SOL) | ${symbol}\n"
                    f"Following: {wallet_label} (WR {wallet_wr:.0f}%)\n"
                    f"Confidence: {confidence} wallet(s)",
                    token_address
                )
                logger.info("[WALLET] Copy-bought $%s for $%.0f (confidence=%d)", symbol, buy_usd, confidence)
                wallet_tracker.mark_buy_acted(wallet_address, token_address)
            else:
                logger.warning("[WALLET] Copy-buy failed for $%s", symbol)
        else:
            logger.info("[WALLET] Copy-buy skipped for $%s: %s", symbol, reason)


# -- Background tasks (independent of per-chain scheduling) --

async def _snapshot_loop() -> None:
    """Performance snapshot tracker -- every 5 minutes, shared across all chains."""
    while True:
        await asyncio.sleep(300)
        try:
            stats = performance_tracker.run_snapshot_check()
            if stats.get("updated") or stats.get("rugged"):
                logger.info("[SNAPSHOT] %s", stats)
            # Label ML features periodically
            labeled = feature_logger.label_outcomes()
            if labeled:
                logger.info("[ML] Labeled %d new outcomes", labeled)
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
                # Skip positions bought less than 60s ago (let price stabilize after entry)
                pos_age = time.time() - pos.get("bought_at", 0)
                if pos_age < 60:
                    continue

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


async def _notify_new_wallets(newly_added: list[dict]) -> None:
    """Send Telegram alerts for newly discovered wallets."""
    total_tracked = wallet_tracker.get_wallet_count()
    for w in newly_added:
        addr = w["address"]
        short_addr = f"{addr[:8]}...{addr[-6:]}"
        tokens_str = ", ".join(f"${t}" for t in w["appeared_in"][:5])

        alert_text = (
            f"🔍 NEW ALPHA WALLET FOUND\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👛 {short_addr}\n"
            f"📊 Win Rate: {w['win_rate']:.0f}%\n"
            f"📈 Trades: {w['winning_trades']}/{w['total_trades']} winning\n"
            f"💰 Avg Return: {w['avg_return']:+.1f}%\n"
            f"🏆 Early in: {tokens_str}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Auto-added to tracker ({total_tracked} total)\n"
            f"Solscan: solscan.io/account/{addr}"
        )
        await tg.send_trade_notification(alert_text)


async def _wallet_discovery_loop() -> None:
    """Periodically discover alpha wallets from trending tokens + alert history.

    Runs every 6 hours. On first run, bootstraps from DexScreener top gainers.
    """
    # Run trending discovery immediately on startup (no wait needed)
    await asyncio.sleep(60)  # Just 1 min to let bot stabilize

    while True:
        try:
            # Phase 1: Bootstrap from trending tokens (always works, no history needed)
            logger.info("[DISCOVERY] Starting trending token discovery...")
            trending_added = await asyncio.to_thread(wallet_tracker.discover_from_trending)
            if trending_added:
                await _notify_new_wallets(trending_added)
                logger.info("[DISCOVERY] Trending: %d new wallets", len(trending_added))

            # Phase 2: Discover from our own alert winners (needs outcome data)
            logger.info("[DISCOVERY] Starting alert history discovery...")
            history_added = await asyncio.to_thread(wallet_tracker.discover_alpha_wallets)
            if history_added:
                await _notify_new_wallets(history_added)
                logger.info("[DISCOVERY] History: %d new wallets", len(history_added))

            if not trending_added and not history_added:
                logger.info("[DISCOVERY] No new wallets found this cycle")

            # Phase 3: Prune underperforming wallets
            pruned = await asyncio.to_thread(wallet_tracker.prune_underperforming_wallets)
            if pruned:
                total_tracked = wallet_tracker.get_wallet_count()
                for w in pruned:
                    addr = w["address"]
                    short_addr = f"{addr[:8]}...{addr[-6:]}"
                    old_wr = w.get("old_win_rate", 0)
                    new_wr = w.get("new_win_rate")
                    wr_str = f"{new_wr:.0f}%" if new_wr is not None else "N/A"

                    alert_text = (
                        f"🚫 WALLET DROPPED\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"👛 {w.get('label', short_addr)}\n"
                        f"📉 Win Rate: {old_wr:.0f}% → {wr_str}\n"
                        f"❌ Reason: {w['reason']}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Remaining tracked: {total_tracked}"
                    )
                    await tg.send_trade_notification(alert_text)

        except Exception:
            logger.exception("[DISCOVERY] Error in discovery loop")

        # Run every 6 hours
        await asyncio.sleep(21600)


async def _tx_confirmation_loop() -> None:
    """Poll RPC for pending buy transaction confirmations.

    Checks every 10 seconds. If confirmed/finalized -> update status.
    If failed -> mark position as 'failed'. If not found after 90s -> mark expired.
    """
    import executor

    while True:
        await asyncio.sleep(10)
        try:
            pending = storage.get_pending_positions()
            if not pending:
                continue

            for pos in pending:
                sig = pos.get("buy_signature", "")
                pos_id = pos.get("id", 0)
                bought_at = pos.get("bought_at", 0)
                age_seconds = time.time() - bought_at

                if not sig:
                    storage.update_tx_status(pos_id, "no_sig")
                    continue

                status = await asyncio.to_thread(executor.confirm_transaction, sig)

                if status in ("confirmed", "finalized"):
                    storage.update_tx_status(pos_id, status)
                    symbol = pos.get("token_symbol") or pos.get("token_address", "?")[:8]
                    logger.info("[TX] Position #%d ($%s) confirmed: %s", pos_id, symbol, status)
                    await tg.send_trade_notification(
                        f"TX CONFIRMED ${symbol}\n"
                        f"Status: {status}\n"
                        f"Sig: {sig[:20]}...\n"
                        f"View: solscan.io/tx/{sig}"
                    )
                elif status == "failed":
                    storage.update_tx_status(pos_id, "failed")
                    # Close the position since tx failed on-chain
                    storage.close_position(pos_id, 0, sig)
                    symbol = pos.get("token_symbol") or pos.get("token_address", "?")[:8]
                    logger.warning("[TX] Position #%d ($%s) FAILED on-chain", pos_id, symbol)
                    await tg.send_trade_notification(
                        f"TX FAILED ${symbol}\n"
                        f"Your buy transaction failed on-chain.\n"
                        f"Sig: {sig[:20]}..."
                    )
                elif age_seconds > 90 and status == "not_found":
                    # Don't close position — tx may have succeeded but RPC can't find it
                    # The tokens are in the wallet, so keep the position open
                    storage.update_tx_status(pos_id, "unconfirmed")
                    symbol = pos.get("token_symbol") or pos.get("token_address", "?")[:8]
                    logger.warning("[TX] Position #%d ($%s) unconfirmed after %.0fs (keeping open)", pos_id, symbol, age_seconds)
                    await tg.send_trade_notification(
                        f"⚠️ TX UNCONFIRMED ${symbol}\n"
                        f"Could not confirm after 90s but position kept open.\n"
                        f"Check: solscan.io/tx/{sig}"
                    )
                # else: still pending, wait more

        except Exception:
            logger.exception("[TX] Error in confirmation loop")


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
    asyncio.create_task(_tx_confirmation_loop())
    asyncio.create_task(_wallet_discovery_loop())

    # Start new token discovery (RugCheck feed)
    listener = pool_listener.PoolListener(on_new_pool=_handle_new_pool)
    asyncio.create_task(listener.start())

    # Start smart wallet tracker (copy-trading)
    asyncio.create_task(wallet_tracker.poll_tracked_wallets(_handle_wallet_buy))

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
