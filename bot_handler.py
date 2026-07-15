"""Telegram bot UI handler -- Trojan/Maestro style.

Features:
- /start shows main menu with inline buttons
- Command suggestions via BotFather menu
- Inline keyboard navigation for all features
- Clean emoji-formatted output
"""

import asyncio
import datetime
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

import config
import executor
import storage

logger = logging.getLogger(__name__)


def _is_authorized(update: Update) -> bool:
    """Allow trading controls only from configured user/chat."""
    expected = (config.TELEGRAM_CHAT_ID or "").strip()
    if not expected:
        return True

    query = update.callback_query
    msg = update.message or (query.message if query else None)
    user_id = str(query.from_user.id) if query and query.from_user else (str(update.effective_user.id) if update.effective_user else "")
    chat_id = str(msg.chat_id) if msg else ""
    return expected in {user_id, chat_id}


async def _safe_edit_message(query, text: str, reply_markup=None) -> None:
    """Edit callback message and fall back to a reply if edit fails."""
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        logger.exception("[BOT] Failed to edit message; falling back to reply")
        if query.message:
            await query.message.reply_text(text, reply_markup=reply_markup)


# -- Main Menu --

def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Portfolio", callback_data="menu:positions"),
         InlineKeyboardButton("Trades", callback_data="menu:trades")],
        [InlineKeyboardButton("Wallet", callback_data="menu:wallet"),
         InlineKeyboardButton("Watchlist", callback_data="menu:watchlist")],
        [InlineKeyboardButton("Settings", callback_data="menu:settings"),
         InlineKeyboardButton("Stop Trading", callback_data="menu:stop")],
        [InlineKeyboardButton("Auto-Buy: " + ("ON" if config.AUTO_BUY_ENABLED else "OFF"), callback_data="menu:autobuy")],
    ])


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu."""
    wallet = executor.get_wallet_address()
    balance = executor.get_wallet_balance()
    positions = storage.get_open_positions_count()

    wallet_str = f"{wallet[:6]}...{wallet[-4:]}" if wallet else "Not set"
    bal_str = f"{balance['sol']:.4f} SOL (${balance['usd']:.2f})" if balance else "N/A"

    text = (
        "MemeScout Bot\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Wallet: {wallet_str}\n"
        f"Balance: {bal_str}\n"
        f"Open Positions: {positions}/{config.MAX_OPEN_POSITIONS}\n"
        f"Trading: {'ON' if config.TRADING_ENABLED else 'OFF'}\n"
        f"Auto-Buy: {'ON' if config.AUTO_BUY_ENABLED else 'OFF'} (${config.AUTO_BUY_AMOUNT_USD:.0f})\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=_main_menu_keyboard())


# -- Menu Callbacks --

async def _handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu button presses."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    action = query.data.split(":", 1)[1] if ":" in query.data else query.data

    if action == "positions":
        await _show_positions(query)
    elif action == "trades":
        await _show_trades(query)
    elif action.startswith("trade_"):
        pos_id = int(action.split("_")[1])
        await _show_trade_detail(query, pos_id)
    elif action == "wallet":
        await _show_wallet(query)
    elif action == "watchlist":
        await _show_watchlist(query)
    elif action == "autobuy":
        await _toggle_autobuy(query)
    elif action == "settings":
        await _show_settings(query)
    elif action == "sellall":
        await _sell_all(query)
    elif action == "stop":
        await _stop_trading(query)
    elif action == "back":
        await _handle_start(update, context)
    elif action.startswith("sell_"):
        pos_id = int(action.split("_")[1])
        await _sell_position(query, pos_id)
    elif action == "autobuy_pools":
        config.AUTO_BUY_NEW_POOLS = not config.AUTO_BUY_NEW_POOLS
        await _show_settings(query)
    elif action.startswith("set_amount_"):
        amount = float(action.split("_")[2])
        config.AUTO_BUY_AMOUNT_USD = amount
        await _show_settings(query)
    elif action.startswith("unwatch_"):
        addr = action[8:]
        storage.remove_from_watchlist(addr)
        await _show_watchlist(query)


async def _show_positions(query) -> None:
    """Show portfolio with live PnL."""
    positions = storage.get_open_positions()
    if not positions:
        recent = storage.get_recent_positions(limit=5)
        if not recent:
            await query.edit_message_text(
                "No open positions.\n\nBuy tokens from alerts or use /buy <token> $amount",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Back", callback_data="menu:back")
                ]])
            )
            return

        lines = ["No open positions.\n", "Recent trades:\n"]
        for p in recent:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            spent = p.get("buy_amount_sol", 0) or 0
            got = p.get("sell_amount_sol")
            status = p.get("status", "open")
            if got is not None:
                pnl_sol = got - spent
                pnl_pct = (pnl_sol / spent * 100) if spent > 0 else 0
                sign = "+" if pnl_pct >= 0 else ""
                lines.append(f"${sym}: {sign}{pnl_pct:.0f}% ({sign}{pnl_sol:.4f} SOL)")
            else:
                lines.append(f"${sym}: status={status}, buy={spent:.4f} SOL")

        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back", callback_data="menu:back")
            ]])
        )
        return

    lines = ["PORTFOLIO\n"]
    total_invested = 0.0
    total_current = 0.0
    buttons = []

    for i, p in enumerate(positions):
        token_addr = p.get("token_address", "?")
        amount_sol = p.get("buy_amount_sol", 0)
        pos_id = p.get("id", 0)
        token_amount = p.get("token_amount", 0)
        symbol = p.get("token_symbol") or token_addr[:8]
        entry_mc = p.get("entry_mc", 0) or 0
        entry_price = p.get("entry_price_usd", 0) or 0
        total_invested += amount_sol

        pnl = await asyncio.to_thread(executor.check_position_pnl, p)

        if pnl:
            current_val = pnl["current_value_sol"]
            pnl_pct = pnl["pnl_pct"]
            pnl_sol = pnl["pnl_sol"]
            total_current += current_val

            import dexscreener_client as dex
            pairs = await asyncio.to_thread(dex.fetch_pair_details, "solana", token_addr)
            current_mc = 0
            current_price = 0.0
            if pairs:
                current_mc = pairs[0].get("marketCap") or pairs[0].get("fdv") or 0
                current_price = float(pairs[0].get("priceUsd", 0) or 0)
                base = pairs[0].get("baseToken", {})
                symbol = base.get("symbol", symbol)

            sign = "+" if pnl_pct >= 0 else ""
            emoji = "🟢" if pnl_pct >= 0 else "🔴"

            def _mc(v):
                return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"

            def _price(v):
                if v <= 0: return "N/A"
                if v < 0.0001: return f"${v:.10f}"
                if v < 0.01: return f"${v:.6f}"
                return f"${v:.4f}"

            lines.append(
                f"{emoji} ${symbol}\n"
                f"   MC: {_mc(entry_mc)} -> {_mc(current_mc)}\n"
                f"   Price: {_price(entry_price)} -> {_price(current_price)}\n"
                f"   PnL: {sign}{pnl_pct:.0f}% ({sign}{pnl_sol:.4f} SOL)"
            )
            buttons.append(InlineKeyboardButton(f"Sell ${symbol}", callback_data=f"menu:sell_{pos_id}"))
        else:
            lines.append(f"${symbol} | {amount_sol:.4f} SOL | PnL: N/A")

        lines.append("")

    total_pnl = total_current - total_invested
    total_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    sign = "+" if total_pct >= 0 else ""
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"Invested: {total_invested:.4f} SOL")
    lines.append(f"Value: {total_current:.4f} SOL")
    lines.append(f"Total: {sign}{total_pct:.0f}% ({sign}{total_pnl:.4f} SOL)")

    # Build button rows (2 per row)
    button_rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    button_rows.append([InlineKeyboardButton("Sell All", callback_data="menu:sellall"),
                        InlineKeyboardButton("Back", callback_data="menu:back")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(button_rows)
    )


async def _show_trades(query) -> None:
    """Show Trades screen: pending, open, and recent closed trades (Trojan-style)."""

    pending = storage.get_pending_positions()
    open_pos = storage.get_open_positions()
    closed = storage.get_closed_positions(limit=10)

    lines = ["TRADES\n━━━━━━━━━━━━━━━━━━\n"]
    buttons = []

    # -- Pending confirmations --
    if pending:
        lines.append("⏳ PENDING CONFIRMATION\n")
        for p in pending:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            age = time.time() - p.get("bought_at", time.time())
            lines.append(f"  ${sym} | {p.get('buy_amount_sol', 0):.4f} SOL | {age:.0f}s ago")
            buttons.append(InlineKeyboardButton(f"#{p['id']} ${sym}", callback_data=f"menu:trade_{p['id']}"))
        lines.append("")

    # -- Open positions --
    if open_pos:
        lines.append(f"🟢 OPEN ({len(open_pos)})\n")
        for p in open_pos:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            tx_st = p.get("tx_status", "?")
            lines.append(f"  ${sym} | {p.get('buy_amount_sol', 0):.4f} SOL | tx: {tx_st}")
            buttons.append(InlineKeyboardButton(f"#{p['id']} ${sym}", callback_data=f"menu:trade_{p['id']}"))
        lines.append("")

    # -- Closed trades --
    if closed:
        lines.append(f"📋 CLOSED (last {len(closed)})\n")
        for p in closed:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            spent = p.get("buy_amount_sol", 0) or 0
            got = p.get("sell_amount_sol") or 0
            pnl_sol = got - spent
            pnl_pct = (pnl_sol / spent * 100) if spent > 0 else 0
            sign = "+" if pnl_pct >= 0 else ""
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(f"  {emoji} ${sym}: {sign}{pnl_pct:.0f}% ({sign}{pnl_sol:.4f} SOL)")
            buttons.append(InlineKeyboardButton(f"#{p['id']} ${sym}", callback_data=f"menu:trade_{p['id']}"))
        lines.append("")

    if not pending and not open_pos and not closed:
        lines.append("No trades yet.\nBuy tokens from alerts or use /buy <token> $amount")

    # Summary
    total_trades = len(open_pos) + len(closed) + len(pending)
    lines.append(f"━━━━━━━━━━━━━━━━━━\nTotal: {total_trades} trades")

    # Button grid (3 per row)
    button_rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    button_rows.append([InlineKeyboardButton("Refresh", callback_data="menu:trades"),
                        InlineKeyboardButton("Back", callback_data="menu:back")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(button_rows)
    )


async def _show_trade_detail(query, pos_id: int) -> None:
    """Show detailed trade timeline for a single position (like Trojan trade view)."""

    pos = storage.get_position_by_id(pos_id)
    if not pos:
        await query.edit_message_text(
            f"Trade #{pos_id} not found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:trades")]])
        )
        return

    sym = pos.get("token_symbol") or pos.get("token_address", "")[:8]
    token_addr = pos.get("token_address", "")
    status = pos.get("status", "?")
    tx_status = pos.get("tx_status", "?")
    buy_sig = pos.get("buy_signature", "")
    sell_sig = pos.get("sell_signature", "")
    bought_at = pos.get("bought_at", 0)
    sold_at = pos.get("sold_at")
    buy_sol = pos.get("buy_amount_sol", 0)
    sell_sol = pos.get("sell_amount_sol")
    entry_price = pos.get("entry_price_usd", 0) or 0
    entry_mc = pos.get("entry_mc", 0) or 0

    def _mc(v):
        if not v: return "N/A"
        return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"

    def _price(v):
        if not v: return "N/A"
        if v < 0.0001: return f"${v:.10f}"
        if v < 0.01: return f"${v:.6f}"
        return f"${v:.4f}"

    def _time(ts):
        if not ts: return "N/A"
        return datetime.datetime.fromtimestamp(ts).strftime("%m/%d %H:%M:%S")

    def _age(ts):
        if not ts: return ""
        secs = time.time() - ts
        if secs < 60: return f"{secs:.0f}s"
        if secs < 3600: return f"{secs/60:.0f}m"
        return f"{secs/3600:.1f}h"

    # Status emoji
    if status == "open":
        status_emoji = "🟢 OPEN"
    elif status == "closed" and sell_sol and sell_sol > buy_sol:
        status_emoji = "🟢 CLOSED (profit)"
    elif status == "closed":
        status_emoji = "🔴 CLOSED"
    else:
        status_emoji = f"⚪ {status.upper()}"

    # PnL
    pnl_line = ""
    if sell_sol is not None:
        pnl_sol = sell_sol - buy_sol
        pnl_pct = (pnl_sol / buy_sol * 100) if buy_sol > 0 else 0
        sign = "+" if pnl_pct >= 0 else ""
        pnl_line = f"PnL: {sign}{pnl_pct:.1f}% ({sign}{pnl_sol:.4f} SOL)"
    elif status == "open":
        pnl = await asyncio.to_thread(executor.check_position_pnl, pos)
        if pnl:
            sign = "+" if pnl["pnl_pct"] >= 0 else ""
            pnl_line = f"Live PnL: {sign}{pnl['pnl_pct']:.1f}% ({sign}{pnl['pnl_sol']:.4f} SOL)"

    lines = [
        f"TRADE #{pos_id} — ${sym}",
        "━━━━━━━━━━━━━━━━━━",
        f"Status: {status_emoji}",
        f"Tx Confirm: {tx_status}",
        "",
        "📥 BUY",
        f"  Amount: {buy_sol:.4f} SOL",
        f"  Entry Price: {_price(entry_price)}",
        f"  Entry MC: {_mc(entry_mc)}",
        f"  Time: {_time(bought_at)} ({_age(bought_at)} ago)",
        f"  Sig: {buy_sig[:24]}..." if buy_sig else "  Sig: N/A",
    ]

    if sell_sig or sold_at:
        lines += [
            "",
            "📤 SELL",
            f"  Received: {sell_sol:.4f} SOL" if sell_sol else "  Received: N/A",
            f"  Time: {_time(sold_at)} ({_age(sold_at)} ago)" if sold_at else "  Time: N/A",
            f"  Sig: {sell_sig[:24]}..." if sell_sig else "  Sig: N/A",
        ]

    if pnl_line:
        lines += ["", pnl_line]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"Token: {token_addr[:20]}...",
        f"Solscan: solscan.io/token/{token_addr[:20]}...",
    ]

    # Action buttons
    action_buttons = []
    if status == "open":
        action_buttons.append(InlineKeyboardButton(f"Sell ${sym}", callback_data=f"menu:sell_{pos_id}"))
    if buy_sig:
        action_buttons.append(InlineKeyboardButton("View Tx", url=f"https://solscan.io/tx/{buy_sig}"))

    button_rows = []
    if action_buttons:
        button_rows.append(action_buttons)
    button_rows.append([InlineKeyboardButton("Back to Trades", callback_data="menu:trades"),
                        InlineKeyboardButton("Main Menu", callback_data="menu:back")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(button_rows)
    )


async def _show_wallet(query) -> None:
    """Show wallet details."""
    wallet = executor.get_wallet_address()
    balance = executor.get_wallet_balance()
    sol_price = executor.get_sol_price()

    wallet_str = wallet if wallet else "Not configured"
    bal_str = f"{balance['sol']:.4f} SOL (${balance['usd']:.2f})" if balance else "N/A"

    text = (
        "WALLET\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Address:\n{wallet_str}\n\n"
        f"Balance: {bal_str}\n"
        f"SOL Price: ${sol_price:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Refresh", callback_data="menu:wallet"),
            InlineKeyboardButton("Back", callback_data="menu:back"),
        ]])
    )


async def _show_watchlist(query) -> None:
    """Show token watchlist with live prices."""
    watchlist = storage.get_watchlist()
    if not watchlist:
        await query.edit_message_text(
            "WATCHLIST\n━━━━━━━━━━━━━━━━━━\nEmpty. Use /watch <token_address> to add.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]])
        )
        return

    lines = ["WATCHLIST\n"]
    buttons = []

    for w in watchlist:
        addr = w["token_address"]
        symbol = w.get("symbol") or addr[:8]
        add_price = w.get("price_at_add", 0) or 0
        add_mc = w.get("mc_at_add", 0) or 0

        # Fetch current price
        import dexscreener_client as dex
        pairs = await asyncio.to_thread(dex.fetch_pair_details, "solana", addr)
        current_price = 0.0
        current_mc = 0
        if pairs:
            current_price = float(pairs[0].get("priceUsd", 0) or 0)
            current_mc = pairs[0].get("marketCap") or pairs[0].get("fdv") or 0
            base = pairs[0].get("baseToken", {})
            symbol = base.get("symbol", symbol)

        # Calculate change since added
        if add_price > 0 and current_price > 0:
            change = ((current_price - add_price) / add_price) * 100
            sign = "+" if change >= 0 else ""
            emoji = "🟢" if change >= 0 else "🔴"
        else:
            change = 0
            sign = ""
            emoji = "⚪"

        def _mc(v):
            return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"

        lines.append(f"{emoji} ${symbol}: {_mc(add_mc)} -> {_mc(current_mc)} ({sign}{change:.0f}%)")
        buttons.append(InlineKeyboardButton(f"Remove ${symbol}", callback_data=f"menu:unwatch_{addr}"))

    button_rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    button_rows.append([InlineKeyboardButton("Back", callback_data="menu:back")])

    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(button_rows))


async def _show_settings(query) -> None:
    """Show trading settings."""
    text = (
        "SETTINGS\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Trading: {'ON' if config.TRADING_ENABLED else 'OFF'}\n"
        f"Auto-Buy: {'ON' if config.AUTO_BUY_ENABLED else 'OFF'}\n"
        f"Auto-Buy Pools: {'ON' if config.AUTO_BUY_NEW_POOLS else 'OFF'}\n"
        f"Buy Amount: ${config.AUTO_BUY_AMOUNT_USD:.0f}\n"
        f"Take Profit: +{config.TAKE_PROFIT_PCT:.0f}%\n"
        f"Stop Loss: {config.STOP_LOSS_PCT:.0f}%\n"
        f"Max Positions: {config.MAX_OPEN_POSITIONS}\n"
        f"Daily Limit: {config.DAILY_LOSS_LIMIT_SOL} SOL\n"
        f"Exit Check: every {config.EXIT_CHECK_INTERVAL}s\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Auto-Buy: " + ("ON" if config.AUTO_BUY_ENABLED else "OFF"), callback_data="menu:autobuy"),
             InlineKeyboardButton("Pools: " + ("ON" if config.AUTO_BUY_NEW_POOLS else "OFF"), callback_data="menu:autobuy_pools")],
            [InlineKeyboardButton("$1", callback_data="menu:set_amount_1"),
             InlineKeyboardButton("$2", callback_data="menu:set_amount_2"),
             InlineKeyboardButton("$3", callback_data="menu:set_amount_3"),
             InlineKeyboardButton("$5", callback_data="menu:set_amount_5")],
            [InlineKeyboardButton("Back", callback_data="menu:back")],
        ])
    )


async def _toggle_autobuy(query) -> None:
    """Toggle auto-buy."""
    config.AUTO_BUY_ENABLED = not config.AUTO_BUY_ENABLED
    state = "ON" if config.AUTO_BUY_ENABLED else "OFF"
    logger.info("[BOT] Auto-buy toggled: %s", state)
    await _handle_start(Update(update_id=0, callback_query=query), None)


async def _sell_position(query, pos_id: int) -> None:
    """Sell a specific position."""
    positions = storage.get_open_positions()
    pos = next((p for p in positions if p.get("id") == pos_id), None)
    if not pos:
        await query.edit_message_text(f"Position #{pos_id} not found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]]))
        return

    symbol = pos.get("token_symbol") or pos["token_address"][:8]
    await query.edit_message_text(f"Selling ${symbol}...")

    result = await asyncio.to_thread(
        executor.sell_token, pos["id"], pos["token_address"], pos["token_amount"]
    )
    if result:
        sol = result["sol_received"]
        pnl_sol = sol - pos["buy_amount_sol"]
        pnl_pct = (pnl_sol / pos["buy_amount_sol"] * 100) if pos["buy_amount_sol"] > 0 else 0
        sign = "+" if pnl_pct >= 0 else ""
        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        await query.edit_message_text(
            f"{emoji} Sold ${symbol}\n"
            f"Received: {sol:.4f} SOL\n"
            f"PnL: {sign}{pnl_pct:.0f}% ({sign}{pnl_sol:.4f} SOL)\n"
            f"Tx: {result['signature'][:20]}...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]])
        )
    else:
        await query.edit_message_text(f"Failed to sell ${symbol}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]]))


async def _sell_all(query) -> None:
    """Sell all open positions."""
    positions = storage.get_open_positions()
    if not positions:
        await query.edit_message_text("No positions to sell.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]]))
        return

    await query.edit_message_text(f"Selling {len(positions)} position(s)...")
    results = []
    for pos in positions:
        result = await asyncio.to_thread(
            executor.sell_token, pos["id"], pos["token_address"], pos["token_amount"]
        )
        symbol = pos.get("token_symbol") or pos["token_address"][:8]
        if result:
            results.append(f"${symbol}: {result['sol_received']:.4f} SOL")
        else:
            results.append(f"${symbol}: FAILED")

    await query.edit_message_text(
        "SOLD ALL\n━━━━━━━━━━━━━━━━━━\n" + "\n".join(results),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]])
    )


async def _stop_trading(query) -> None:
    """Emergency stop."""
    config.TRADING_ENABLED = False
    config.AUTO_BUY_ENABLED = False
    logger.warning("[BOT] EMERGENCY STOP via menu")
    await query.edit_message_text(
        "TRADING STOPPED\n\nAll buying disabled. Open positions will NOT auto-sell.\nSet TRADING_ENABLED=true in Render to re-enable.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:back")]])
    )


# -- Buy Callbacks (from alert buttons) --

async def _handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle buy button press from alerts."""
    query = update.callback_query
    if not query or not query.data:
        return

    # Route menu callbacks
    if query.data.startswith("menu:"):
        await _handle_menu_callback(update, context)
        return

    await query.answer()
    logger.info("[BOT] Buy callback received: data=%s", query.data)

    parts = query.data.split(":")
    if len(parts) == 3 and parts[0] == "buyusd":
        try:
            usd_amount = float(parts[1])
        except ValueError:
            return
        token_mint = parts[2]
        amount_sol = executor.usd_to_sol(usd_amount)
        display_amount = f"${usd_amount:.0f} ({amount_sol:.3f} SOL)"
    elif len(parts) == 2 and parts[0] == "buycustom":
        # Custom amount -- ask user to reply with amount
        token_mint = parts[1]
        context.user_data["pending_buy_token"] = token_mint
        base_text = query.message.text or "Trade"
        await _safe_edit_message(query, base_text + "\n\nType the amount in $ (e.g. 2.5):")
        return
    elif len(parts) == 2 and parts[0] == "buy":
        token_mint = parts[1]
        amount_sol = config.TRADE_AMOUNT_SOL
        display_amount = f"{amount_sol} SOL"
    else:
        return

    if not _is_authorized(update):
        logger.warning("[BOT] Unauthorized buy attempt: user=%s chat=%s", str(query.from_user.id) if query.from_user else "?", str(query.message.chat_id) if query.message else "?")
        await query.answer("Not authorized", show_alert=True)
        return

    allowed, reason = executor.can_trade()
    if not allowed:
        logger.info("[BOT] Buy blocked by safety rails: %s", reason)
        await query.answer(f"Blocked: {reason}", show_alert=True)
        return

    sol_price = executor.get_sol_price()
    base_text = query.message.text or "Trade"
    await _safe_edit_message(query, base_text + f"\n\nBuying {display_amount}...")

    try:
        result = await asyncio.to_thread(executor.buy_token, token_mint, amount_sol)
    except Exception:
        logger.exception("[BOT] Buy execution crashed for %s", token_mint)
        await _safe_edit_message(query, base_text + "\n\nBuy failed due to internal error. Please retry.")
        return

    if result:
        sig = result.get("signature", "")[:16]
        spent = result.get("amount_sol", 0)
        impact = result.get("price_impact_pct", 0)
        usd_spent = spent * sol_price
        position_recorded = bool(result.get("position_recorded", True))
        if position_recorded:
            msg = base_text + f"\n\nBought ${usd_spent:.0f} ({spent:.3f} SOL) | impact: {impact:.1f}% | tx: {sig}...\nAdded to portfolio."
        else:
            msg = base_text + f"\n\nTx submitted (${usd_spent:.0f}, {spent:.3f} SOL) | tx: {sig}...\nWarning: portfolio save failed. Check logs."
        await _safe_edit_message(query, msg)
    else:
        await _safe_edit_message(query, base_text + "\n\nBuy failed. Check logs for reason.")


# -- Text input handler (for custom buy amounts) --

async def _handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages -- used for custom buy amount input."""
    if not update.message or not update.message.text:
        return

    # Check if there's a pending custom buy
    token_mint = context.user_data.get("pending_buy_token")
    if not token_mint:
        return  # No pending action, ignore

    text = update.message.text.strip().replace("$", "").replace(",", "")
    try:
        usd_amount = float(text)
    except ValueError:
        await update.message.reply_text("Invalid amount. Send a number (e.g. 2.5)")
        return

    if usd_amount <= 0 or usd_amount > 100:
        await update.message.reply_text("Amount must be $0.50 - $100")
        return

    # Clear pending state
    del context.user_data["pending_buy_token"]

    # Execute buy
    amount_sol = executor.usd_to_sol(usd_amount)
    logger.info("[BOT] Custom buy requested: token=%s usd=%.2f", token_mint[:16], usd_amount)
    allowed, reason = executor.can_trade()
    if not allowed:
        await update.message.reply_text(f"Buy blocked: {reason}")
        return

    sol_price = executor.get_sol_price()
    await update.message.reply_text(f"Buying ${usd_amount:.2f} ({amount_sol:.4f} SOL)...")

    try:
        result = await asyncio.to_thread(executor.buy_token, token_mint, amount_sol)
    except Exception:
        logger.exception("[BOT] Custom buy execution crashed for %s", token_mint)
        await update.message.reply_text("Buy failed due to internal error. Please retry.")
        return
    if result:
        sig = result.get("signature", "")[:16]
        impact = result.get("price_impact_pct", 0)
        if result.get("position_recorded", True):
            await update.message.reply_text(
                f"Bought ${usd_amount:.2f} ({amount_sol:.4f} SOL)\n"
                f"Impact: {impact:.1f}%\n"
                f"Tx: {sig}...\n"
                "Added to portfolio."
            )
        else:
            await update.message.reply_text(
                f"Tx submitted for ${usd_amount:.2f} ({amount_sol:.4f} SOL)\n"
                f"Tx: {sig}...\n"
                "Warning: portfolio save failed."
            )
    else:
        await update.message.reply_text("Buy failed -- check logs")


# -- Legacy text commands --

async def _handle_buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /buy <token> <$amount>."""
    if not update.message:
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /buy <token_address> $5")
        return

    token_mint = args[0]
    try:
        usd = float(args[1].replace("$", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Invalid amount")
        return

    if usd <= 0 or usd > 100:
        await update.message.reply_text("Amount: $0.50 - $100")
        return

    amount_sol = executor.usd_to_sol(usd)
    allowed, reason = executor.can_trade()
    if not allowed:
        await update.message.reply_text(f"Blocked: {reason}")
        return

    logger.info("[BOT] Command buy requested: token=%s usd=%.2f", token_mint[:16], usd)
    await update.message.reply_text(f"Buying ${usd:.0f} ({amount_sol:.3f} SOL)...")
    try:
        result = await asyncio.to_thread(executor.buy_token, token_mint, amount_sol)
    except Exception:
        logger.exception("[BOT] Command buy execution crashed for %s", token_mint)
        await update.message.reply_text("Buy failed due to internal error. Please retry.")
        return
    if result:
        if result.get("position_recorded", True):
            await update.message.reply_text(f"Bought! tx: {result['signature'][:20]}... (added to portfolio)")
        else:
            await update.message.reply_text(f"Tx sent: {result['signature'][:20]}... but portfolio save failed")
    else:
        await update.message.reply_text("Buy failed")


async def _handle_bot_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error hook so callback failures are always visible in logs."""
    logger.exception("[BOT] Unhandled exception in update handler", exc_info=context.error)


async def _handle_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trades command — show trade history inline."""
    if not update.message:
        return

    pending = storage.get_pending_positions()
    open_pos = storage.get_open_positions()
    closed = storage.get_closed_positions(limit=10)

    lines = ["TRADES\n━━━━━━━━━━━━━━━━━━\n"]

    if pending:
        lines.append("⏳ PENDING\n")
        for p in pending:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            age = time.time() - p.get("bought_at", time.time())
            lines.append(f"  #{p['id']} ${sym} | {p.get('buy_amount_sol', 0):.4f} SOL | {age:.0f}s ago")
        lines.append("")

    if open_pos:
        lines.append(f"🟢 OPEN ({len(open_pos)})\n")
        for p in open_pos:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            lines.append(f"  #{p['id']} ${sym} | {p.get('buy_amount_sol', 0):.4f} SOL | tx: {p.get('tx_status', '?')}")
        lines.append("")

    if closed:
        lines.append(f"📋 CLOSED (last {len(closed)})\n")
        for p in closed:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            spent = p.get("buy_amount_sol", 0) or 0
            got = p.get("sell_amount_sol") or 0
            pnl_sol = got - spent
            pnl_pct = (pnl_sol / spent * 100) if spent > 0 else 0
            sign = "+" if pnl_pct >= 0 else ""
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(f"  {emoji} #{p['id']} ${sym}: {sign}{pnl_pct:.0f}% ({sign}{pnl_sol:.4f} SOL)")
        lines.append("")

    if not pending and not open_pos and not closed:
        lines.append("No trades yet.")

    lines.append("━━━━━━━━━━━━━━━━━━\nUse /trade <id> for details")
    await update.message.reply_text("\n".join(lines))


async def _handle_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trade <id> — show detailed trade view."""
    if not update.message:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /trade <id>\nUse /trades to see all trades with IDs.")
        return

    try:
        pos_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Invalid trade ID. Use a number from /trades.")
        return

    pos = storage.get_position_by_id(pos_id)
    if not pos:
        await update.message.reply_text(f"Trade #{pos_id} not found.")
        return

    sym = pos.get("token_symbol") or pos.get("token_address", "")[:8]
    token_addr = pos.get("token_address", "")
    status = pos.get("status", "?")
    tx_status = pos.get("tx_status", "?")
    buy_sig = pos.get("buy_signature", "")
    sell_sig = pos.get("sell_signature", "")
    bought_at = pos.get("bought_at", 0)
    sold_at = pos.get("sold_at")
    buy_sol = pos.get("buy_amount_sol", 0)
    sell_sol = pos.get("sell_amount_sol")
    entry_price = pos.get("entry_price_usd", 0) or 0
    entry_mc = pos.get("entry_mc", 0) or 0

    def _mc(v):
        if not v: return "N/A"
        return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"

    def _price(v):
        if not v: return "N/A"
        if v < 0.0001: return f"${v:.10f}"
        if v < 0.01: return f"${v:.6f}"
        return f"${v:.4f}"

    def _ts(ts):
        if not ts: return "N/A"
        return datetime.datetime.fromtimestamp(ts).strftime("%m/%d %H:%M:%S")

    def _age(ts):
        if not ts: return ""
        secs = time.time() - ts
        if secs < 60: return f"{secs:.0f}s"
        if secs < 3600: return f"{secs/60:.0f}m"
        return f"{secs/3600:.1f}h"

    # Status line
    if status == "open":
        status_line = "🟢 OPEN"
    elif status == "closed" and sell_sol and sell_sol > buy_sol:
        status_line = "🟢 CLOSED (profit)"
    elif status == "closed":
        status_line = "🔴 CLOSED (loss)"
    else:
        status_line = f"⚪ {status.upper()}"

    # PnL
    pnl_line = ""
    if sell_sol is not None:
        pnl_sol_v = sell_sol - buy_sol
        pnl_pct = (pnl_sol_v / buy_sol * 100) if buy_sol > 0 else 0
        sign = "+" if pnl_pct >= 0 else ""
        pnl_line = f"PnL: {sign}{pnl_pct:.1f}% ({sign}{pnl_sol_v:.4f} SOL)"
    elif status == "open":
        pnl = await asyncio.to_thread(executor.check_position_pnl, pos)
        if pnl:
            sign = "+" if pnl["pnl_pct"] >= 0 else ""
            pnl_line = f"Live PnL: {sign}{pnl['pnl_pct']:.1f}% ({sign}{pnl['pnl_sol']:.4f} SOL)"

    lines = [
        f"TRADE #{pos_id} — ${sym}",
        "━━━━━━━━━━━━━━━━━━",
        f"Status: {status_line}",
        f"Tx Confirm: {tx_status}",
        "",
        "📥 BUY",
        f"  Amount: {buy_sol:.4f} SOL",
        f"  Entry Price: {_price(entry_price)}",
        f"  Entry MC: {_mc(entry_mc)}",
        f"  Time: {_ts(bought_at)} ({_age(bought_at)} ago)",
        f"  Tx: solscan.io/tx/{buy_sig[:32]}..." if buy_sig else "  Tx: N/A",
    ]

    if sell_sig or sold_at:
        lines += [
            "",
            "📤 SELL",
            f"  Received: {sell_sol:.4f} SOL" if sell_sol else "  Received: N/A",
            f"  Time: {_ts(sold_at)} ({_age(sold_at)} ago)" if sold_at else "  Time: N/A",
            f"  Tx: solscan.io/tx/{sell_sig[:32]}..." if sell_sig else "  Tx: N/A",
        ]

    if pnl_line:
        lines += ["", pnl_line]

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"Token: {token_addr}",
    ]

    await update.message.reply_text("\n".join(lines))


async def _handle_sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sell [id]."""
    if not update.message:
        return
    args = context.args
    positions = storage.get_open_positions()

    if not args:
        if not positions:
            await update.message.reply_text("No positions")
            return
        await update.message.reply_text(f"Selling {len(positions)} position(s)...")
        for pos in positions:
            result = await asyncio.to_thread(executor.sell_token, pos["id"], pos["token_address"], pos["token_amount"])
            sym = pos.get("token_symbol") or "?"
            if result:
                await update.message.reply_text(f"Sold ${sym}: {result['sol_received']:.4f} SOL")
            else:
                await update.message.reply_text(f"Failed: ${sym}")
        return

    try:
        pos_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Usage: /sell <id> or /sell")
        return

    pos = next((p for p in positions if p["id"] == pos_id), None)
    if not pos:
        await update.message.reply_text(f"Position #{pos_id} not found")
        return

    result = await asyncio.to_thread(executor.sell_token, pos["id"], pos["token_address"], pos["token_amount"])
    if result:
        await update.message.reply_text(f"Sold for {result['sol_received']:.4f} SOL")
    else:
        await update.message.reply_text("Sell failed")


async def _handle_watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /watch <token_address> — add token to watchlist."""
    if not update.message:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /watch <token_address>")
        return

    token_addr = args[0]

    # Fetch current data from DexScreener
    import dexscreener_client as dex
    pairs = await asyncio.to_thread(dex.fetch_pair_details, "solana", token_addr)
    price = 0.0
    mc = 0
    symbol = token_addr[:8]
    if pairs:
        price = float(pairs[0].get("priceUsd", 0) or 0)
        mc = pairs[0].get("marketCap") or pairs[0].get("fdv") or 0
        base = pairs[0].get("baseToken", {})
        symbol = base.get("symbol", symbol)

    storage.add_to_watchlist(token_addr, symbol, price, mc)

    def _mc(v):
        return f"${v/1000:.0f}K" if v >= 1000 else f"${v:.0f}"

    await update.message.reply_text(f"Added ${symbol} to watchlist\nMC: {_mc(mc)}\nUse /start -> Watchlist to view")


# -- Start Bot --

async def start_bot_handler() -> None:
    """Start the Telegram bot with menu commands."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("[BOT] No TELEGRAM_BOT_TOKEN")
        return

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Set command suggestions in Telegram
    await app.bot.set_my_commands([
        BotCommand("start", "Main menu"),
        BotCommand("positions", "View portfolio & PnL"),
        BotCommand("trades", "Trade history & status"),
        BotCommand("trade", "Trade detail: /trade <id>"),
        BotCommand("buy", "Buy token: /buy <address> $5"),
        BotCommand("sell", "Sell: /sell <id> or /sell all"),
        BotCommand("watch", "Watch token: /watch <address>"),
        BotCommand("autobuy", "Toggle auto-buy"),
        BotCommand("stop", "Emergency stop all trading"),
    ])

    # Register handlers
    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(CommandHandler("positions", lambda u, c: _handle_start(u, c)))
    app.add_handler(CommandHandler("status", lambda u, c: _handle_start(u, c)))
    app.add_handler(CommandHandler("trades", _handle_trades_command))
    app.add_handler(CommandHandler("trade", _handle_trade_command))
    app.add_handler(CommandHandler("buy", _handle_buy_command))
    app.add_handler(CommandHandler("sell", _handle_sell_command))
    app.add_handler(CommandHandler("watch", _handle_watch_command))
    app.add_handler(CommandHandler("autobuy", lambda u, c: u.message.reply_text(
        f"Auto-buy: {'ON -> OFF' if config.AUTO_BUY_ENABLED else 'OFF -> ON'}",
    ) if (setattr(config, 'AUTO_BUY_ENABLED', not config.AUTO_BUY_ENABLED) or True) else None))
    app.add_handler(CommandHandler("stop", lambda u, c: (
        setattr(config, 'TRADING_ENABLED', False),
        setattr(config, 'AUTO_BUY_ENABLED', False),
        u.message.reply_text("STOPPED. All trading disabled.")
    )[-1]))
    app.add_handler(CallbackQueryHandler(_handle_buy_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text_input))
    app.add_error_handler(_handle_bot_error)

    logger.info("[BOT] Telegram UI started with command menu")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
