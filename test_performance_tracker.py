"""Regression tests for outcome tracking safety."""

import unittest
from unittest.mock import patch

import performance_tracker as tracker
import holder_analysis


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
