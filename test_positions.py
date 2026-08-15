"""Regression tests for /positions rendering and sell safety."""

import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import bot_handler
import storage


def _position(pos_id=1, symbol="TOK", tx_status="confirmed"):
    return {
        "id": pos_id,
        "status": "open",
        "tx_status": tx_status,
        "token_address": f"token-{pos_id}",
        "token_symbol": symbol,
        "token_amount": 100,
        "buy_amount_sol": 1.0,
        "entry_mc": 1000,
        "entry_price_usd": 0.01,
    }


class PositionSafetyTests(unittest.TestCase):
    def test_pending_position_is_not_sellable(self):
        self.assertFalse(bot_handler._position_is_sellable(_position(tx_status="pending")))

    def test_confirmed_position_is_sellable(self):
        self.assertTrue(bot_handler._position_is_sellable(_position()))


class PositionClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "positions.db"
        storage.record_position("token", "solana", 1.0, 100, "sig")
        self.position_id = storage.get_open_positions()[0]["id"]

    def tearDown(self):
        storage.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_only_one_sell_can_claim_position(self):
        self.assertTrue(storage.claim_position_for_sell(self.position_id))
        self.assertFalse(storage.claim_position_for_sell(self.position_id))

    def test_failed_sell_can_release_position(self):
        self.assertTrue(storage.claim_position_for_sell(self.position_id))
        storage.release_position_sell(self.position_id)
        self.assertTrue(storage.claim_position_for_sell(self.position_id))

    def test_position_keeps_owning_wallet(self):
        storage.record_position("token-2", "solana", 0.5, 50, "sig-2", wallet_address="wallet-2")
        position = next(p for p in storage.get_open_positions() if p["token_address"] == "token-2")
        self.assertEqual(position["wallet_address"], "wallet-2")


class PositionRenderingTests(unittest.IsolatedAsyncioTestCase):
    @patch("dexscreener_client.fetch_pair_details", return_value=[])
    @patch("bot_handler.executor.check_position_pnl")
    @patch("bot_handler.storage.get_open_positions")
    async def test_unavailable_quote_is_not_counted_as_zero_value(
        self, get_positions, check_pnl, _fetch_pairs
    ):
        quoted = _position(1, "GOOD")
        unavailable = _position(2, "UNKNOWN")
        get_positions.return_value = [quoted, unavailable]
        check_pnl.side_effect = [
            {"current_value_sol": 1.5, "pnl_pct": 50.0, "pnl_sol": 0.5},
            None,
        ]
        message = SimpleNamespace(reply_text=AsyncMock())

        await bot_handler._show_positions(message)

        text = message.reply_text.await_args.args[0]
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertIn("Invested: 2.0000 SOL", text)
        self.assertIn("Quoted value: 1.5000 SOL | 1 unavailable", text)
        self.assertIn("Quoted PnL: +50%", text)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertIn("menu:sell_2", callbacks)

    @patch("bot_handler._is_authorized", return_value=True)
    @patch("bot_handler._show_positions", new_callable=AsyncMock)
    async def test_positions_command_opens_portfolio_directly(self, show_positions, _authorized):
        message = AsyncMock()
        update = Mock(message=message)
        await bot_handler._handle_positions_command(update, None)
        show_positions.assert_awaited_once_with(message)


if __name__ == "__main__":
    unittest.main()
