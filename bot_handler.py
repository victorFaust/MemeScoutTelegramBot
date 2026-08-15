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

MIN_BUY_USD = 0.50
MAX_BUY_USD = 100.0


def _is_valid_solana_address(value: str) -> bool:
    """Return whether value is a canonical Solana public key."""
    try:
        from solders.pubkey import Pubkey  # type: ignore
        Pubkey.from_string(value)
        return True
    except Exception:
        return False


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
        [InlineKeyboardButton("🟢 Buy", callback_data="menu:buy"),
         InlineKeyboardButton("🔴 Sell", callback_data="menu:positions")],
        [InlineKeyboardButton("📊 Positions", callback_data="menu:positions"),
         InlineKeyboardButton("🎯 Orders", callback_data="menu:orders")],
        [InlineKeyboardButton("👛 Wallets", callback_data="menu:wallet"),
         InlineKeyboardButton("⚙️ Profiles", callback_data="menu:profiles")],
        [InlineKeyboardButton("👁 Watchlist", callback_data="menu:watchlist"),
         InlineKeyboardButton("📜 History", callback_data="menu:trades")],
        [InlineKeyboardButton("🤖 Auto-Buy: " + ("ON" if config.AUTO_BUY_ENABLED else "OFF"), callback_data="menu:autobuy"),
         InlineKeyboardButton("🚨 Stop", callback_data="menu:stop")],
    ])


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show main menu."""
    if not _is_authorized(update):
        if update.message:
            await update.message.reply_text("Not authorized")
        return
    wallet = executor.get_wallet_address()
    balance = executor.get_wallet_balance()
    positions = storage.get_open_positions_count()
    active_orders = len(storage.get_active_trade_orders())
    mismatches = sum(1 for p in storage.get_open_positions()
                     if p.get("reconciliation_status") in {"balance_missing", "balance_mismatch"})

    wallet_str = f"{wallet[:6]}...{wallet[-4:]}" if wallet else "Not set"
    bal_str = f"{balance['sol']:.4f} SOL (${balance['usd']:.2f})" if balance else "N/A"

    text = (
        "⚡ MEMESCOUT TRADING\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👛 {wallet_str} · {bal_str}\n"
        f"📊 {positions}/{config.MAX_OPEN_POSITIONS} positions · 🎯 {active_orders} orders\n"
        f"Trading {'🟢 ON' if config.TRADING_ENABLED else '🔴 OFF'} · Auto {'🟢 ON' if config.AUTO_BUY_ENABLED else '⚪ OFF'}\n"
        + (f"⚠️ {mismatches} balance mismatch(es) need review\n" if mismatches else "✅ Portfolio reconciled\n") +
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

    if not _is_authorized(update):
        logger.warning("[BOT] Unauthorized menu access blocked")
        await query.answer("Not authorized", show_alert=True)
        return

    action = query.data.split(":", 1)[1] if ":" in query.data else query.data
    if (action in {"sellall", "sellallconfirm", "stop", "autobuy", "autobuy_pools"}
            or action.startswith(("sell_", "set_amount_", "cancelorder_"))):
        if not _is_authorized(update):
            logger.warning("[BOT] Unauthorized menu action blocked: %s", action)
            await query.answer("Not authorized", show_alert=True)
            return

    await query.answer()

    if action == "positions":
        await _show_positions(query)
    elif action == "buy":
        await _show_buy(query)
    elif action == "orders":
        await _show_orders(query)
    elif action == "profiles":
        await _show_profiles(query)
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
        positions = [p for p in storage.get_open_positions() if _position_is_sellable(p)]
        await query.edit_message_text(
            f"⚠️ Sell all {len(positions)} confirmed position(s)?\nThis submits on-chain market sells and cannot be undone.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Confirm Sell All", callback_data="menu:sellallconfirm")],
                [InlineKeyboardButton("Cancel", callback_data="menu:positions")],
            ]),
        )
    elif action == "sellallconfirm":
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
    elif action.startswith("cancelorder_"):
        order_id = int(action.split("_")[1])
        storage.cancel_trade_order(order_id)
        await _show_orders(query)


async def _show_buy(query) -> None:
    await query.edit_message_text(
        "🟢 BUY\n━━━━━━━━━━━━━━━━━━\n"
        "Market: /buy <token> $amount\n"
        "Limit: /limitbuy <token> <price_usd> $amount\n\n"
        "Alert cards also provide quick-buy presets. Orders remain active across restarts.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Dashboard", callback_data="menu:back")]]),
    )


async def _show_orders(query) -> None:
    orders = storage.get_trade_orders(limit=20)
    lines = ["🎯 ORDERS", "━━━━━━━━━━━━━━━━━━"]
    buttons = []
    if not orders:
        lines.append("No orders yet.\nUse /limitbuy or open a confirmed position to create exits.")
    for order in orders:
        symbol = order.get("token_symbol") or order["token_address"][:8]
        kind = order["order_type"].replace("_", " ").title()
        trigger = order["trigger_value"]
        trigger_text = f"${trigger:.10g}" if order["order_type"] == "limit_buy" else f"{trigger:+.0f}%"
        icon = {"active": "🟢", "executing": "🟡", "completed": "✅", "cancelled": "⚪"}.get(order["status"], "⚠️")
        lines.append(f"{icon} #{order['id']} {kind} · ${symbol} · {trigger_text}")
        if order["status"] == "active":
            buttons.append(InlineKeyboardButton(f"Cancel #{order['id']}", callback_data=f"menu:cancelorder_{order['id']}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="menu:orders"),
                 InlineKeyboardButton("⬅️ Dashboard", callback_data="menu:back")])
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def _show_profiles(query) -> None:
    wallets = executor.get_all_wallet_addresses()
    lines = ["⚙️ EXECUTION PROFILES", "━━━━━━━━━━━━━━━━━━"]
    if not wallets:
        lines.append("No trading wallet configured.")
    for index, wallet in enumerate(wallets, 1):
        profile = storage.get_wallet_profile(wallet)
        lines += [
            f"W{index} · {wallet[:6]}...{wallet[-4:]}",
            f"  Slippage {profile['slippage_bps']/100:.1f}% · Priority {profile['priority_fee_lamports']} lamports",
            f"  MEV {'ON' if profile['mev_protection'] else 'OFF'} · Jito tip {profile['jito_tip_lamports']} · Buys ${profile['buy_presets_usd']}",
        ]
    lines += ["", "Edit: /profile <wallet#> <slippage%> <priority> <jito_tip> <on|off> [presets]"]
    await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ Dashboard", callback_data="menu:back")
    ]]))


async def _send_positions(target, text: str, reply_markup) -> None:
    """Render positions to either a callback query or a command message."""
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=reply_markup)
    else:
        await target.reply_text(text, reply_markup=reply_markup)


def _position_is_sellable(position: dict) -> bool:
    """Only offer sells when a buy may actually have delivered tokens."""
    return (
        position.get("status") == "open"
        and (position.get("token_amount", 0) or 0) > 0
        and position.get("tx_status", "confirmed") in {"confirmed", "finalized"}
    )


async def _show_positions(target) -> None:
    """Show portfolio with live PnL."""
    positions = storage.get_open_positions()
    if not positions:
        recent = storage.get_recent_positions(limit=5)
        if not recent:
            await _send_positions(target,
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

        await _send_positions(target,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back", callback_data="menu:back")
            ]])
        )
        return

    lines = ["PORTFOLIO\n"]
    total_invested = 0.0
    valued_invested = 0.0
    total_current = 0.0
    unavailable = 0
    buttons = []

    for i, p in enumerate(positions):
        token_addr = p.get("token_address", "?")
        amount_sol = p.get("buy_amount_sol", 0) or 0
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
            valued_invested += amount_sol
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
        else:
            unavailable += 1
            tx_status = p.get("tx_status", "?")
            lines.append(f"⚪ ${symbol} | {amount_sol:.4f} SOL | PnL: unavailable | tx: {tx_status}")

        if _position_is_sellable(p):
            buttons.append(InlineKeyboardButton(f"Sell ${symbol}", callback_data=f"menu:sell_{pos_id}"))

        lines.append("")

    total_pnl = total_current - valued_invested
    total_pct = (total_pnl / valued_invested * 100) if valued_invested > 0 else 0
    sign = "+" if total_pct >= 0 else ""
    lines.append(f"━━━━━━━━━━━━━━━━━━")
    lines.append(f"Invested: {total_invested:.4f} SOL")
    value_suffix = f" | {unavailable} unavailable" if unavailable else ""
    lines.append(f"Quoted value: {total_current:.4f} SOL{value_suffix}")
    if valued_invested > 0:
        lines.append(f"Quoted PnL: {sign}{total_pct:.0f}% ({sign}{total_pnl:.4f} SOL)")
    else:
        lines.append("Quoted PnL: N/A")

    # Build button rows (2 per row)
    button_rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    footer = [InlineKeyboardButton("Back", callback_data="menu:back")]
    if any(_position_is_sellable(p) for p in positions):
        footer.insert(0, InlineKeyboardButton("Sell All", callback_data="menu:sellall"))
    button_rows.append(footer)

    await _send_positions(target,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(button_rows)
    )


async def _handle_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the portfolio directly for /positions."""
    if update.message and not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    if update.message:
        await _show_positions(update.message)


async def _show_trades(query) -> None:
    """Show Trades screen: pending, open, and recent closed trades (Trojan-style)."""

    pending = storage.get_pending_positions()
    open_pos = [p for p in storage.get_open_positions()
                if p.get("tx_status") != "pending"]
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
    if _position_is_sellable(pos):
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
    """Show wallet details (all configured wallets when multi-wallet mode active)."""
    all_wallets = executor.get_all_wallet_addresses()
    balance = executor.get_wallet_balance()
    sol_price = executor.get_sol_price()

    bal_str = f"{balance['sol']:.4f} SOL (${balance['usd']:.2f})" if balance else "N/A"

    lines = [
        "WALLET\n━━━━━━━━━━━━━━━━━━",
        f"Balance: {bal_str}",
        f"SOL Price: ${sol_price:.2f}",
        "",
    ]
    if len(all_wallets) > 1:
        lines.append(f"Multi-wallet mode ({len(all_wallets)} wallets, round-robin):")
        for i, addr in enumerate(all_wallets):
            lines.append(f"  W{i+1}: {addr[:8]}...{addr[-6:]}")
    else:
        wallet = all_wallets[0] if all_wallets else "Not configured"
        lines.append(f"Address:\n{wallet}")
    lines.append("━━━━━━━━━━━━━━━━━━")

    await query.edit_message_text(
        "\n".join(lines),
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
    if not _position_is_sellable(pos):
        await query.edit_message_text(
            f"Position #{pos_id} is not sellable while transaction status is {pos.get('tx_status', '?')}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="menu:positions")]]),
        )
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
    positions = [p for p in storage.get_open_positions() if _position_is_sellable(p)]
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

    if not _is_authorized(update):
        logger.warning("[BOT] Unauthorized buy attempt blocked")
        await query.answer("Not authorized", show_alert=True)
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
        if not MIN_BUY_USD <= usd_amount <= MAX_BUY_USD:
            await _safe_edit_message(query, "Buy amount must be $0.50 - $100")
            return
        amount_sol = executor.usd_to_sol(usd_amount)
        if amount_sol <= 0:
            await _safe_edit_message(query, "SOL price is unavailable. Please try again shortly.")
            return
        display_amount = f"${usd_amount:.0f} ({amount_sol:.3f} SOL)"
    elif len(parts) == 2 and parts[0] == "buycustom":
        # Custom amount -- ask user to reply with amount
        token_mint = parts[1]
        if not _is_valid_solana_address(token_mint):
            await _safe_edit_message(query, "Invalid Solana token address.")
            return
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

    if not _is_valid_solana_address(token_mint):
        await _safe_edit_message(query, "Invalid Solana token address.")
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
            msg = base_text + f"\n\nBuy submitted: ${usd_spent:.0f} ({spent:.3f} SOL) | impact: {impact:.1f}% | tx: {sig}...\nAwaiting on-chain confirmation; added to portfolio."
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

    if not _is_authorized(update):
        context.user_data.pop("pending_buy_token", None)
        logger.warning("[BOT] Unauthorized custom buy attempt blocked")
        await update.message.reply_text("Not authorized")
        return

    text = update.message.text.strip().replace("$", "").replace(",", "")
    try:
        usd_amount = float(text)
    except ValueError:
        await update.message.reply_text("Invalid amount. Send a number (e.g. 2.5)")
        return

    if not MIN_BUY_USD <= usd_amount <= MAX_BUY_USD:
        await update.message.reply_text("Amount must be $0.50 - $100")
        return

    # Clear pending state
    del context.user_data["pending_buy_token"]

    # Execute buy
    amount_sol = executor.usd_to_sol(usd_amount)
    if amount_sol <= 0:
        await update.message.reply_text("SOL price is unavailable. Please try again shortly.")
        return
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
                f"Buy submitted: ${usd_amount:.2f} ({amount_sol:.4f} SOL)\n"
                f"Impact: {impact:.1f}%\n"
                f"Tx: {sig}...\n"
                "Awaiting on-chain confirmation; added to portfolio."
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
    if not _is_authorized(update):
        logger.warning("[BOT] Unauthorized /buy attempt blocked")
        await update.message.reply_text("Not authorized")
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

    if not MIN_BUY_USD <= usd <= MAX_BUY_USD:
        await update.message.reply_text("Amount: $0.50 - $100")
        return

    if not _is_valid_solana_address(token_mint):
        await update.message.reply_text("Invalid Solana token address")
        return

    amount_sol = executor.usd_to_sol(usd)
    if amount_sol <= 0:
        await update.message.reply_text("SOL price is unavailable. Please try again shortly.")
        return
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
            await update.message.reply_text(f"Buy submitted: {result['signature'][:20]}... (awaiting confirmation; added to portfolio)")
        else:
            await update.message.reply_text(f"Tx sent: {result['signature'][:20]}... but portfolio save failed")
    else:
        await update.message.reply_text("Buy failed")


async def _handle_limitbuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a persistent limit buy: /limitbuy <mint> <price_usd> <$amount>."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    if len(context.args) < 3:
        await update.message.reply_text("Usage: /limitbuy <token_address> <price_usd> $5")
        return
    mint = context.args[0]
    if not _is_valid_solana_address(mint):
        await update.message.reply_text("Invalid Solana token address")
        return
    try:
        target_price = float(context.args[1].replace("$", ""))
        usd = float(context.args[2].replace("$", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Price and amount must be numbers")
        return
    if target_price <= 0 or not MIN_BUY_USD <= usd <= MAX_BUY_USD:
        await update.message.reply_text("Price must be positive and amount must be $0.50 - $100")
        return
    amount_sol = executor.usd_to_sol(usd)
    if amount_sol <= 0:
        await update.message.reply_text("SOL price is unavailable. Please try again shortly.")
        return
    order_id = storage.create_trade_order(
        mint, "limit_buy", target_price, amount_sol=amount_sol
    )
    await update.message.reply_text(
        f"🎯 Limit buy #{order_id} created\nTrigger: ${target_price:.10g}\nSpend: ${usd:.2f} ({amount_sol:.4f} SOL)\nUse /orders to manage it."
    )


async def _handle_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    orders = storage.get_trade_orders(limit=20)
    if not orders:
        await update.message.reply_text("No orders. Use /limitbuy to create one.")
        return
    lines = ["🎯 ORDERS", "━━━━━━━━━━━━━━━━━━"]
    for order in orders:
        symbol = order.get("token_symbol") or order["token_address"][:8]
        trigger = (f"${order['trigger_value']:.10g}" if order["order_type"] == "limit_buy"
                   else f"{order['trigger_value']:+.0f}%")
        lines.append(f"#{order['id']} · {order['order_type'].replace('_', ' ')} · ${symbol} · {trigger} · {order['status']}")
    lines.append("\nCancel: /cancelorder <id>")
    await update.message.reply_text("\n".join(lines))


async def _handle_cancelorder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    try:
        order_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /cancelorder <id>")
        return
    message = f"Order #{order_id} cancelled" if storage.cancel_trade_order(order_id) else f"Active order #{order_id} not found"
    await update.message.reply_text(message)


async def _handle_profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View or update a wallet execution profile."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    wallets = executor.get_all_wallet_addresses()
    if len(context.args) < 5:
        await update.message.reply_text(
            "Usage: /profile <wallet#> <slippage%> <priority_lamports> <jito_tip> <on|off> [buy_presets]\n"
            "Example: /profile 1 5 100000 10000 on 1,2,3,5"
        )
        return
    try:
        wallet_index = int(context.args[0]) - 1
        slippage_pct = float(context.args[1])
        priority = int(context.args[2])
        jito_tip = int(context.args[3])
        mev = context.args[4].lower() in {"on", "true", "1", "yes"}
        wallet = wallets[wallet_index]
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid wallet number or numeric setting")
        return
    if not 0.1 <= slippage_pct <= 50 or not 0 <= priority <= 10_000_000 or not 0 <= jito_tip <= 10_000_000:
        await update.message.reply_text("Slippage must be 0.1-50%; fees must be 0-10,000,000 lamports")
        return
    presets = context.args[5] if len(context.args) > 5 else storage.get_wallet_profile(wallet)["buy_presets_usd"]
    try:
        preset_values = [float(value) for value in presets.split(",")]
        if not preset_values or any(not MIN_BUY_USD <= value <= MAX_BUY_USD for value in preset_values):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Presets must be comma-separated amounts from $0.50 to $100")
        return
    profile = storage.update_wallet_profile(
        wallet, slippage_bps=round(slippage_pct * 100), priority_fee_lamports=priority,
        jito_tip_lamports=jito_tip, mev_protection=int(mev), buy_presets_usd=presets,
    )
    await update.message.reply_text(
        f"✅ W{wallet_index + 1} profile saved\nSlippage {profile['slippage_bps']/100:.1f}% · MEV {'ON' if mev else 'OFF'}"
    )


async def _handle_autobuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_authorized(update):
        if update.message:
            await update.message.reply_text("Not authorized")
        return
    config.AUTO_BUY_ENABLED = not config.AUTO_BUY_ENABLED
    await update.message.reply_text(f"Auto-buy is now {'ON' if config.AUTO_BUY_ENABLED else 'OFF'}")


async def _handle_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not _is_authorized(update):
        if update.message:
            await update.message.reply_text("Not authorized")
        return
    config.TRADING_ENABLED = False
    config.AUTO_BUY_ENABLED = False
    await update.message.reply_text("🚨 Trading stopped. Buys, sells, and order execution are disabled.")


async def _handle_bot_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error hook so callback failures are always visible in logs."""
    logger.exception("[BOT] Unhandled exception in update handler", exc_info=context.error)


async def _handle_trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /trades command — show trade history inline."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
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


async def _handle_pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pnl — show realized + unrealized PnL summary."""
    if not update.message:
        return

    now_ts = time.time()
    day_start = now_ts - 86400
    week_start = now_ts - 7 * 86400

    closed_today = storage.get_closed_positions_since(day_start)
    closed_week = storage.get_closed_positions_since(week_start)
    open_pos = storage.get_open_positions()

    def _pnl_stats(positions):
        realized = sum((p.get("sell_amount_sol") or 0) - (p.get("buy_amount_sol") or 0)
                       for p in positions)
        wins = sum(1 for p in positions
                   if (p.get("sell_amount_sol") or 0) > (p.get("buy_amount_sol") or 0))
        wr = wins / len(positions) * 100 if positions else 0
        return realized, wr, len(positions)

    r_day, wr_day, n_day = _pnl_stats(closed_today)
    r_week, wr_week, n_week = _pnl_stats(closed_week)

    lines = ["💰 PnL DASHBOARD\n━━━━━━━━━━━━━━━━━━\n"]

    lines.append(f"📅 Today ({n_day} trades)")
    s = "+" if r_day >= 0 else ""
    e = "🟢" if r_day >= 0 else "🔴"
    lines.append(f"  {e} Realized: {s}{r_day:.4f} SOL | WR: {wr_day:.0f}%")

    lines.append(f"\n📆 Last 7 Days ({n_week} trades)")
    s = "+" if r_week >= 0 else ""
    e = "🟢" if r_week >= 0 else "🔴"
    lines.append(f"  {e} Realized: {s}{r_week:.4f} SOL | WR: {wr_week:.0f}%")

    if open_pos:
        total_in = sum(p.get("buy_amount_sol", 0) for p in open_pos)
        lines.append(f"\n📈 Open Positions ({len(open_pos)})")
        lines.append(f"  Invested: {total_in:.4f} SOL")
        for p in open_pos:
            sym = p.get("token_symbol") or p.get("token_address", "")[:8]
            bought = p.get("buy_amount_sol", 0)
            pnl = await asyncio.to_thread(executor.check_position_pnl, p)
            if pnl:
                s = "+" if pnl["pnl_pct"] >= 0 else ""
                e = "🟢" if pnl["pnl_pct"] >= 0 else "🔴"
                lines.append(f"  {e} ${sym}: {s}{pnl['pnl_pct']:.0f}% ({pnl['current_value_sol']:.4f} SOL)")
            else:
                lines.append(f"  ⚪ ${sym}: {bought:.4f} SOL (no quote)")

    if closed_week:
        best = max(closed_week, key=lambda p: (p.get("sell_amount_sol") or 0) - (p.get("buy_amount_sol") or 0))
        worst = min(closed_week, key=lambda p: (p.get("sell_amount_sol") or 0) - (p.get("buy_amount_sol") or 0))
        best_sym = best.get("token_symbol") or best.get("token_address", "")[:8]
        worst_sym = worst.get("token_symbol") or worst.get("token_address", "")[:8]
        best_pnl = (best.get("sell_amount_sol") or 0) - (best.get("buy_amount_sol") or 0)
        worst_pnl = (worst.get("sell_amount_sol") or 0) - (worst.get("buy_amount_sol") or 0)
        lines.append(f"\n🏆 Best (7d): ${best_sym} {'+' if best_pnl >= 0 else ''}{best_pnl:.4f} SOL")
        lines.append(f"📉 Worst (7d): ${worst_sym} {'+' if worst_pnl >= 0 else ''}{worst_pnl:.4f} SOL")

    lines.append("\n━━━━━━━━━━━━━━━━━━")
    await update.message.reply_text("\n".join(lines))


async def _handle_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /report [days] — send performance report to Telegram."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return

    days = 7
    if context.args:
        try:
            days = int(context.args[0])
        except ValueError:
            pass

    rows = storage.get_outcomes_for_report(days)
    if not rows:
        await update.message.reply_text(f"No alert data in the last {days} days.")
        return

    total = len(rows)
    rugged = sum(1 for r in rows if r.get("rugged"))
    rug_rate = (rugged / total * 100) if total > 0 else 0

    def _pct(a, b):
        if not a or not b or b <= 0: return None
        return ((a - b) / b) * 100

    def _fmt(v):
        if v is None: return "-"
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f}%"

    def _stats(window):
        price_col = f"price_{window}"
        checked_col = f"checked_{window}"
        valid = [r for r in rows if r.get(checked_col) and r.get(price_col) and r.get("price_at_alert")]
        if not valid:
            return 0, "-", "-", "-"
        returns = [_pct(r[price_col], r["price_at_alert"]) for r in valid]
        returns = [r for r in returns if r is not None]
        if not returns:
            return 0, "-", "-", "-"
        wins = sum(1 for r in returns if r > 0)
        wr = f"{wins/len(returns)*100:.0f}%"
        avg = _fmt(sum(returns)/len(returns))
        mx = _fmt(max(returns))
        return len(returns), wr, avg, mx

    lines = [
        f"📊 PERFORMANCE REPORT ({days}d)",
        "━━━━━━━━━━━━━━━━━━",
        f"Alerts: {total} | Rugged: {rugged} ({rug_rate:.0f}%)",
        "",
    ]

    # Win rates by timeframe
    for window in ["15m", "1h", "6h", "24h"]:
        n, wr, avg, mx = _stats(window)
        if n > 0:
            lines.append(f"  {window}: {wr} win ({n}) | avg {avg} | best {mx}")

    # Top gainers at 1h
    gainers = []
    for r in rows:
        if r.get("checked_1h") and r.get("price_1h") and r.get("price_at_alert"):
            pct = _pct(r["price_1h"], r["price_at_alert"])
            if pct is not None:
                gainers.append((r, pct))

    if gainers:
        gainers.sort(key=lambda x: x[1], reverse=True)
        lines += ["", "🏆 TOP GAINERS (1h)"]
        for r, pct in gainers[:5]:
            sym = r.get("token_symbol") or "?"
            score = r.get("score_at_alert", 0)
            mc = r.get("market_cap_at_alert", 0) or 0
            mc_str = f"${mc/1000:.0f}K" if mc >= 1000 else f"${mc:.0f}"
            lines.append(f"  🟢 ${sym}: {_fmt(pct)} | score {score:.0f} | MC {mc_str}")

    # Worst losers at 1h
    if gainers:
        losers = sorted(gainers, key=lambda x: x[1])[:3]
        lines += ["", "📉 WORST (1h)"]
        for r, pct in losers:
            sym = r.get("token_symbol") or "?"
            rug = " 🚩" if r.get("rugged") else ""
            lines.append(f"  🔴 ${sym}: {_fmt(pct)}{rug}")

    # Score bracket analysis
    brackets = [(70, 100, "70+"), (55, 70, "55-69"), (40, 55, "40-54")]
    bracket_lines = []
    for lo, hi, label in brackets:
        br = [r for r in rows if lo <= (r.get("score_at_alert") or 0) < hi]
        if not br:
            continue
        valid_1h = [r for r in br if r.get("checked_1h") and r.get("price_1h") and r.get("price_at_alert")]
        if valid_1h:
            returns_1h = [_pct(r["price_1h"], r["price_at_alert"]) for r in valid_1h]
            returns_1h = [r for r in returns_1h if r is not None]
            if returns_1h:
                wins = sum(1 for r in returns_1h if r > 0)
                avg = sum(returns_1h) / len(returns_1h)
                bracket_lines.append(f"  Score {label}: {len(br)} alerts, {wins/len(returns_1h)*100:.0f}% win, avg {_fmt(avg)}")

    if bracket_lines:
        lines += ["", "📊 BY SCORE"] + bracket_lines

    # Provenance is essential for tuning: pool, scan and wallet alerts have
    # very different selection criteria. Rows predating this field are kept in
    # a separate legacy bucket instead of being guessed.
    source_labels = {"scan": "Scan", "pool": "Pool", "wallet": "Wallet", "legacy": "Legacy"}
    source_lines = []
    for source in ("scan", "pool", "wallet", "legacy", "unknown"):
        source_rows = [r for r in rows if (r.get("alert_source") or "legacy") == source]
        if not source_rows:
            continue
        source_rugs = sum(1 for r in source_rows if r.get("rugged"))
        valid = [r for r in source_rows if r.get("checked_1h") and r.get("price_1h") and r.get("price_at_alert")]
        wins = sum(1 for r in valid if (_pct(r["price_1h"], r["price_at_alert"]) or 0) > 0)
        win_text = f" | 1h win {wins/len(valid)*100:.0f}% ({len(valid)})" if valid else ""
        label = source_labels.get(source, source.title())
        source_lines.append(
            f"  {label}: {len(source_rows)} | rugs {source_rugs/len(source_rows)*100:.0f}%{win_text}"
        )
    if source_lines:
        lines += ["", "🔎 BY SOURCE"] + source_lines

    # ML training data + model status
    try:
        import feature_logger
        import ml_model
        ml = feature_logger.get_feature_stats()
        labeled = ml['labeled']
        usable = ml.get('usable_labeled', labeled - ml['rugs'])
        model_info = ml_model.get_model_info()

        if model_info["status"] == "active":
            model_line = f"  Model: ACTIVE ✅ (accuracy {model_info['accuracy']}%)"
        else:
            pct = min(100, model_info.get("progress_pct", 0))
            model_line = f"  Model: training data {pct}% ({usable}/{200} usable samples)"

        lines += [
            "",
            f"🧠 ML DATA: {labeled}/{ml['total']} labeled",
            f"  🌙 moon: {ml['moons']} | 🚀 pump: {ml['pumps']} | ⚪ neutral: {ml['neutrals']} | 📉 dump: {ml['dumps']} | 🚩 rug: {ml['rugs']}",
            model_line,
        ]
        if ml['rugs']:
            lines.append(f"  ⚠️ {ml['rugs']} rug labels quarantined from training")
    except Exception:
        pass

    lines += ["", "━━━━━━━━━━━━━━━━━━", f"Use /report <days> for custom range"]

    await update.message.reply_text("\n".join(lines))


async def _handle_sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sell [id]."""
    if not update.message:
        return
    if not _is_authorized(update):
        logger.warning("[BOT] Unauthorized /sell attempt blocked")
        await update.message.reply_text("Not authorized")
        return
    args = context.args
    positions = [p for p in storage.get_open_positions() if _position_is_sellable(p)]

    if not args:
        await update.message.reply_text("Usage: /sell <id> or /sell all")
        return

    if args[0].lower() == "all":
        if not positions:
            await update.message.reply_text("No sellable positions")
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
        await update.message.reply_text("Usage: /sell <id> or /sell all")
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
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
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


# -- Wallet Tracker Commands --

async def _handle_addwallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addwallet <address> [label] [win_rate]."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /addwallet <address> [label] [win_rate%]\nExample: /addwallet 7xKp...abc WhaleKing 72")
        return

    import wallet_tracker

    address = args[0]
    if len(address) < 32 or len(address) > 44:
        await update.message.reply_text("Invalid Solana address")
        return

    label = args[1] if len(args) > 1 else ""
    win_rate = 0.0
    if len(args) > 2:
        try:
            win_rate = float(args[2])
        except ValueError:
            pass

    wallet_tracker.add_wallet(address, label, win_rate)
    count = wallet_tracker.get_wallet_count()
    await update.message.reply_text(
        f"Added wallet to tracker:\n"
        f"Address: {address[:12]}...{address[-6:]}\n"
        f"Label: {label or 'None'}\n"
        f"Win Rate: {win_rate:.0f}%\n"
        f"\nTotal tracked: {count}"
    )


async def _handle_wallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /wallets — show all tracked wallets."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return

    import wallet_tracker

    wallets = wallet_tracker.get_tracked_wallets()
    if not wallets:
        await update.message.reply_text(
            "No wallets tracked.\n\nUse /addwallet <address> [label] [win_rate%] to add.\n"
            "Find alpha wallets at gmgn.ai/sol/leaderboard"
        )
        return

    lines = ["🐋 TRACKED WALLETS\n━━━━━━━━━━━━━━━━━━\n"]
    for w in wallets:
        addr = w["address"]
        label = w.get("label") or "—"
        wr = w.get("win_rate", 0) or 0
        trades = w.get("total_trades", 0)
        lines.append(f"  {label} ({addr[:8]}...{addr[-4:]})")
        lines.append(f"    WR: {wr:.0f}% | Trades: {trades}")

    lines.append(f"\n━━━━━━━━━━━━━━━━━━\nTotal: {len(wallets)} wallets")
    lines.append("Use /rmwallet <address> to remove")

    await update.message.reply_text("\n".join(lines))


async def _handle_rmwallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /rmwallet <address>."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /rmwallet <address>")
        return

    import wallet_tracker

    address = args[0]
    wallet_tracker.remove_wallet(address)
    await update.message.reply_text(f"Removed: {address[:12]}...")


async def _handle_clearwallets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clearwallets — remove all tracked wallets."""
    if not update.message:
        return
    if not _is_authorized(update):
        await update.message.reply_text("Not authorized")
        return

    import wallet_tracker

    wallets = wallet_tracker.get_tracked_wallets()
    if not wallets:
        await update.message.reply_text("No wallets to clear.")
        return

    count = len(wallets)
    for w in wallets:
        wallet_tracker.remove_wallet(w["address"])

    await update.message.reply_text(f"Cleared {count} tracked wallet(s).\nDiscovery will find new ones in the next cycle.")


async def _handle_discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /discover — manually trigger wallet discovery now."""
    if not update.message:
        return

    import wallet_tracker

    await update.message.reply_text("🔍 Scanning trending tokens for alpha wallets...\nThis may take 1-2 minutes.")

    try:
        newly_added = await asyncio.to_thread(wallet_tracker.discover_from_trending)
    except Exception:
        logger.exception("[BOT] Discover command crashed")
        await update.message.reply_text("Discovery failed — check logs.")
        return

    if not newly_added:
        await update.message.reply_text("No new alpha wallets found this scan.\nTry again later when market is more active.")
        return

    total = wallet_tracker.get_wallet_count()
    lines = [f"🔍 Found {len(newly_added)} new alpha wallet(s)!\n"]

    for w in newly_added:
        addr = w["address"]
        short_addr = f"{addr[:8]}...{addr[-6:]}"
        tokens_str = ", ".join(f"${t}" for t in w["appeared_in"][:5])
        lines.append(
            f"👛 {short_addr}\n"
            f"  WR: {w['win_rate']:.0f}% | {w['winning_trades']}/{w['total_trades']} wins\n"
            f"  Avg: {w['avg_return']:+.1f}% | In: {tokens_str}"
        )

    lines.append(f"\n━━━━━━━━━━━━━━━━━━\nTotal tracked: {total}")
    await update.message.reply_text("\n".join(lines))


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
        BotCommand("report", "Performance report: /report [days]"),
        BotCommand("pnl", "PnL dashboard: today, 7d, open positions"),
        BotCommand("buy", "Buy token: /buy <address> $5"),
        BotCommand("limitbuy", "Create persistent limit buy"),
        BotCommand("sell", "Sell: /sell <id> or /sell all"),
        BotCommand("orders", "View persistent orders"),
        BotCommand("cancelorder", "Cancel order: /cancelorder <id>"),
        BotCommand("profile", "Configure wallet execution profile"),
        BotCommand("watch", "Watch token: /watch <address>"),
        BotCommand("wallets", "View tracked wallets"),
        BotCommand("addwallet", "Track wallet: /addwallet <addr> [label] [wr%]"),
        BotCommand("autobuy", "Toggle auto-buy"),
        BotCommand("stop", "Emergency stop all trading"),
    ])

    # Register handlers
    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(CommandHandler("positions", _handle_positions_command))
    app.add_handler(CommandHandler("status", lambda u, c: _handle_start(u, c)))
    app.add_handler(CommandHandler("trades", _handle_trades_command))
    app.add_handler(CommandHandler("trade", _handle_trade_command))
    app.add_handler(CommandHandler("report", _handle_report_command))
    app.add_handler(CommandHandler("pnl", _handle_pnl_command))
    app.add_handler(CommandHandler("buy", _handle_buy_command))
    app.add_handler(CommandHandler("limitbuy", _handle_limitbuy_command))
    app.add_handler(CommandHandler("sell", _handle_sell_command))
    app.add_handler(CommandHandler("orders", _handle_orders_command))
    app.add_handler(CommandHandler("cancelorder", _handle_cancelorder_command))
    app.add_handler(CommandHandler("profile", _handle_profile_command))
    app.add_handler(CommandHandler("watch", _handle_watch_command))
    app.add_handler(CommandHandler("addwallet", _handle_addwallet_command))
    app.add_handler(CommandHandler("wallets", _handle_wallets_command))
    app.add_handler(CommandHandler("rmwallet", _handle_rmwallet_command))
    app.add_handler(CommandHandler("clearwallets", _handle_clearwallets_command))
    app.add_handler(CommandHandler("autobuy", _handle_autobuy_command))
    app.add_handler(CommandHandler("stop", _handle_stop_command))
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
