"""Regression tests for entry-risk penalties and empirical score calibration."""

import unittest
from unittest.mock import patch

import filters
import score_calibration
import telegram_notifier


def _feature(source="wallet", score=70, entry=1.0, after=1.2, **overrides):
    row = {
        "alert_source": source, "chain_id": "solana", "score_total": score, "alerted_at": 9_999_999_999,
        "price_usd": entry, "price_1h": after, "rugged": 0,
        "price_change_5m": 0, "price_change_1h": 10, "price_change_6h": 20,
        "volume_24h": 100_000, "liquidity_usd": 20_000,
        "txns_1h_buys": 40, "txns_1h_sells": 30,
    }
    row.update(overrides)
    return row


class EntryRiskPenaltyTests(unittest.TestCase):
    def test_overextended_reversing_pair_loses_score(self):
        pair = {
            "chainId": "solana", "pairCreatedAt": 9_999_999_999_999,
            "liquidity": {"usd": 20_000}, "marketCap": 100_000,
            "volume": {"h24": 400_000},
            "priceChange": {"m5": -10, "h1": 180, "h6": 350},
            "txns": {"h1": {"buys": 100, "sells": 20}},
            "baseToken": {"address": "mint"},
        }
        with patch("filters._get_time_of_day_boost", return_value=1.0), \
             patch("storage.get_previous_metrics", return_value=None):
            result = filters.score_pair(pair)
        self.assertGreater(result["entry_risk_penalty"], 0)
        self.assertLess(result["score"], result["setup_score"])
        self.assertTrue(any("overextended" in reason for reason in result["penalty_reasons"]))
        self.assertIn("momentum already reversing", result["penalty_reasons"])

    def test_normal_momentum_has_no_penalty(self):
        pair = {
            "priceChange": {"m5": 5, "h1": 20, "h6": 30},
            "volume": {"h24": 100_000}, "liquidity": {"usd": 20_000},
            "txns": {"h1": {"buys": 40, "sells": 30}},
        }
        penalty, reasons = filters._entry_risk_penalty(pair)
        self.assertEqual(penalty, 0)
        self.assertEqual(reasons, [])


class CalibrationTests(unittest.TestCase):
    def tearDown(self):
        score_calibration.clear_cache()

    @patch("feature_logger.export_training_data")
    def test_positive_wallet_cohort_becomes_trade_eligible(self, export):
        export.return_value = [_feature() for _ in range(25)]
        score_calibration.clear_cache()
        result = score_calibration.calibrate("wallet", 70)
        self.assertEqual(result["samples"], 25)
        self.assertTrue(result["eligible"])
        self.assertGreaterEqual(result["probability"], 0.45)
        self.assertGreaterEqual(result["expectancy_pct"], 5)

    @patch("feature_logger.export_training_data")
    def test_negative_wallet_cohort_blocks_auto_buy(self, export):
        export.return_value = [_feature(after=0.7) for _ in range(25)]
        score_calibration.clear_cache()
        result = score_calibration.calibrate("wallet", 70)
        self.assertFalse(result["eligible"])
        self.assertLess(result["expectancy_pct"], 0)

    @patch("feature_logger.export_training_data")
    def test_scan_buckets_use_penalized_historical_score(self, export):
        export.return_value = [
            _feature(source="scan", score=91, price_change_5m=60, after=1.2)
            for _ in range(25)
        ]
        score_calibration.clear_cache()
        result = score_calibration.calibrate("scan", 75)
        self.assertEqual(result["samples"], 25)

    def test_new_feature_rows_do_not_apply_entry_penalty_twice(self):
        row = _feature(source="scan", score=72, score_setup=91, entry_risk_penalty=19,
                       price_change_5m=60)
        self.assertEqual(score_calibration._historical_entry_score(row), 72)


class AlertRenderingTests(unittest.TestCase):
    def test_alert_distinguishes_setup_score_from_historical_probability(self):
        result = {
            "score": 72, "setup_score": 91, "entry_risk_penalty": 19,
            "penalty_reasons": ["5m overextended +60%"],
            "calibration": {"probability": 0.31, "expectancy_pct": -8.2,
                            "samples": 40, "eligible": False},
            "pair": {"chainId": "solana", "baseToken": {"name": "Token", "symbol": "TOK"},
                     "liquidity": {"usd": 20_000}, "marketCap": 100_000,
                     "volume": {"h24": 50_000}, "priceChange": {}, "txns": {"h1": {}}},
        }
        message = telegram_notifier.build_message(result)
        self.assertIn("setup 91, penalty -19", message)
        self.assertIn("Historical 1h win: *31%*", message)
        self.assertIn("insufficient/negative evidence", message)


if __name__ == "__main__":
    unittest.main()
