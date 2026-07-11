"""Startup self-check module.

Runs once at boot before schedulers start. Validates config, database,
Telegram connectivity, DexScreener chain resolution, and rate limit budget.
Hard failures exit the process; soft failures log warnings and continue.
"""

import logging
import sqlite3
import sys
import time
from pathlib import Path

import requests

import config
import storage

logger = logging.getLogger(__name__)

_RESULTS: list[str] = []


def _pass(msg: str) -> None:
    _RESULTS.append(f"  [OK] {msg}")
    logger.info("[STARTUP OK] %s", msg)


def _warn(msg: str) -> None:
    _RESULTS.append(f"  [!!] {msg}")
    logger.warning("[STARTUP WARN] %s", msg)


def _fail(msg: str) -> None:
    _RESULTS.append(f"  [FAIL] {msg}")
    logger.error("[STARTUP FAIL] %s", msg)


def _hard_exit(msg: str) -> None:
    """Log error and exit with non-zero code."""
    _fail(msg)
    _print_summary()
    logger.critical("Startup aborted due to hard failure: %s", msg)
    sys.exit(1)


def _print_summary() -> None:
    logger.info("")
    logger.info("=" * 50)
    logger.info("  STARTUP CHECK SUMMARY")
    logger.info("=" * 50)
    for line in _RESULTS:
        logger.info(line)
    logger.info("=" * 50)
    logger.info("")


# -- Check 1: Config validation --

def _check_config() -> None:
    """Validate config loads, chains are non-empty, env vars present."""
    # Config already loaded at import time -- if it failed, we wouldn't be here.
    # But verify the chain_config.yaml was actually parsed.
    if not config.CHAIN_CONFIGS:
        _hard_exit("chain_config.yaml failed to load or is empty")

    if not config.CHAINS:
        _hard_exit("CHAINS is empty -- no chains configured in .env")

    # Check each chain has a profile (or at least default exists)
    for chain in config.CHAINS:
        profile = config.get_chain_profile(chain)
        if not profile:
            _hard_exit(f"No config profile found for chain '{chain}' and no 'default' fallback")

    # Required env vars
    if not config.TELEGRAM_BOT_TOKEN:
        _hard_exit("TELEGRAM_BOT_TOKEN is missing or empty in .env")
    if not config.TELEGRAM_CHAT_ID:
        _hard_exit("TELEGRAM_CHAT_ID is missing or empty in .env")

    _pass("Config valid (chains: %s)" % ", ".join(config.CHAINS))


# -- Check 2: Database / disk --

def _check_database() -> None:
    """Verify DB path is writable and all tables exist."""
    db_path = storage.DB_PATH
    db_dir = db_path.parent

    # Directory exists and is writable
    if not db_dir.exists():
        _hard_exit(f"Database directory does not exist: {db_dir}")
    if not db_dir.is_dir():
        _hard_exit(f"Database path parent is not a directory: {db_dir}")

    # Try to open/create the database (this also runs CREATE TABLE IF NOT EXISTS)
    try:
        conn = storage._connect()
    except Exception as e:
        _hard_exit(f"Cannot open database at {db_path}: {e}")

    # Verify expected tables exist
    expected_tables = {"alerted_tokens", "safety_cache", "alert_outcomes"}
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        existing = {r[0] for r in rows}
        missing = expected_tables - existing
        if missing:
            _hard_exit(f"Missing database tables: {missing}")
    except Exception as e:
        _hard_exit(f"Cannot query database schema: {e}")

    # Write test -- insert and delete a throwaway row
    try:
        conn.execute(
            "INSERT INTO alerted_tokens (token_address, chain_id, alerted_at, score) "
            "VALUES ('__startup_test__', '__test__', 0, 0)"
        )
        conn.execute(
            "DELETE FROM alerted_tokens WHERE token_address = '__startup_test__'"
        )
        conn.commit()
    except Exception as e:
        _hard_exit(f"Database is not writable: {e}")
    finally:
        conn.close()

    _pass(f"Database writable ({len(expected_tables)} tables confirmed)")


# -- Check 3: Telegram connectivity --

def _check_telegram() -> None:
    """Verify bot token is valid and can send messages."""
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID

    # getMe -- verify token
    bot_name = None
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if not data.get("ok"):
            _hard_exit(f"Telegram bot token invalid: {data.get('description', 'unknown error')}")
        bot_name = data.get("result", {}).get("username", "unknown")
    except requests.RequestException as e:
        _hard_exit(f"Cannot reach Telegram API (network issue): {e}")

    # Send startup message (retry once on failure)
    chains_str = " + ".join(c.capitalize() for c in config.CHAINS)
    startup_msg = f"Bot started -- {chains_str} monitoring active"

    sent = False
    last_error = ""
    for attempt in range(2):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": startup_msg},
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                sent = True
                break
            else:
                last_error = data.get("description", "unknown")
                if "chat not found" in last_error.lower() or "invalid" in last_error.lower():
                    _hard_exit(f"Telegram chat ID invalid: {last_error}")
        except requests.RequestException as e:
            last_error = str(e)

        if attempt == 0:
            time.sleep(2)  # retry after short pause

    if sent:
        _pass(f"Telegram connected (bot: @{bot_name})")
    else:
        _warn(f"Telegram startup message failed (transient): {last_error}")
        _RESULTS[-1] = f"  [!!] Telegram: token valid (@{bot_name}) but startup message failed: {last_error}"


# -- Check 4: DexScreener chain resolution --

def _check_dexscreener_chains() -> None:
    """Verify each configured chain resolves on DexScreener."""
    # Known test queries per chain
    test_queries = {
        "solana": "SOL",
        "robinhood": "cashcat",
    }

    for chain in config.CHAINS:
        query = test_queries.get(chain, chain)
        try:
            resp = requests.get(
                "https://api.dexscreener.com/latest/dex/search",
                params={"q": query},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            pairs = data.get("pairs", [])
            chain_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain]
            if chain_pairs:
                _pass(f"{chain} chainId resolved ({len(chain_pairs)} pairs found)")
            else:
                _warn(f"{chain} chainId resolution failed -- no pairs with chainId='{chain}' in search results for '{query}'")
        except requests.RequestException as e:
            _warn(f"{chain} chainId check failed (DexScreener unreachable): {e}")


# -- Check 5: Rate limit budget --

def _check_rate_budget() -> None:
    """Estimate max API calls/min and warn if exceeding the ceiling."""
    ceiling = 250  # from main.py rate limit guard
    total_calls_per_min = 0.0

    for chain in config.CHAINS:
        cfg = config.get_chain_profile(chain)
        interval = cfg.get("poll_interval_seconds", 180)
        # Estimate: each cycle does ~2 (discovery) + ~10 (pair fetches avg) = ~12 calls
        estimated_calls_per_cycle = 12
        cycles_per_min = 60.0 / interval
        total_calls_per_min += cycles_per_min * estimated_calls_per_cycle

    # Add snapshot tracker: ~5 calls every 5 min = 1/min
    total_calls_per_min += 1

    est = int(total_calls_per_min)
    if est > ceiling:
        _warn(f"Rate limit budget EXCEEDED: estimated {est}/min vs {ceiling}/min ceiling -- "
              "consider increasing poll intervals")
    else:
        _pass(f"Rate limit budget OK (est. {est}/min of {ceiling} ceiling)")


# -- Public entry point --

def run_startup_checks() -> None:
    """Run all startup checks. Hard failures exit the process."""
    logger.info("=" * 50)
    logger.info("  RUNNING STARTUP SELF-CHECKS")
    logger.info("=" * 50)

    start = time.time()

    _check_config()
    _check_database()
    _check_telegram()
    _check_dexscreener_chains()
    _check_rate_budget()

    elapsed = time.time() - start
    _print_summary()
    logger.info("Startup checks completed in %.1fs", elapsed)
