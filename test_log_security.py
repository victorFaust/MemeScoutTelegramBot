"""Regression tests for credential masking and wallet API retries."""

import logging
import unittest
from unittest.mock import Mock, patch

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


class WalletRetryTests(unittest.TestCase):
    def setUp(self):
        wallet_tracker._fetch_warning_at.clear()

    @patch("wallet_tracker.time.sleep")
    @patch("wallet_tracker.requests.get")
    def test_rate_limit_retries_then_succeeds(self, request_get, _sleep):
        limited = Mock(status_code=429, headers={"Retry-After": "1"})
        success = Mock(status_code=200, headers={})
        success.raise_for_status.return_value = None
        success.json.return_value = []
        request_get.side_effect = [limited, success]
        self.assertEqual(wallet_tracker.fetch_recent_swaps("wallet"), [])
        self.assertEqual(request_get.call_count, 2)

    @patch("wallet_tracker.time.sleep")
    @patch("wallet_tracker.requests.get")
    def test_failure_warning_does_not_include_url(self, request_get, _sleep):
        request_get.side_effect = wallet_tracker.requests.ConnectionError(
            "https://api.helius.xyz/?api-key=secret"
        )
        with self.assertLogs("wallet_tracker", level="WARNING") as logs:
            wallet_tracker.fetch_recent_swaps("wallet-address")
        text = " ".join(logs.output)
        self.assertNotIn("secret", text)
        self.assertIn("ConnectionError", text)


if __name__ == "__main__":
    unittest.main()
