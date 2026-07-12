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
    """Handle buy button press."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()  # Acknowledge the button press

    # Parse callback data: "buy:{token_address}"
    parts = query.data.split(":", 1)
    if len(parts) != 2 or parts[0] != "buy":
        await query.edit_message_reply_markup(reply_markup=None)
        return

    token_mint = parts[1]
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
    await query.edit_message_text(
        text=query.message.text + "\n\n_Executing buy..._",
        parse_mode="Markdown",
    )

    result = await asyncio.to_thread(executor.buy_token, token_mint)

    if result:
        sig = result.get("signature", "")[:16]
        amount = result.get("amount_sol", 0)
        impact = result.get("price_impact_pct", 0)
        await query.edit_message_text(
            text=query.message.text.replace("_Executing buy..._", "") +
                 f"\n\n_Bought {amount} SOL (impact: {impact:.1f}%, tx: {sig}...)_",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            text=query.message.text.replace("_Executing buy..._", "") +
                 "\n\n_Buy failed -- check logs_",
            parse_mode="Markdown",
        )


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

    lines = [
        f"Trading: {'ENABLED' if config.TRADING_ENABLED else 'DISABLED'}",
        f"Wallet: `{wallet[:8]}...{wallet[-4:]}`" if wallet else "Wallet: not configured",
        f"Open positions: {positions}/{config.MAX_OPEN_POSITIONS}",
        f"Trade size: {config.TRADE_AMOUNT_SOL} SOL",
        f"Daily limit: {config.DAILY_LOSS_LIMIT_SOL} SOL",
        f"Status: {reason}",
    ]

    if update.message:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _handle_positions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /positions command -- list open positions."""
    positions = storage.get_open_positions()
    if not positions:
        if update.message:
            await update.message.reply_text("No open positions.")
        return

    lines = ["*Open Positions:*", ""]
    for p in positions:
        token = p.get("token_address", "?")[:12]
        amount = p.get("buy_amount_sol", 0)
        lines.append(f"- `{token}...` | {amount} SOL")

    if update.message:
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


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
    app.add_handler(CommandHandler("stop", _handle_stop_command))
    app.add_handler(CommandHandler("status", _handle_status_command))
    app.add_handler(CommandHandler("positions", _handle_positions_command))

    # Start polling for updates (non-blocking)
    logger.info("[BOT] Starting Telegram callback handler (commands: /stop, /status, /positions)")
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
