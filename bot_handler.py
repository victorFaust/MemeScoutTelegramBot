"""Telegram bot callback handler for inline buy buttons.

Listens for callback queries from buy buttons and executes trades via executor.
Runs as an independent async task alongside the main scanner.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import executor
import storage

logger = logging.getLogger(__name__)


async def _handle_buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle buy button press. Supports both 'buy:{token}' and 'buyusd:{amount}:{token}'."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()  # Acknowledge the button press

    # Parse callback data
    parts = query.data.split(":")
    if len(parts) == 3 and parts[0] == "buyusd":
        # Format: buyusd:{usd_amount}:{token_address}
        try:
            usd_amount = float(parts[1])
        except ValueError:
            return
        token_mint = parts[2]
        amount_sol = executor.usd_to_sol(usd_amount)
        display_amount = f"${usd_amount:.0f} ({amount_sol:.3f} SOL)"
    elif len(parts) == 2 and parts[0] == "buy":
        # Legacy format: buy:{token_address}
        token_mint = parts[1]
        amount_sol = config.TRADE_AMOUNT_SOL
        display_amount = f"{amount_sol} SOL"
    else:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    user_id = str(query.from_user.id) if query.from_user else ""

    # Only allow the configured chat owner to trade
    if user_id != config.TELEGRAM_CHAT_ID:
        await query.answer("Not authorized", show_alert=True)
        return

    # Check if trading is allowed
    allowed, reason = executor.can_trade()
    if not allowed:
        await query.edit_message_text(
            text=query.message.text + f"\n\n_Buy blocked: {reason}_",
            parse_mode="Markdown",
        )
        return

    # Execute the buy
    sol_price = executor.get_sol_price()
    await query.edit_message_text(
        text=query.message.text + f"\n\n_Buying {display_amount} (SOL=${sol_price:.0f})..._",
        parse_mode="Markdown",
    )

    result = await asyncio.to_thread(executor.buy_token, token_mint, amount_sol)

    if result:
        sig = result.get("signature", "")[:16]
        spent = result.get("amount_sol", 0)
        impact = result.get("price_impact_pct", 0)
        usd_spent = spent * sol_price
        await query.edit_message_text(
            text=query.message.text.replace(f"_Buying {display_amount} (SOL=${sol_price:.0f})..._", "") +
                 f"\n\n_Bought ${usd_spent:.0f} ({spent:.3f} SOL) | impact: {impact:.1f}% | tx: {sig}..._",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            text=query.message.text.replace(f"_Buying {display_amount} (SOL=${sol_price:.0f})..._", "") +
                 "\n\n_Buy failed -- check logs_",
            parse_mode="Markdown",
        )


async def _handle_buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /buy <token_address> <$amount> command for custom buys."""
    if not update.message:
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /buy <token_address> <$amount>\nExample: /buy 7NTs9F...pump $25")
        return

    token_mint = args[0]
    amount_str = args[1].replace("$", "").replace(",", "")

    try:
        usd_amount = float(amount_str)
    except ValueError:
        await update.message.reply_text("Invalid amount. Use: /buy <token> $25")
        return

    if usd_amount <= 0 or usd_amount > 100:
        await update.message.reply_text("Amount must be between $0.50 and $100")
        return

    amount_sol = executor.usd_to_sol(usd_amount)
    sol_price = executor.get_sol_price()

    allowed, reason = executor.can_trade()
    if not allowed:
        await update.message.reply_text(f"Buy blocked: {reason}")
        return

    await update.message.reply_text(f"Buying ${usd_amount:.0f} ({amount_sol:.3f} SOL @ ${sol_price:.0f}/SOL)...")

    result = await asyncio.to_thread(executor.buy_token, token_mint, amount_sol)
    if result:
        sig = result.get("signature", "")[:16]
        impact = result.get("price_impact_pct", 0)
        await update.message.reply_text(
            f"Bought ${usd_amount:.0f} ({amount_sol:.3f} SOL) | impact: {impact:.1f}% | tx: {sig}..."
        )
    else:
        await update.message.reply_text("Buy failed -- check logs or token address")


async def _handle_autobuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /autobuy command -- toggle auto-buy or set amount."""
    if not update.message:
        return

    args = context.args
    if not args:
        # Toggle
        config.AUTO_BUY_ENABLED = not config.AUTO_BUY_ENABLED
        state = "ENABLED" if config.AUTO_BUY_ENABLED else "DISABLED"
        await update.message.reply_text(
            f"Auto-buy {state}\n"
            f"Amount: ${config.AUTO_BUY_AMOUNT_USD:.0f}\n"
            f"New pools: {'yes' if config.AUTO_BUY_NEW_POOLS else 'no'}"
        )
    elif args[0].startswith("$"):
        # Set amount
        try:
            amount = float(args[0].replace("$", ""))
            if 0.5 <= amount <= 100:
                config.AUTO_BUY_AMOUNT_USD = amount
                await update.message.reply_text(f"Auto-buy amount set to ${amount:.0f}")
            else:
                await update.message.reply_text("Amount must be $0.50 - $100")
        except ValueError:
            await update.message.reply_text("Usage: /autobuy or /autobuy $5")
    elif args[0].lower() == "pools":
        config.AUTO_BUY_NEW_POOLS = not config.AUTO_BUY_NEW_POOLS
        state = "ENABLED" if config.AUTO_BUY_NEW_POOLS else "DISABLED"
        await update.message.reply_text(f"Auto-buy new pools: {state}")
    else:
        await update.message.reply_text("Usage: /autobuy (toggle) | /autobuy $5 (set amount) | /autobuy pools (toggle new pools)")
    logger.info("[BOT] Auto-buy: enabled=%s, amount=$%.0f, pools=%s",
                config.AUTO_BUY_ENABLED, config.AUTO_BUY_AMOUNT_USD, config.AUTO_BUY_NEW_POOLS)


async def _handle_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop command -- disable trading."""
    # This sets a runtime flag; doesn't persist across restarts
    config.TRADING_ENABLED = False
    if update.message:
        await update.message.reply_text("Trading DISABLED. Set TRADING_ENABLED=true in env to re-enable.")
    logger.warning("[TRADE] Trading disabled via /stop command")


async def _handle_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command -- show trading status."""
    allowed, reason = executor.can_trade()
    wallet = executor.get_wallet_address()
    positions = storage.get_open_positions_count()
    balance = executor.get_wallet_balance()

    wallet_str = f"{wallet[:8]}...{wallet[-4:]}" if wallet else "not configured"
    if balance:
        balance_str = f"{balance['sol']} SOL (${balance['usd']:.2f})"
    else:
        balance_str = "unavailable"

    lines = [
        f"Trading: {'ENABLED' if config.TRADING_ENABLED else 'DISABLED'}",
        f"Auto-buy: {'ON' if config.AUTO_BUY_ENABLED else 'OFF'} (${config.AUTO_BUY_AMOUNT_USD:.0f})",
        f"Auto-buy pools: {'ON' if config.AUTO_BUY_NEW_POOLS else 'OFF'}",
        f"Wallet: {wallet_str}",
        f"Balance: {balance_str}",
        f"Open positions: {positions}/{config.MAX_OPEN_POSITIONS}",
        f"TP: +{config.TAKE_PROFIT_PCT:.0f}% / SL: {config.STOP_LOSS_PCT:.0f}%",
        f"Daily limit: {config.DAILY_LOSS_LIMIT_SOL} SOL",
        f"Status: {reason}",
    ]

    if update.message:
        await update.message.reply_text("\n".join(lines))


async def _handle_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions command -- list open positions with PnL."""
    positions = storage.get_open_positions()
    if not positions:
        if update.message:
            await update.message.reply_text("No open positions.")
        return

    lines = ["*Open Positions:*", ""]
    for i, p in enumerate(positions):
        token = p.get("token_address", "?")[:12]
        amount = p.get("buy_amount_sol", 0)
        pos_id = p.get("id", 0)
        lines.append(f"{i+1}. `{token}...` | {amount} SOL | ID: {pos_id}")

    lines.append("")
    lines.append("Use /sell <ID> to close a position")

    if update.message:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _handle_sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /sell <position_id> command -- manually close a position."""
    if not update.message:
        return

    args = context.args
    if not args:
        # Sell all positions
        positions = storage.get_open_positions()
        if not positions:
            await update.message.reply_text("No open positions to sell.")
            return
        await update.message.reply_text(f"Selling {len(positions)} position(s)...")
        for pos in positions:
            result = await asyncio.to_thread(
                executor.sell_token, pos["id"], pos["token_address"], pos["token_amount"]
            )
            if result:
                await update.message.reply_text(
                    f"Sold #{pos['id']} for {result['sol_received']:.4f} SOL"
                )
            else:
                await update.message.reply_text(f"Failed to sell #{pos['id']}")
        return

    # Sell specific position by ID
    try:
        pos_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Usage: /sell <position_id> or /sell (sells all)")
        return

    positions = storage.get_open_positions()
    pos = next((p for p in positions if p.get("id") == pos_id), None)
    if not pos:
        await update.message.reply_text(f"Position #{pos_id} not found or already closed.")
        return

    await update.message.reply_text(f"Selling position #{pos_id}...")
    result = await asyncio.to_thread(
        executor.sell_token, pos["id"], pos["token_address"], pos["token_amount"]
    )
    if result:
        await update.message.reply_text(
            f"Sold #{pos_id} for {result['sol_received']:.4f} SOL (tx: {result['signature'][:16]}...)"
        )
    else:
        await update.message.reply_text(f"Failed to sell #{pos_id} -- check logs")


async def start_bot_handler() -> None:
    """Start the Telegram bot application for handling callbacks and commands.
    
    This runs alongside the main scanner and listens for button presses.
    """
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("[BOT] No TELEGRAM_BOT_TOKEN -- cannot start callback handler")
        return

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CallbackQueryHandler(_handle_buy_callback))
    app.add_handler(CommandHandler("buy", _handle_buy_command))
    app.add_handler(CommandHandler("autobuy", _handle_autobuy_command))
    app.add_handler(CommandHandler("stop", _handle_stop_command))
    app.add_handler(CommandHandler("status", _handle_status_command))
    app.add_handler(CommandHandler("positions", _handle_positions_command))
    app.add_handler(CommandHandler("sell", _handle_sell_command))

    # Start polling for updates (non-blocking)
    logger.info("[BOT] Telegram handler started (commands: /buy, /autobuy, /sell, /positions, /status, /stop)")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep running until stopped
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
