"""Regression tests for event-driven wallet tracking and fallback processing."""

import unittest
from unittest.mock import AsyncMock, patch

import wallet_tracker


class WalletStreamDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_buy_uses_existing_dedup_and_confidence_pipeline(self):
        callback = AsyncMock()
        swap = {"token_bought": "mint", "token_sold": wallet_tracker.SOL_MINT, "signature": "sig"}
        with patch("wallet_tracker.was_buy_already_seen", return_value=False), \
             patch("wallet_tracker.storage.was_recently_alerted", return_value=False), \
             patch("wallet_tracker.get_confidence_for_token", return_value=2), \
             patch("wallet_tracker.record_wallet_buy") as record:
            await wallet_tracker._dispatch_swap("wallet", swap, callback)
        record.assert_called_once_with("wallet", "mint", "sig", 3)
        callback.assert_awaited_once_with("wallet", "mint", 3, "sig")

    async def test_duplicate_stream_buy_is_ignored(self):
        callback = AsyncMock()
        swap = {"token_bought": "mint", "token_sold": wallet_tracker.SOL_MINT, "signature": "sig"}
        with patch("wallet_tracker.was_buy_already_seen", return_value=True), \
             patch("wallet_tracker.record_wallet_buy") as record:
            await wallet_tracker._dispatch_swap("wallet", swap, callback)
        record.assert_not_called()
        callback.assert_not_awaited()

    async def test_stream_sell_calls_sell_pipeline(self):
        callback = AsyncMock()
        swap = {"token_bought": wallet_tracker.SOL_MINT, "token_sold": "mint", "signature": "sig"}
        with patch("wallet_tracker.was_buy_already_seen", return_value=False), \
             patch("wallet_tracker.record_wallet_buy") as record:
            await wallet_tracker._dispatch_swap("wallet", swap, AsyncMock(), callback)
        record.assert_called_once_with("wallet", "sell_mint", "sig", 0)
        callback.assert_awaited_once_with("wallet", "mint", "sig")


class StreamTransactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_notified_transaction_retries_until_rpc_indexes_it(self):
        transaction = {"meta": {"err": None}}
        with patch("wallet_tracker.rpc_client.rpc_call", side_effect=[None, transaction]) as rpc, \
             patch("wallet_tracker._parse_rpc_swap", return_value={"signature": "sig"}) as parse, \
             patch("wallet_tracker.asyncio.sleep", new_callable=AsyncMock):
            result = await wallet_tracker._fetch_stream_transaction("wallet", "sig")
        self.assertEqual(result, {"signature": "sig"})
        self.assertEqual(rpc.call_count, 2)
        parse.assert_called_once_with(transaction, "wallet", "sig")


if __name__ == "__main__":
    unittest.main()
