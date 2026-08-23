"""Regression tests for outcome tracking safety."""

import unittest
from unittest.mock import patch

import performance_tracker as tracker
import holder_analysis
from bot_handler import _outcome_coverage


class RugStatusTests(unittest.TestCase):
    def setUp(self):
        tracker._rug_miss_counts.clear()

    @patch("performance_tracker.dex.get_pair", return_value=None)
    def test_api_failure_is_inconclusive(self, _get_pair):
        self.assertIsNone(tracker._rug_status("solana", "pair"))
        self.assertNotIn("solana:pair", tracker._rug_miss_counts)

    @patch("performance_tracker.dex.get_pair", return_value=[])
    def test_rug_requires_three_confirmations(self, _get_pair):
        self.assertIsNone(tracker._rug_status("solana", "pair"))
        self.assertIsNone(tracker._rug_status("solana", "pair"))
        self.assertIs(tracker._rug_status("solana", "pair"), True)

    @patch("performance_tracker.dex.get_pair")
    def test_liquidity_resets_miss_counter(self, get_pair):
        get_pair.side_effect = [[], [{"liquidity": {"usd": 5000}}], []]
        self.assertIsNone(tracker._rug_status("solana", "pair"))
        self.assertIs(tracker._rug_status("solana", "pair"), False)
        self.assertIsNone(tracker._rug_status("solana", "pair"))


class SnapshotTimingTests(unittest.TestCase):
    def test_report_coverage_only_counts_mature_alerts_with_valid_prices(self):
        rows = [
            {"alerted_at": 1_000.0, "checked_1h": 1, "price_1h": 2.0, "price_at_alert": 1.0},
            {"alerted_at": 1_000.0, "checked_1h": 1, "price_1h": None, "price_at_alert": 1.0},
            {"alerted_at": 9_500.0, "checked_1h": 0, "price_1h": None, "price_at_alert": 1.0},
        ]
        self.assertEqual(_outcome_coverage(rows, "1h", 10_000.0), (1, 2))

    @patch("performance_tracker.time.sleep")
    @patch("performance_tracker._get_current_price", return_value=2.0)
    @patch("performance_tracker._rug_status", return_value=False)
    @patch("performance_tracker.storage.update_snapshot")
    @patch("performance_tracker.storage.mark_snapshot_missed")
    @patch("performance_tracker.storage.get_pending_snapshots")
    @patch("performance_tracker.time.time", return_value=100_000.0)
    def test_expired_windows_are_not_backfilled(
        self, _now, get_pending, mark_missed, update_snapshot, _rug, _price, _sleep
    ):
        get_pending.return_value = [{
            "id": 1, "alerted_at": 100_000.0 - 7 * 3600, "chain_id": "solana",
            "pair_address": "pair", "token_address": "mint", "token_symbol": "OLD",
            "price_at_alert": 1.0, "max_price_24h": None, "rugged": 0,
            "checked_15m": 0, "checked_1h": 0, "checked_6h": 0, "checked_24h": 0,
        }]

        stats = tracker.run_snapshot_check()

        self.assertEqual(stats["missed"], 2)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(
            {call.args[1] for call in mark_missed.call_args_list}, {"15m", "1h"}
        )
        self.assertEqual(update_snapshot.call_args.args[1], "6h")


class HolderFailureTests(unittest.TestCase):
    @patch("holder_analysis._rpc_call")
    def test_failed_transaction_lookups_are_not_wash_trading(self, rpc_call):
        rpc_call.side_effect = [
            [{"signature": f"sig{i}"} for i in range(10)],
            *([None] * 10),
        ]
        result = holder_analysis.get_unique_buyers_recent("mint", limit=10)
        self.assertEqual(result["tx_count_checked"], 0)
        self.assertEqual(result["total_txns"], 0)


if __name__ == "__main__":
    unittest.main()
