"""Tests for alert provenance storage and migrations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import feature_logger
import storage


class AlertSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = storage.DB_PATH
        storage.DB_PATH = Path(self.temp_dir.name) / "test.db"

    def tearDown(self):
        storage.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_outcome_source_is_persisted(self):
        storage.record_outcome(
            "token", "solana", "pair", "TOK", 0, 1.0, 1000, 2000,
            alert_source="pool",
        )
        rows = storage.get_outcomes_for_report(1)
        self.assertEqual(rows[0]["alert_source"], "pool")

    def test_outcome_calibration_evidence_is_persisted(self):
        storage.record_outcome(
            "token", "solana", "pair", "TOK", 72, 1.0, 1000, 2000,
            alert_source="scan",
            calibration={"probability": 0.31, "expectancy_pct": -8.2, "samples": 40},
        )
        row = storage.get_outcomes_for_report(1)[0]
        self.assertEqual(row["calibrated_probability"], 0.31)
        self.assertEqual(row["calibrated_expectancy"], -8.2)
        self.assertEqual(row["calibration_samples"], 40)

    def test_existing_outcomes_migrate_to_legacy(self):
        conn = sqlite3.connect(storage.DB_PATH)
        conn.execute("CREATE TABLE alert_outcomes (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        migrated = storage._connect()
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(alert_outcomes)")}
        migrated.close()
        self.assertIn("alert_source", columns)

    def test_feature_source_is_persisted(self):
        feature_logger.log_features(
            "token", "solana", "TOK",
            {"score": 72, "setup_score": 91, "entry_risk_penalty": 19, "breakdown": {}},
            {"priceUsd": "1"}, alert_source="pool",
        )
        conn = sqlite3.connect(storage.DB_PATH)
        row = conn.execute(
            "SELECT alert_source, score_total, score_setup, entry_risk_penalty FROM ml_features"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("pool", 72.0, 91.0, 19.0))


if __name__ == "__main__":
    unittest.main()
