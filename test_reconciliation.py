"""Regression tests for transaction and on-chain position reconciliation."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import executor
import main
import storage


class ReconciliationStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "reconciliation.db"
        storage.record_position("mint", "solana", 1.0, 100, "sig", wallet_address="wallet")
        self.position_id = storage.get_open_positions()[0]["id"]

    def tearDown(self):
        storage.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_unconfirmed_positions_remain_in_confirmation_queue(self):
        storage.update_tx_status(self.position_id, "unconfirmed")
        ids = [p["id"] for p in storage.get_positions_needing_confirmation()]
        self.assertIn(self.position_id, ids)

    def test_confirmed_delta_can_correct_estimated_amount(self):
        storage.reconcile_position(self.position_id, 125, "confirmed", update_token_amount=True)
        position = storage.get_position_by_id(self.position_id)
        self.assertEqual(position["token_amount"], 125)
        self.assertEqual(position["onchain_token_amount"], 125)
        self.assertEqual(position["reconciliation_status"], "confirmed")


class RpcReconciliationTests(unittest.TestCase):
    @patch("rpc_client.rpc_call")
    def test_token_balance_sums_all_owned_accounts(self, rpc_call):
        rpc_call.return_value = {"value": [
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "10"}}}}}},
            {"account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "15"}}}}}},
        ]}
        self.assertEqual(executor.get_token_balance("wallet", "mint"), 25)

    @patch("rpc_client.rpc_call")
    def test_confirmed_token_delta_uses_owner_and_mint(self, rpc_call):
        rpc_call.return_value = {"meta": {
            "err": None,
            "preTokenBalances": [{"owner": "wallet", "mint": "mint", "uiTokenAmount": {"amount": "5"}}],
            "postTokenBalances": [{"owner": "wallet", "mint": "mint", "uiTokenAmount": {"amount": "25"}}],
        }}
        self.assertEqual(executor.get_confirmed_token_delta("sig", "wallet", "mint"), 20)


class OpenPositionReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_mismatch_is_recorded_not_silently_overwritten(self):
        position = {
            "id": 1, "wallet_address": "wallet", "token_address": "mint",
            "token_amount": 100, "tx_status": "confirmed",
        }
        with patch("main.storage.get_open_positions", return_value=[position]), \
             patch("executor.get_token_balance", return_value=80), \
             patch("main.storage.reconcile_position") as reconcile:
            await main._reconcile_open_positions(force=True)
        reconcile.assert_called_once_with(1, 80, "balance_mismatch")


if __name__ == "__main__":
    unittest.main()
