"""Regression tests for wallet profiles and the trading dashboard."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_handler
import storage


VALID_MINT = "So11111111111111111111111111111111111111112"


class WalletProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "profiles.db"

    def tearDown(self):
        storage.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_profile_is_persistent_per_wallet(self):
        storage.update_wallet_profile(
            "wallet-a", slippage_bps=750, priority_fee_lamports=222,
            jito_tip_lamports=333, mev_protection=0, buy_presets_usd="1,5",
        )
        profile = storage.get_wallet_profile("wallet-a")
        self.assertEqual(profile["slippage_bps"], 750)
        self.assertEqual(profile["priority_fee_lamports"], 222)
        self.assertEqual(profile["jito_tip_lamports"], 333)
        self.assertEqual(profile["mev_protection"], 0)
        self.assertEqual(profile["buy_presets_usd"], "1,5")


class DashboardTests(unittest.TestCase):
    def test_dashboard_exposes_primary_trading_actions(self):
        labels = [button.text for row in bot_handler._main_menu_keyboard().inline_keyboard for button in row]
        self.assertTrue(any("Buy" in label for label in labels))
        self.assertTrue(any("Positions" in label for label in labels))
        self.assertTrue(any("Orders" in label for label in labels))
        self.assertTrue(any("Profiles" in label for label in labels))


class LimitBuyCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_buy_creates_durable_order(self):
        message = SimpleNamespace(chat_id=1, reply_text=AsyncMock())
        update = SimpleNamespace(message=message, callback_query=None, effective_user=SimpleNamespace(id=1))
        context = SimpleNamespace(args=[VALID_MINT, "0.001", "$5"])
        with patch.object(bot_handler.config, "TELEGRAM_CHAT_ID", "1"), \
             patch("bot_handler.executor.usd_to_sol", return_value=0.025), \
             patch("bot_handler.storage.create_trade_order", return_value=9) as create:
            await bot_handler._handle_limitbuy_command(update, context)
        create.assert_called_once_with(VALID_MINT, "limit_buy", 0.001, amount_sol=0.025)
        self.assertIn("Limit buy #9 created", message.reply_text.await_args.args[0])

    async def test_unauthorized_limit_buy_is_blocked(self):
        message = SimpleNamespace(chat_id=2, reply_text=AsyncMock())
        update = SimpleNamespace(message=message, callback_query=None, effective_user=SimpleNamespace(id=2))
        context = SimpleNamespace(args=[VALID_MINT, "0.001", "$5"])
        with patch.object(bot_handler.config, "TELEGRAM_CHAT_ID", "1"), \
             patch("bot_handler.storage.create_trade_order") as create:
            await bot_handler._handle_limitbuy_command(update, context)
        create.assert_not_called()
        message.reply_text.assert_awaited_once_with("Not authorized")


if __name__ == "__main__":
    unittest.main()
