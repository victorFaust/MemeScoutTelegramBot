"""Send formatted alert messages to Telegram."""

import logging
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


def build_message(result: dict) -> str:
    """Build a Markdown-formatted Telegram message from a scored result."""
    pair = result["pair"]
    score = result["score"]

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

    lines = [
        f"*NEW MEMECOIN ALERT*  --  Score: *{score}/100*",
        f"Chain: `{chain}`",
        "",
        f"*{name}* (${symbol})",
        "",
        f"Liquidity: {_fmt_num(liq)}",
        f"Market Cap: {_fmt_num(mc)}",
        f"Volume 24h: {_fmt_num(vol_24h)}",
        "",
        f"1h: {_fmt_pct(pc.get('h1'))}  |  6h: {_fmt_pct(pc.get('h6'))}  |  24h: {_fmt_pct(pc.get('h24'))}",
        f"Buys/Sells (1h): {buy_ratio}",
        "",
        f"Score: *{score}/100*",
    ]

    if dex_url:
        lines.append(f"[View on DexScreener]({dex_url})")

    return "\n".join(lines)


async def send_alert(result: dict) -> bool:
    """Send a single alert message. Returns True on success."""
    if not config.TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_CHAT_ID is not set -- skipping alert")
        return False
    text = build_message(result)
    try:
        bot = _get_bot()
        await bot.send_message(
            chat_id=config.TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=telegram.constants.ParseMode.MARKDOWN,
        )
        logger.info("Sent alert for %s", _safe(result, "pair", "baseToken", "symbol", default="?"))
        return True
    except Exception:
        logger.exception("Failed to send Telegram alert")
        return False
