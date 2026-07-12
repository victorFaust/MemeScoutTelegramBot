"""Send formatted alert messages to Telegram."""

import logging
import time
from typing import Any

import telegram
import telegram.constants

import config

logger = logging.getLogger(__name__)

_bot: telegram.Bot | None = None


def _get_bot() -> telegram.Bot:
    global _bot
    if _bot is None:
        if not config.TELEGRAM_BOT_TOKEN:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        _bot = telegram.Bot(token=config.TELEGRAM_BOT_TOKEN)
    return _bot


def _safe(d: dict | None, *keys: str, default: Any = None) -> Any:
    obj: Any = d
    for k in keys:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k, default)
    return obj


def _fmt_num(n: float | int | None) -> str:
    if n is None:
        return "N/A"
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:,.2f}M"
    if abs(n) >= 1_000:
        return f"${n / 1_000:,.1f}K"
    return f"${n:,.2f}"


def _fmt_pct(n: float | None) -> str:
    if n is None:
        return "N/A"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.1f}%"


def build_message(result: dict, safety: dict | None = None) -> str:
    """Build a Markdown-formatted Telegram message from a scored result."""
    pair = result["pair"]
    score = result["score"]
    is_momentum = result.get("momentum_realert", False)

    base = pair.get("baseToken") or {}
    name = base.get("name", "Unknown")
    symbol = base.get("symbol", "???")
    chain = (pair.get("chainId") or "unknown").upper()

    pair_addr = pair.get("pairAddress", "")
    chain_lower = (pair.get("chainId") or "").lower()
    dex_url = f"https://dexscreener.com/{chain_lower}/{pair_addr}" if pair_addr else ""

    liq = _safe(pair, "liquidity", "usd")
    mc = pair.get("marketCap") or pair.get("fdv")
    vol_24h = _safe(pair, "volume", "h24")
    pc = pair.get("priceChange") or {}

    txns_h1 = _safe(pair, "txns", "h1") or {}
    buys = txns_h1.get("buys", 0)
    sells = txns_h1.get("sells", 0)
    total_tx = buys + sells
    buy_ratio = f"{buys}/{sells}" if total_tx > 0 else "N/A"

    # All alerts are momentum-confirmed now
    prev = result.get("prev_score", 0)
    if prev and prev > 0:
        header = f"*MOMENTUM CONFIRMED*  --  Score: *{prev:.0f} -> {score}/100*"
    else:
        header = f"*MOMENTUM CONFIRMED*  --  Score: *{score}/100*"

    lines = [
        header,
        f"Chain: `{chain}`",
        "",
        f"*{name}* (${symbol})",
        "",
    ]

    # For Robinhood Chain, show market cap first and prominently
    if chain_lower == "robinhood":
        lines.append(f"*Market Cap: {_fmt_num(mc)}*")
        lines.append(f"Liquidity: {_fmt_num(liq)}")
    else:
        lines.append(f"Liquidity: {_fmt_num(liq)}")
        lines.append(f"Market Cap: {_fmt_num(mc)}")

    lines.append(f"Volume 24h: {_fmt_num(vol_24h)}")

    # Token age
    pair_created = pair.get("pairCreatedAt")
    if pair_created:
        import time as _time
        age_hours = (_time.time() * 1000 - pair_created) / 3_600_000
        if age_hours < 1:
            age_str = f"{age_hours * 60:.0f}m"
        elif age_hours < 24:
            age_str = f"{age_hours:.0f}h"
        else:
            age_str = f"{age_hours / 24:.1f}d"
        lines.append(f"Age: {age_str}")

    lines.append("")
    lines.append(f"1h: {_fmt_pct(pc.get('h1'))}  |  6h: {_fmt_pct(pc.get('h6'))}  |  24h: {_fmt_pct(pc.get('h24'))}")
    lines.append(f"Buys/Sells (1h): {buy_ratio}")

    # Safety check section
    if safety:
        lines.append("")
        if safety.get("check_failed"):
            lines.append("-- SAFETY: check unavailable --")
        else:
            risk = safety.get("risk_label", "N/A")
            risk_icon = {"LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH"}.get(risk, "???")
            lines.append(f"-- SAFETY: {risk_icon} risk --")

            buy_tax = safety.get("buy_tax_pct")
            sell_tax = safety.get("sell_tax_pct")
            if buy_tax is not None or sell_tax is not None:
                bt = f"{buy_tax:.1f}%" if buy_tax is not None else "N/A"
                st = f"{sell_tax:.1f}%" if sell_tax is not None else "N/A"
                lines.append(f"Tax: buy {bt} / sell {st}")

            mint = safety.get("mint_authority_active")
            if mint is not None:
                lines.append(f"Mint authority: {'ACTIVE' if mint else 'Renounced'}")

            top10 = safety.get("top10_holder_pct")
            if top10 is not None:
                lines.append(f"Top 10 holders: {top10:.1f}%")

        # RugCheck data (Solana)
        rc_score = safety.get("rugcheck_score")
        if rc_score is not None:
            lp_locked = safety.get("lp_locked_pct", 0)
            rc_risks = safety.get("risk_count", 0)
            lines.append(f"RugCheck: {rc_score:.0%} safe | LP locked: {lp_locked:.0f}%")
            if rc_risks > 0:
                risk_names = safety.get("risks", [])
                lines.append(f"Risks: {', '.join(risk_names[:3])}")
        elif safety.get("rugcheck_unavailable"):
            lines.append("RugCheck: unavailable")

        # Holder analysis
        unique_buyers = safety.get("unique_buyers")
        if unique_buyers is not None:
            quality = safety.get("buy_quality", 0)
            top1 = safety.get("top1_pct", 0)
            holders_min = safety.get("holder_count_min", 0)
            lines.append(f"Holders: {holders_min}+ | Top wallet: {top1:.0f}%")
            lines.append(f"Unique buyers: {unique_buyers} (quality: {quality:.0%})")
            creates = safety.get("creator_token_creates")
            if creates is not None:
                lines.append(f"Creator: {creates} tokens deployed")

    lines.append("")
    lines.append(f"Score: *{score}/100*")

    if dex_url:
        lines.append(f"[View on DexScreener]({dex_url})")

    return "\n".join(lines)


async def send_alert(result: dict, safety: dict | None = None) -> bool:
    """Send a single alert message with optional buy buttons. Returns True on success."""
    if not config.TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID is not set -- skipping alert")
        return False
    text = build_message(result, safety)

    # Add buy buttons if trading is enabled (multiple USD amounts)
    reply_markup = None
    if config.TRADING_ENABLED:
        pair = result.get("pair", {})
        base = pair.get("baseToken", {})
        token_addr = base.get("address", "")
        if token_addr:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            reply_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("$1", callback_data=f"buyusd:1:{token_addr}"),
                InlineKeyboardButton("$3", callback_data=f"buyusd:3:{token_addr}"),
                InlineKeyboardButton("$5", callback_data=f"buyusd:5:{token_addr}"),
                InlineKeyboardButton("$8", callback_data=f"buyusd:8:{token_addr}"),
            ]])

    try:
        bot = _get_bot()
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=reply_markup,
        )
        logger.info("Sent alert for %s", _safe(result, "pair", "baseToken", "symbol", default="?"))
        return True
    except Exception:
        logger.exception("Failed to send Telegram alert")
        return False


async def send_new_pool_alert(token_info: dict, rc_data: dict | None = None) -> bool:
    """Send alert for a brand-new pool detected via websocket."""
    if not config.TELEGRAM_CHAT_ID:
        return False

    token_addr = token_info.get("token_address", "???")
    sol = token_info.get("sol_deposited", 0)
    symbol = token_info.get("symbol", "???")
    total_txns = token_info.get("total_txns_90s", 0)
    buys = token_info.get("buys_90s", 0)

    dex_url = f"https://dexscreener.com/solana/{token_addr}"

    # Get extra data from DexScreener pair if available
    pair_data = token_info.get("pair_data", {})
    mc = pair_data.get("marketCap") or pair_data.get("fdv") or 0
    liq_usd = (pair_data.get("liquidity") or {}).get("usd", sol * 170)

    lines = [
        f"*NEW POOL*  --  ${symbol}",
        "Chain: `SOLANA`",
        "",
        f"Token: `{token_addr}`",
        f"Liquidity: {_fmt_num(liq_usd)}",
    ]
    if mc:
        lines.append(f"Market Cap: {_fmt_num(mc)}")

    # Token age
    pair_created = pair_data.get("pairCreatedAt")
    if pair_created:
        import time as _time
        age_hours = (_time.time() * 1000 - pair_created) / 3_600_000
        if age_hours < 1:
            age_str = f"{age_hours * 60:.0f}m"
        elif age_hours < 24:
            age_str = f"{age_hours:.0f}h"
        else:
            age_str = f"{age_hours / 24:.1f}d"
        lines.append(f"Age: {age_str}")

    lines.append(f"Activity (90s): {total_txns} txns ({buys} buys)")

    if rc_data:
        rc_score = rc_data.get("rugcheck_score")
        lp_locked = rc_data.get("lp_locked_pct", 0)
        if rc_score is not None:
            lines.append(f"RugCheck: {rc_score:.0%} safe | LP: {lp_locked:.0f}%")

    lines.append("")
    lines.append(f"[View on DexScreener]({dex_url})")

    text = "\n".join(lines)

    # Add buy buttons if trading is enabled
    reply_markup = None
    if config.TRADING_ENABLED and token_addr:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("$1", callback_data=f"buyusd:1:{token_addr}"),
            InlineKeyboardButton("$3", callback_data=f"buyusd:3:{token_addr}"),
            InlineKeyboardButton("$5", callback_data=f"buyusd:5:{token_addr}"),
            InlineKeyboardButton("$8", callback_data=f"buyusd:8:{token_addr}"),
        ]])

    try:
        bot = _get_bot()
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
            reply_markup=reply_markup,
        )
        return True
    except Exception:
        logger.exception("Failed to send new pool alert")
        return False


async def send_trade_notification(message: str, token_address: str = "") -> bool:
    """Send a trade execution notification (buy/sell confirmations)."""
    if not config.TELEGRAM_CHAT_ID:
        return False

    dex_url = f"https://dexscreener.com/solana/{token_address}" if token_address else ""
    text = f"*TRADE*: {message}"
    if dex_url:
        text += f"\n[Chart]({dex_url})"

    try:
        bot = _get_bot()
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
        )
        return True
    except Exception:
        logger.exception("Failed to send trade notification")
        return False
