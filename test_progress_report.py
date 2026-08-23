"""Regression tests for the prediction-readiness scorecard."""

import unittest
from unittest.mock import patch

import progress_report


def _metrics(**overrides):
    values = {
        "test_samples": 120, "predicted_trades": 35, "precision_pct": 55.0,
        "brier": 0.18, "expectancy_pct": 7.0, "max_drawdown_pct": -20.0,
        "test_start": 2_000.0, "test_end": 3_000.0,
    }
    values.update(overrides)
    return values


class ProgressReportTests(unittest.TestCase):
    @patch("progress_report.storage.get_execution_attempts_since", return_value=[])
    @patch("progress_report.storage.get_outcomes_for_report")
    @patch("progress_report.storage.get_model_evaluation_runs")
    @patch("progress_report.storage.save_model_evaluations")
    @patch("progress_report.ml_model.get_model_info")
    def test_candidate_requires_two_passing_forward_periods(
        self, model_info, _save, runs, outcomes, _attempts
    ):
        current = _metrics(test_start=2_000.0, test_end=3_000.0)
        previous = _metrics(precision_pct=52.0, test_start=1_000.0, test_end=2_000.0)
        model_info.return_value = {
            "status": "active", "trained_at": 10_000.0,
            "metrics": {"all": current, "scan": current},
        }
        runs.return_value = [
            {"trained_at": 10_000.0, "metrics": {"all": current}},
            {"trained_at": 9_000.0, "metrics": {"all": previous}},
        ]
        outcomes.return_value = [
            {"alerted_at": 1.0, "checked_1h": 1, "price_1h": 2.0, "price_at_alert": 1.0}
            for _ in range(100)
        ]

        report = progress_report.build(7, now=20_000.0)

        self.assertIn("State: CANDIDATE", report)
        self.assertIn("previous forward period: passed independently", report)
        self.assertIn("precision: 52.00 → 55.00 (+3.00)", report)
        self.assertIn("Scan: test 120", report)
        self.assertIn("Pool: insufficient model samples", report)

    @patch("progress_report.storage.get_execution_attempts_since", return_value=[])
    @patch("progress_report.storage.get_outcomes_for_report", return_value=[])
    @patch("progress_report.storage.get_model_evaluation_runs", return_value=[])
    @patch("progress_report.storage.save_model_evaluations")
    @patch("progress_report.ml_model.get_model_info")
    def test_single_passing_period_stays_shadow(self, model_info, _save, _runs, _rows, _attempts):
        model_info.return_value = {
            "status": "active", "trained_at": 10_000.0, "metrics": {"all": _metrics()}
        }
        report = progress_report.build(7, now=20_000.0)
        self.assertIn("State: SHADOW", report)
        self.assertIn("previous forward period: not available yet", report)

    @patch("progress_report.storage.get_execution_attempts_since", return_value=[])
    @patch("progress_report.storage.get_outcomes_for_report", return_value=[])
    @patch("progress_report.storage.get_model_evaluation_runs")
    @patch("progress_report.storage.save_model_evaluations")
    @patch("progress_report.ml_model.get_model_info")
    def test_overlapping_passing_runs_stay_shadow(self, model_info, _save, runs, _rows, _attempts):
        current = _metrics(test_start=2_000.0, test_end=3_000.0)
        previous = _metrics(test_start=1_500.0, test_end=2_500.0)
        model_info.return_value = {
            "status": "active", "trained_at": 10_000.0, "metrics": {"all": current}
        }
        runs.return_value = [
            {"trained_at": 10_000.0, "metrics": {"all": current}},
            {"trained_at": 9_000.0, "metrics": {"all": previous}},
        ]
        report = progress_report.build(7, now=20_000.0)
        self.assertIn("State: SHADOW", report)
        self.assertIn("previous forward period: overlaps current test", report)

    @patch("progress_report.ml_model.get_model_info")
    def test_untrained_model_is_collecting(self, model_info):
        model_info.return_value = {"status": "not_trained", "labeled_samples": 80, "needed": 200}
        report = progress_report.build()
        self.assertIn("State: COLLECTING", report)


if __name__ == "__main__":
    unittest.main()
