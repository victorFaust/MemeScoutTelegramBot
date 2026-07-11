import os
from dotenv import load_dotenv

load_dotenv()


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


# Telegram
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# Chains
CHAINS: list[str] = [
    c.strip().lower() for c in os.getenv("CHAINS", "solana,base").split(",") if c.strip()
]

# Polling
POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 180)

# Filter thresholds
MIN_LIQUIDITY_USD: float = _float("MIN_LIQUIDITY_USD", 5000)
MAX_LIQUIDITY_USD: float = _float("MAX_LIQUIDITY_USD", 50000)
MAX_MARKET_CAP: float = _float("MAX_MARKET_CAP", 500000)
MAX_PAIR_AGE_HOURS: float = _float("MAX_PAIR_AGE_HOURS", 168)
MIN_VOLUME_LIQUIDITY_RATIO: float = _float("MIN_VOLUME_LIQUIDITY_RATIO", 0.5)
MIN_TX_COUNT_1H: int = _int("MIN_TX_COUNT_1H", 10)
MIN_PRICE_CHANGE_1H: float = _float("MIN_PRICE_CHANGE_1H", 0.0)
MIN_PRICE_CHANGE_6H: float = _float("MIN_PRICE_CHANGE_6H", 0.0)

# Dedup
DEDUP_COOLDOWN_HOURS: float = _float("DEDUP_COOLDOWN_HOURS", 6)

# Scoring
MIN_ALERT_SCORE: float = _float("MIN_ALERT_SCORE", 50)

# Logging
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")
LOG_MAX_BYTES: int = _int("LOG_MAX_BYTES", 5_242_880)
LOG_BACKUP_COUNT: int = _int("LOG_BACKUP_COUNT", 3)
