"""Configuration loader.

Secrets and global settings come from .env.
Per-chain filter/scoring/safety thresholds come from chain_config.yaml.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


# -- Secrets & global settings (from .env) ----------------------------

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

CHAINS: list[str] = [
    c.strip().lower() for c in os.getenv("CHAINS", "solana,base").split(",") if c.strip()
]

POLL_INTERVAL_SECONDS: int = _int("POLL_INTERVAL_SECONDS", 180)
DEDUP_COOLDOWN_HOURS: float = _float("DEDUP_COOLDOWN_HOURS", 6)
POOL_ALERT_MAX_PER_HOUR: int = _int("POOL_ALERT_MAX_PER_HOUR", 12)
SAFETY_CHECK_CACHE_HOURS: float = _float("SAFETY_CHECK_CACHE_HOURS", 1)
SKIP_ON_SAFETY_CHECK_FAILURE: bool = os.getenv("SKIP_ON_SAFETY_CHECK_FAILURE", "true").lower() in ("1", "true", "yes")

# QuickNode Solana
QUICKNODE_WSS_URL: str = os.getenv("QUICKNODE_WSS_URL", "")
QUICKNODE_HTTP_URL: str = os.getenv("QUICKNODE_HTTP_URL", "")

# Shyft Solana
SHYFT_HTTP_URL: str = os.getenv("SHYFT_HTTP_URL", "")

# Alchemy Solana (primary wallet tracker RPC)
ALCHEMY_API_KEY: str = os.getenv("ALCHEMY_API_KEY", "")
ALCHEMY_RPC_URL: str = (
    f"https://solana-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    if ALCHEMY_API_KEY else ""
)
ALCHEMY_WSS_URL: str = (
    f"wss://solana-mainnet.g.alchemy.com/v2/{ALCHEMY_API_KEY}"
    if ALCHEMY_API_KEY else ""
)

# Birdeye API (optional — free public tier works without a key)
BIRDEYE_API_KEY: str = os.getenv("BIRDEYE_API_KEY", "")

# Solscan API (optional — free public tier works without a key)
SOLSCAN_API_KEY: str = os.getenv("SOLSCAN_API_KEY", "")

# Trading (Jupiter swap)
TRADING_WALLET_PRIVATE_KEY: str = os.getenv("TRADING_WALLET_PRIVATE_KEY", "")
# Additional wallets for multi-wallet execution (optional, comma-separated keys)
TRADING_WALLET_KEYS: list[str] = [
    k.strip() for k in os.getenv("TRADING_WALLET_KEYS", "").split(",") if k.strip()
]
TRADE_AMOUNT_SOL: float = _float("TRADE_AMOUNT_SOL", 0.1)
MAX_OPEN_POSITIONS: int = _int("MAX_OPEN_POSITIONS", 5)
DAILY_LOSS_LIMIT_SOL: float = _float("DAILY_LOSS_LIMIT_SOL", 1.0)
TRADE_SLIPPAGE_BPS: int = _int("TRADE_SLIPPAGE_BPS", 500)  # 5% default slippage
TRADING_ENABLED: bool = os.getenv("TRADING_ENABLED", "false").lower() in ("1", "true", "yes")
TAKE_PROFIT_PCT: float = _float("TAKE_PROFIT_PCT", 100.0)
PARTIAL_SELL_PCT: float = _float("PARTIAL_SELL_PCT", 50.0)
STOP_LOSS_PCT: float = _float("STOP_LOSS_PCT", -20.0)
EXIT_CHECK_INTERVAL: int = _int("EXIT_CHECK_INTERVAL", 15)

# DCA exit stages (sell in thirds: 33% at stage1, 33% at stage2, remainder with trail)
DCA_STAGE1_PCT: float = _float("DCA_STAGE1_PCT", 50.0)   # +50% → sell 33%
DCA_STAGE2_PCT: float = _float("DCA_STAGE2_PCT", 100.0)  # +100% → sell 33%
DCA_SELL_EACH: float = _float("DCA_SELL_EACH", 33.0)     # % of position to sell each stage

# Auto-buy (fully autonomous mode)
AUTO_BUY_ENABLED: bool = os.getenv("AUTO_BUY_ENABLED", "false").lower() in ("1", "true", "yes")
AUTO_BUY_AMOUNT_USD: float = _float("AUTO_BUY_AMOUNT_USD", 3.0)
AUTO_BUY_NEW_POOLS: bool = os.getenv("AUTO_BUY_NEW_POOLS", "false").lower() in ("1", "true", "yes")
AUTO_BUY_MIN_CALIBRATION_SAMPLES: int = _int("AUTO_BUY_MIN_CALIBRATION_SAMPLES", 20)
AUTO_BUY_MIN_CALIBRATED_PROB: float = _float("AUTO_BUY_MIN_CALIBRATED_PROB", 0.45)
AUTO_BUY_MIN_EXPECTANCY_PCT: float = _float("AUTO_BUY_MIN_EXPECTANCY_PCT", 5.0)
ML_ESTIMATED_TRADING_COST_PCT: float = _float("ML_ESTIMATED_TRADING_COST_PCT", 3.0)

LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")
LOG_MAX_BYTES: int = _int("LOG_MAX_BYTES", 5_242_880)
LOG_BACKUP_COUNT: int = _int("LOG_BACKUP_COUNT", 3)


# -- Per-chain config (from chain_config.yaml) ------------------------

_CONFIG_PATH = Path(__file__).parent / "chain_config.yaml"

_DEFAULT_PROFILE: dict[str, Any] = {
    "min_liquidity_usd": 5000,
    "max_liquidity_usd": 50000,
    "max_market_cap": 500000,
    "max_pair_age_hours": 168,
    "min_volume_liquidity_ratio": 0.5,
    "min_price_change_1h": 0.0,
    "min_price_change_6h": 0.0,
    "min_buy_sell_ratio": 1.0,
    "min_txns_1h": 10,
    "min_alert_score": 50,
    "weights": {
        "liquidity": 15,
        "market_cap": 15,
        "pair_age": 10,
        "vol_liq_ratio": 20,
        "price_change": 20,
        "buy_sell_ratio": 20,
    },
    "safety": {
        "max_buy_tax_pct": 10,
        "max_sell_tax_pct": 10,
        "max_top10_holder_pct": 70,
        "reject_honeypot": True,
        "reject_mint_authority": True,
        "reject_blacklist": True,
    },
}


def _load_chain_configs() -> dict[str, dict[str, Any]]:
    """Load chain_config.yaml and return a dict keyed by chain name."""
    if not _CONFIG_PATH.exists():
        logger.warning("chain_config.yaml not found at %s -- using built-in defaults", _CONFIG_PATH)
        return {"default": _DEFAULT_PROFILE}

    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        logger.error("chain_config.yaml is not a valid mapping -- using defaults")
        return {"default": _DEFAULT_PROFILE}

    return raw


CHAIN_CONFIGS: dict[str, dict[str, Any]] = _load_chain_configs()


def get_chain_profile(chain_id: str) -> dict[str, Any]:
    """Return the config profile for a chain, falling back to 'default'."""
    chain = chain_id.lower()
    if chain in CHAIN_CONFIGS:
        return CHAIN_CONFIGS[chain]
    if "default" in CHAIN_CONFIGS:
        logger.warning("No config profile for chain '%s' -- using default", chain)
        return CHAIN_CONFIGS["default"]
    logger.warning("No config profile for chain '%s' and no default -- using built-in", chain)
    return _DEFAULT_PROFILE

