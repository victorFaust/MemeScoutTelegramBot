"""Entry point -- polling loop that discovers, scores, and alerts."""

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

import config
import dexscreener_client as dex
import filters
import storage
import telegram_notifier as tg


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Rotating file
    fh = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


logger = logging.getLogger(__name__)


async def _run_cycle() -> None:
    """Single polling cycle: discover -> fetch pairs -> score -> alert."""
    tokens = dex.discover_tokens(config.CHAINS)
    logger.info("Cycle start -- %d tokens discovered", len(tokens))

    all_pairs: list[dict] = []
    for tok in tokens:
        chain = tok["chainId"]
        addr = tok["tokenAddress"]
        pairs = dex.fetch_pair_details(chain, addr)
        all_pairs.extend(pairs)

    logger.info("Fetched %d total pairs", len(all_pairs))

    scored = filters.filter_and_score(all_pairs)
    logger.info("%d pairs passed scoring threshold", len(scored))

    sent = 0
    for result in scored:
        pair = result["pair"]
        chain = (pair.get("chainId") or "").lower()
        base = pair.get("baseToken") or {}
        addr = base.get("address") or pair.get("tokenAddress", "")

        if storage.was_recently_alerted(chain, addr):
            logger.debug("Skipping %s (recently alerted)", addr)
            continue

        ok = await tg.send_alert(result)
        if ok:
            storage.record_alert(chain, addr, result["score"])
            sent += 1

    logger.info("Cycle done -- %d alerts sent", sent)


async def main() -> None:
    _setup_logging()
    logger.info(
        "Bot starting -- chains=%s, interval=%ds, min_score=%.1f",
        config.CHAINS, config.POLL_INTERVAL_SECONDS, config.MIN_ALERT_SCORE,
    )

    # Periodic old-record cleanup
    last_cleanup = 0.0

    while True:
        try:
            await _run_cycle()
        except Exception:
            logger.exception("Unhandled error in polling cycle")

        # Cleanup once per day
        if time.time() - last_cleanup > 86400:
            storage.cleanup_old_records()
            last_cleanup = time.time()

        logger.info("Sleeping %ds until next cycle...", config.POLL_INTERVAL_SECONDS)
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
