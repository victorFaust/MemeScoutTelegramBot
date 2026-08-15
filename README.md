# DexScreener Memecoin Alert Bot

Monitors low-cap tokens, scores and safety-checks candidates, tracks smart wallets, and provides a private Telegram trading dashboard for Solana execution through Jupiter and Jito.

> Trading uses a hot wallet. Keep only trading funds in it, restrict `TELEGRAM_CHAT_ID`, and never commit private keys.

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and fill in your secrets:

```bash
cp .env.example .env
```

Required:
- `TELEGRAM_BOT_TOKEN` -- create a bot via [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` -- send `/start` to your bot, then get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`

Global settings (in `.env`):
- `CHAINS` -- comma-separated list of chains to monitor (default: `solana,base`)
- `POLL_INTERVAL_SECONDS` -- how often to poll (default: `180`)
- `DEDUP_COOLDOWN_HOURS` -- dedup window (default: `6`)
- `SAFETY_CHECK_CACHE_HOURS` -- cache GoPlus results (default: `1`)
- `SKIP_ON_SAFETY_CHECK_FAILURE` -- `true` = skip alert if safety API fails (default: `true`)

Trading requires `TRADING_ENABLED=true` and `TRADING_WALLET_PRIVATE_KEY`. Optional additional wallets are configured with `TRADING_WALLET_KEYS`. RPC provider secrets, including `ALCHEMY_RPC_URL`, should be configured in Render rather than committed.

## Telegram Trading

The `/start` dashboard provides market buy/sell, reconciled positions, persistent orders, wallets, execution profiles, watchlist, history, auto-buy controls, and an emergency stop.

- `/buy <token_address> $5` — submit a market buy
- `/limitbuy <token_address> <price_usd> $5` — create a restart-safe limit buy
- `/positions` and `/sell <id>` — inspect or close positions
- `/orders` and `/cancelorder <id>` — manage persistent limit/stop-loss/take-profit orders
- `/profile <wallet#> <slippage%> <priority_lamports> <jito_tip> <on|off> [presets]` — configure execution per wallet
- `/trades`, `/trade <id>`, and `/pnl` — review lifecycle and performance
- `/stop` — disable trading and auto-buy immediately

Confirmed buys automatically receive durable stop-loss and staged take-profit orders. Transaction confirmation and wallet token balances are reconciled in the background; mismatches are surfaced without silently rewriting trade history.

Tracked wallets use Alchemy WebSocket log subscriptions for low-latency detection. The tracker reconnects automatically, refreshes subscriptions when the wallet list changes, and runs an RPC polling pass during every stream outage. Stream mode, subscription count, and recent processing latency appear on the Wallets screen.

### 3. Per-Chain Configuration

All filter thresholds, scoring weights, and safety check tolerances are in `chain_config.yaml`. Edit this file to tune per chain -- no Python changes needed.

**To add a new chain:**

1. Add the chain name to `CHAINS` in `.env` (e.g. `CHAINS=solana,base,bsc`)
2. Add a new block to `chain_config.yaml`:

```yaml
bsc:
  min_liquidity_usd: 3000
  max_liquidity_usd: 60000
  max_market_cap: 600000
  max_pair_age_hours: 120
  min_volume_liquidity_ratio: 0.4
  min_price_change_1h: 0.0
  min_price_change_6h: 0.0
  min_buy_sell_ratio: 1.0
  min_txns_1h: 8
  min_alert_score: 45

  weights:
    liquidity: 15
    market_cap: 15
    pair_age: 10
    vol_liq_ratio: 20
    price_change: 20
    buy_sell_ratio: 20

  safety:
    max_buy_tax_pct: 10
    max_sell_tax_pct: 10
    max_top10_holder_pct: 70
    reject_honeypot: true
    reject_mint_authority: false
    reject_blacklist: true
```

3. If no entry exists for a chain, the `default` profile is used automatically.

### 4. Run

```bash
python main.py
```

### Docker

```bash
docker compose up -d
```

## Scoring

Each pair is scored 0-100 using per-chain weighted criteria:

| Criterion | Default Weight | Description |
|---|---|---|
| Volume/Liquidity ratio | 20 | Higher trading activity relative to pool size |
| Price momentum | 20 | Positive 1h + 6h price change |
| Buy/sell ratio | 20 | More buys than sells in last hour |
| Liquidity | 15 | In the sweet-spot range |
| Market cap | 15 | Lower cap = more room to grow |
| Pair age | 10 | Newer pairs score higher |

Pairs below `min_alert_score` (per chain, default 50) are not alerted.

## Safety Check (GoPlus API)

After scoring, each token is checked against the GoPlus Security API:
- Honeypot detection
- Buy/sell tax
- Mint authority status (Solana)
- Blacklist/whitelist functions
- Top 10 holder concentration

Tokens that fail safety criteria are silently dropped. Results are cached in SQLite for 1 hour (configurable).

## Dedup

Alerted tokens are tracked in `alerts.db`. The same token won't re-alert within the cooldown window (default 6 hours).

## Project Structure

```
main.py                 Entry point + async polling loop
config.py               Loads .env (secrets) + chain_config.yaml (thresholds)
chain_config.yaml       Per-chain filter/scoring/safety settings
dexscreener_client.py   DexScreener API wrapper
filters.py              Per-chain scoring and filtering
safety_check.py         GoPlus honeypot/contract safety checks
telegram_notifier.py    Telegram message formatting + sending
storage.py              SQLite dedup + safety cache
executor.py             Jupiter quotes, signing, Jito/RPC submission, reconciliation
order_engine.py         Persistent limit and exit order evaluation
bot_handler.py          Private Telegram trading dashboard and commands
test_safety.py          Test script for safety_check module
```
