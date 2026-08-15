"""Regression tests for credential masking and RPC wallet tracking."""

import logging
import unittest
from unittest.mock import patch

import main
import wallet_tracker


class SecretFilterTests(unittest.TestCase):
    def test_telegram_and_query_credentials_are_redacted(self):
        record = logging.LogRecord(
            "httpx", logging.INFO, __file__, 1,
            "POST https://api.telegram.org/bot123456:secret-token/getUpdates?api-key=abc123",
            (), None,
        )
        main._SecretFilter().filter(record)
        message = record.getMessage()
        self.assertNotIn("secret-token", message)
        self.assertNotIn("abc123", message)
        self.assertIn("[REDACTED]", message)


class WalletRpcTests(unittest.TestCase):
    def setUp(self):
        wallet_tracker._fetch_warning_at.clear()
        wallet_tracker._last_wallet_signature.clear()

    def _transaction(self, sol_before=2_000_000_000, sol_after=1_000_000_000,
                     token_before=0, token_after=100):
        def bal(amount):
            return {"owner": "wallet", "mint": "TOKEN",
                    "uiTokenAmount": {"uiAmount": amount}}
        return {
            "blockTime": 123,
            "transaction": {"message": {"accountKeys": [{"pubkey": "wallet"}]}},
            "meta": {
                "preBalances": [sol_before], "postBalances": [sol_after],
                "preTokenBalances": [bal(token_before)],
                "postTokenBalances": [bal(token_after)],
            },
        }

    def test_parses_native_sol_buy(self):
        swap = wallet_tracker._parse_rpc_swap(self._transaction(), "wallet", "sig")
        self.assertEqual(swap["token_bought"], "TOKEN")
        self.assertEqual(swap["token_sold"], wallet_tracker.SOL_MINT)
        self.assertEqual(swap["amount_sol"], 1.0)

    @patch("wallet_tracker.rpc_client.rpc_call")
    def test_fetch_uses_cursor_on_next_poll(self, rpc_call):
        rpc_call.side_effect = [
            [{"signature": "sig1", "err": None}], self._transaction(), []
        ]
        swaps = wallet_tracker.fetch_recent_swaps("wallet", 5)
        self.assertEqual(len(swaps), 1)
        wallet_tracker.fetch_recent_swaps("wallet", 5)
        second_options = rpc_call.call_args_list[2].args[1][1]
        self.assertEqual(second_options["until"], "sig1")

    @patch("wallet_tracker.rpc_client.rpc_call", return_value=None)
    def test_provider_failure_warning_is_throttled(self, _rpc_call):
        with self.assertLogs("wallet_tracker", level="WARNING") as logs:
            wallet_tracker.fetch_recent_swaps("wallet-address")
            wallet_tracker.fetch_recent_swaps("wallet-address")
        text = " ".join(logs.output)
        self.assertEqual(text.count("All RPC providers failed"), 1)


if __name__ == "__main__":
    unittest.main()
