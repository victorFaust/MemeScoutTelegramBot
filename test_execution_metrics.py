"""Regression tests for persistent execution-quality telemetry."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_handler
import executor
import main
import storage


class ExecutionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "execution.db"

    def tearDown(self):
        storage.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_submitted_attempt_remains_pending_until_terminal(self):
        attempt_id = storage.create_execution_attempt("buy", "mint", "wallet")
        storage.update_execution_attempt(
            attempt_id, status="submitted", signature="sig", submitted_at=time.time(),
            quote_ms=10, build_ms=20, sign_ms=2, submit_ms=30, expected_out=100,
            submit_provider="jito",
        )
        pending = storage.get_pending_execution_attempts()
        self.assertEqual([row["id"] for row in pending], [attempt_id])
        storage.update_execution_attempt(attempt_id, status="confirmed", realized_out=98)
        self.assertEqual(storage.get_pending_execution_attempts(), [])

    def test_execution_report_includes_latency_and_realized_output(self):
        attempt_id = storage.create_execution_attempt("sell", "mint", "wallet")
        storage.update_execution_attempt(
            attempt_id, status="confirmed", signature="sig", submitted_at=time.time(),
            confirmed_at=time.time(), quote_ms=10, build_ms=20, sign_ms=2,
            submit_ms=30, confirmation_ms=500, expected_out=100, realized_out=98,
            submit_provider="rpc",
        )
        report = bot_handler._execution_report(7)
        self.assertIn("Confirmed: 1", report)
        self.assertIn("Quote 10ms", report)
        self.assertIn("Realized vs quote: -2.00%", report)
        self.assertIn("RPC: 1/1 confirmed", report)


class ConfirmationTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sell_confirmation_records_realized_sol_and_latency(self):
        attempt = {
            "id": 4, "side": "sell", "token_address": "mint", "wallet_address": "wallet",
            "signature": "sig", "status": "submitted", "submitted_at": time.time() - 1,
            "started_at": time.time() - 2,
        }
        with patch("main.storage.get_pending_execution_attempts", return_value=[attempt]), \
             patch("executor.confirm_transaction", return_value="confirmed"), \
             patch("executor.get_confirmed_sol_delta", return_value=123) as delta, \
             patch("main.storage.update_execution_attempt") as update:
            await main._reconcile_execution_attempts()
        delta.assert_called_once_with("sig", "wallet")
        kwargs = update.call_args.kwargs
        self.assertEqual(kwargs["status"], "confirmed")
        self.assertEqual(kwargs["realized_out"], 123)
        self.assertGreater(kwargs["confirmation_ms"], 0)


class SolDeltaTests(unittest.TestCase):
    @patch("rpc_client.rpc_call")
    def test_confirmed_sol_delta_uses_wallet_account(self, rpc_call):
        rpc_call.return_value = {
            "meta": {"err": None, "preBalances": [100, 200], "postBalances": [100, 260]},
            "transaction": {"message": {"accountKeys": ["other", {"pubkey": "wallet"}]}},
        }
        self.assertEqual(executor.get_confirmed_sol_delta("sig", "wallet"), 60)


if __name__ == "__main__":
    unittest.main()
