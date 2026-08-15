"""Regression tests for credential masking and RPC wallet tracking."""

import logging
import unittest
from unittest.mock import Mock, patch

import main
import rpc_client
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


class AlchemyProviderTests(unittest.TestCase):
    def setUp(self):
        rpc_client._initialized = False
        rpc_client._providers = []
        rpc_client._provider_cooldowns.clear()
        rpc_client._current_index = 0

    @patch("rpc_client.requests.post")
    @patch("rpc_client.config.ALCHEMY_RPC_URL", "https://solana-mainnet.g.alchemy.com/v2/test")
    @patch("rpc_client.config.QUICKNODE_HTTP_URL", "https://quicknode.test")
    @patch("rpc_client.config.SHYFT_HTTP_URL", "")
    def test_alchemy_is_preferred_for_wallet_calls(self, request_post):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": []}
        request_post.return_value = response
        result = rpc_client.rpc_call(
            "getSignaturesForAddress", ["wallet"], preferred_name="Alchemy"
        )
        self.assertEqual(result, [])
        self.assertEqual(
            request_post.call_args.args[0],
            "https://solana-mainnet.g.alchemy.com/v2/test",
        )

    @patch("wallet_tracker.rpc_client.rpc_call")
    @patch("wallet_tracker.config.ALCHEMY_RPC_URL", "https://alchemy.test")
    def test_alchemy_extracts_early_token_recipient(self, rpc_call):
        def balance(owner, amount):
            return {"owner": owner, "mint": "TOKEN",
                    "uiTokenAmount": {"uiAmount": amount}}
        rpc_call.return_value = {"transactions": [{"meta": {
            "preTokenBalances": [balance("buyer", 0)],
            "postTokenBalances": [balance("buyer", 25)],
        }}]}
        buyers = wallet_tracker._fetch_early_buyers_alchemy("TOKEN", 10)
        self.assertEqual(buyers, ["buyer"])

    @patch("wallet_tracker._get_from_url")
    def test_empty_birdeye_result_is_not_a_provider_failure(self, get_url):
        response = Mock()
        response.json.return_value = {"data": {"items": []}}
        get_url.return_value = response
        self.assertEqual(wallet_tracker._fetch_early_buyers_birdeye("TOKEN", 10), [])


if __name__ == "__main__":
    unittest.main()
