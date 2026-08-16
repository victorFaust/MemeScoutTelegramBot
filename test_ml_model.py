"""Regression tests for chronological shadow-model evaluation."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import ml_model
import bot_handler


def _row(index: int, return_pct: float = 20, source: str = "scan"):
    entry = 1.0
    return {
        "alerted_at": float(index), "alert_source": source, "price_usd": entry,
        "price_1h": entry * (1 + return_pct / 100), "rugged": 0,
        "outcome_label": "pump" if return_pct >= 10 else "dump",
        "score_total": 60 + index % 20,
    }


class TargetTests(unittest.TestCase):
    def test_target_is_explicit_ten_percent_one_hour_return(self):
        self.assertAlmostEqual(ml_model._realized_return(_row(1, 9)), 9)
        self.assertAlmostEqual(ml_model._realized_return(_row(1, 10)), 10)

    def test_confirmed_rug_is_retained_as_negative_return(self):
        row = _row(1, 50)
        row.update(rugged=1, rug_verified=1, price_1h=None, outcome_label="rug")
        self.assertEqual(ml_model._realized_return(row), -100)

    def test_legacy_rug_without_price_is_quarantined(self):
        row = _row(1, 50)
        row.update(rugged=1, rug_verified=0, price_1h=None, outcome_label="rug")
        self.assertIsNone(ml_model._realized_return(row))

    def test_live_breakdown_aliases_map_to_training_features(self):
        record = {"liquidity": 0.7, "market_cap": 0.6, "pair_age": 0.5,
                  "price_change": 0.4, "buy_sell_ratio": 0.3, "velocity": 0.2}
        values = dict(zip(ml_model.FEATURES, ml_model._to_row(record)))
        self.assertEqual(values["score_liquidity"], 0.7)
        self.assertEqual(values["score_market_cap"], 0.6)
        self.assertEqual(values["score_velocity"], 0.2)


class ChronologicalSplitTests(unittest.TestCase):
    def test_newest_twenty_percent_is_untouched_test_set(self):
        rows = [_row(index) for index in range(100)]
        train, calibration, test = ml_model._chronological_split(rows)
        self.assertEqual((len(train), len(calibration), len(test)), (60, 20, 20))
        self.assertEqual(train[-1]["alerted_at"], 59)
        self.assertEqual(calibration[0]["alerted_at"], 60)
        self.assertEqual(test[0]["alerted_at"], 80)

    def test_drawdown_uses_prediction_order(self):
        self.assertEqual(ml_model._max_drawdown([10, -50, 10]), -50.0)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = ml_model.MODEL_PATH
        self.original_models = ml_model._models
        self.original_metadata = ml_model._metadata
        ml_model.MODEL_PATH = Path(self.temp_dir.name) / "model.pkl"
        ml_model._models = {}
        ml_model._metadata = {}

    def tearDown(self):
        ml_model.MODEL_PATH = self.original_path
        ml_model._models = self.original_models
        ml_model._metadata = self.original_metadata
        self.temp_dir.cleanup()

    def test_legacy_artifact_is_not_loaded(self):
        import pickle
        with open(ml_model.MODEL_PATH, "wb") as file:
            pickle.dump({"model": "legacy", "accuracy": 1.0}, file)
        self.assertFalse(ml_model.load_model())

    @patch("feature_logger.export_training_data")
    def test_training_rows_are_sorted_and_use_explicit_target(self, export):
        export.return_value = [_row(3, -20), _row(1, 20), _row(2, 10)]
        rows = ml_model._training_rows()
        self.assertEqual([row["alerted_at"] for row in rows], [1, 2, 3])
        self.assertEqual([row["target_1h"] for row in rows], [1, 1, 0])


class ModelReportTests(unittest.TestCase):
    @patch("ml_model.get_model_info")
    def test_shadow_report_exposes_forward_metrics(self, info):
        info.return_value = {
            "status": "active", "target": "return_1h >= 10%", "metrics": {
                "all": {"samples": 300, "test_samples": 60, "precision_pct": 55,
                        "recall_pct": 40, "brier": 0.18, "predicted_trades": 10,
                        "expectancy_pct": 7.2, "max_drawdown_pct": -12.0}
            },
        }
        report = bot_handler._model_report()
        self.assertIn("SHADOW MODE", report)
        self.assertIn("Precision 55.0%", report)
        self.assertIn("Brier 0.1800", report)
        self.assertIn("net expectancy +7.2%", report)


if __name__ == "__main__":
    unittest.main()
