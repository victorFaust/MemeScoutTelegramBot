"""Regression tests for durable trade orders."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import order_engine
import storage


class TradeOrderStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "orders.db"

    def tearDown(self):
        storage.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_claim_is_atomic_and_completed_order_stays_terminal(self):
        order_id = storage.create_trade_order("mint", "limit_buy", 0.001, amount_sol=0.1)
        self.assertTrue(storage.claim_trade_order(order_id))
        self.assertFalse(storage.claim_trade_order(order_id))
        storage.complete_trade_order(order_id, "sig")
        order = storage.get_trade_orders()[0]
        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["signature"], "sig")

    def test_default_exit_orders_are_idempotent(self):
        storage.record_position("mint", "solana", 1, 100, "sig", token_symbol="TOK")
        position = storage.get_open_positions()[0]
        order_engine.create_default_exit_orders(position)
        order_engine.create_default_exit_orders(position)
        self.assertEqual(len(storage.get_active_trade_orders()), 3)

    def test_interrupted_order_is_recovered(self):
        order_id = storage.create_trade_order("mint", "limit_buy", 0.001, amount_sol=0.1)
        self.assertTrue(storage.claim_trade_order(order_id))
        self.assertEqual(storage.recover_stale_trade_orders(stale_seconds=0), 1)
        self.assertEqual(storage.get_trade_orders()[0]["status"], "active")


class TradeOrderEngineTests(unittest.TestCase):
    @patch("order_engine.executor.buy_token", return_value={"signature": "sig"})
    @patch("order_engine._current_price", return_value=0.0009)
    @patch("order_engine.storage")
    def test_triggered_limit_buy_is_claimed_and_completed(self, db, _price, buy):
        order = {
            "id": 7, "position_id": None, "token_address": "mint", "token_symbol": "TOK",
            "order_type": "limit_buy", "trigger_value": 0.001, "amount_sol": 0.1,
            "amount_pct": None,
        }
        db.get_active_trade_orders.return_value = [order]
        db.claim_trade_order.return_value = True
        events = order_engine.process_orders_once()
        buy.assert_called_once_with("mint", 0.1)
        db.complete_trade_order.assert_called_once_with(7, "sig")
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
