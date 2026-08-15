"""Regression tests for manual buy validation and safety rails."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot_handler
import executor


VALID_MINT = "So11111111111111111111111111111111111111112"


class BuyCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_buy_is_blocked_before_execution(self):
        message = SimpleNamespace(text="/buy", chat_id=999, reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message, callback_query=None,
            effective_user=SimpleNamespace(id=999),
        )
        context = SimpleNamespace(args=[VALID_MINT, "$5"])

        with patch.object(bot_handler.config, "TELEGRAM_CHAT_ID", "123"), \
             patch("bot_handler.executor.buy_token") as buy:
            await bot_handler._handle_buy_command(update, context)

        buy.assert_not_called()
        message.reply_text.assert_awaited_once_with("Not authorized")

    async def test_subminimum_buy_is_rejected(self):
        message = SimpleNamespace(text="/buy", chat_id=123, reply_text=AsyncMock())
        update = SimpleNamespace(
            message=message, callback_query=None,
            effective_user=SimpleNamespace(id=123),
        )
        context = SimpleNamespace(args=[VALID_MINT, "$0.49"])

        with patch.object(bot_handler.config, "TELEGRAM_CHAT_ID", "123"), \
             patch("bot_handler.executor.buy_token") as buy:
            await bot_handler._handle_buy_command(update, context)

        buy.assert_not_called()
        message.reply_text.assert_awaited_once_with("Amount: $0.50 - $100")


class BuyExecutorTests(unittest.TestCase):
    @patch("executor.get_quote")
    def test_invalid_mint_never_reaches_quote(self, get_quote):
        self.assertIsNone(executor.buy_token("not-a-solana-mint", 0.01))
        get_quote.assert_not_called()

    def test_usd_conversion_fails_closed_without_price(self):
        with patch("executor.get_sol_price", return_value=0):
            self.assertEqual(executor.usd_to_sol(5), 0)

    @patch("executor.get_quote")
    @patch("executor.can_trade", return_value=(True, "OK"))
    def test_buy_that_would_exceed_daily_limit_is_blocked(self, _can_trade, get_quote):
        old_spent = executor._daily_spent_sol
        old_reset = executor._daily_reset_time
        try:
            executor._daily_spent_sol = 0.9
            executor._daily_reset_time = __import__("time").time()
            with patch.object(executor.config, "DAILY_LOSS_LIMIT_SOL", 1.0):
                self.assertIsNone(executor.buy_token(VALID_MINT, 0.2))
            get_quote.assert_not_called()
        finally:
            executor._daily_spent_sol = old_spent
            executor._daily_reset_time = old_reset


if __name__ == "__main__":
    unittest.main()
