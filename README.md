# DexScreener Memecoin Alert Bot

Monitors DexScreener for ultra-low-cap memecoins with breakout potential and sends alerts to Telegram. **Read-only / alert-only** — no trading or wallet functionality.

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required:
- `TELEGRAM_BOT_TOKEN` — create a bot via [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` — send `/start` to your bot, then get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`

Optional (all have sensible defaults):
- `CHAINS` — comma-separated list of chains to monitor (default: `solana,base`)
- `POLL_INTERVAL_SECONDS` — how often to poll (default: `180`)
- Filter thresholds — see `.env.example` for the full list

### 3. Run

```bash
python main.py
```

### Docker

```bash
# Create .env first, then:
docker compose up -d
```

## Scoring

Each discovered pair is scored 0-100 based on weighted criteria:

| Criterion | Weight | Description |
|---|---|---|
| Volume/Liquidity ratio | 20 | Higher trading activity relative to pool size |
| Price momentum | 20 | Positive 1h + 6h price change |
| Buy/sell ratio | 20 | More buys than sells in last hour |
| Liquidity | 15 | In the $5K–$50K sweet spot |
| Market cap | 15 | Lower cap = more room to grow |
| Pair age | 10 | Newer pairs score higher |

Pairs below `MIN_ALERT_SCORE` (default 50) are not alerted. Hard filters reject pairs that fail minimum thresholds before scoring.

## Dedup

Alerted tokens are tracked in a local SQLite database (`alerts.db`). The same token won't trigger another alert within the cooldown window (default 6 hours).

## Project Structure

```
main.py                 Entry point + polling loop
config.py               Settings loaded from .env
dexscreener_client.py   DexScreener API wrapper
filters.py              Scoring and filtering logic
telegram_notifier.py    Telegram message formatting + sending
storage.py              SQLite dedup tracking
```
