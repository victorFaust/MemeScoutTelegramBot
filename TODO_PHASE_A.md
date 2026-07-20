# TODO_PHASE_A

## Step 1
- [ ] Add DB table `wallet_sells` and position exit-state storage helper(s) in `storage.py`.

## Step 2
- [ ] Fix `wallet_tracker.py` sell detection + buy detection bug, add `on_wallet_sell` callback plumbing.

## Step 3
- [ ] Update `main.py` to pass `on_wallet_sell` into `wallet_tracker.poll_tracked_wallets` and implement `_handle_wallet_sell`.

## Step 4
- [ ] Implement 3-stage DCA exit (+50%/+100%/final trailing) by replacing `_exit_monitor_loop` logic in `main.py`.

## Step 5
- [ ] Add per-position exit state tracking to avoid double-selling (DB or in-memory with persistence).

## Step 6
- [ ] Ensure notifications for staged exits in Telegram.

## Step 7
- [ ] Run `python -c "import main"` and sanity imports for modules.
