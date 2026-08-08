"""XGBoost model for predicting token pump probability.

Trains on labeled feature data from feature_logger.
Scores new tokens at alert time to filter low-probability ones.
Model auto-trains when 200+ labeled samples are available.
"""

import logging
import os
import pickle
import time
from pathlib import Path

import storage

logger = logging.getLogger(__name__)

MODEL_PATH = Path(os.getenv("DB_PATH", "alerts.db")).parent / "ml_model.pkl"
MIN_SAMPLES = 200
RETRAIN_INTERVAL = 86400  # retrain daily

# Cached model and metadata
_model = None
_model_loaded_at: float = 0
_model_accuracy: float = 0
_last_train_attempt: float = 0

FEATURES = [
    "score_total", "score_liquidity", "score_market_cap", "score_pair_age",
    "score_vol_liq", "score_price_change", "score_buy_sell", "score_velocity",
    "liquidity_usd", "market_cap", "volume_24h", "volume_1h",
    "price_change_5m", "price_change_1h", "price_change_6h",
    "txns_1h_buys", "txns_1h_sells",
    "pair_age_hours", "vol_liq_ratio", "buy_sell_ratio_1h",
    "rugcheck_score", "lp_locked_pct", "risk_count",
    "hour_utc", "day_of_week", "is_us_hours",
]


def _to_row(record: dict) -> list:
    """Convert a feature record dict to a model input row."""
    return [float(record.get(f) or 0) for f in FEATURES]


def train() -> dict:
    """Train (or retrain) the XGBoost model on current labeled data.

    Returns stats dict: {samples, accuracy, trained_at, status}
    """
    global _model, _model_loaded_at, _model_accuracy, _last_train_attempt
    _last_train_attempt = time.time()

    try:
        import xgboost as xgb
        from sklearn.model_selection import train_test_split
        from sklearn.metrics import accuracy_score
        import numpy as np
    except ImportError:
        logger.error("[ML] xgboost or sklearn not installed -- skipping training")
        return {"status": "missing_deps"}

    import feature_logger
    rows = feature_logger.export_training_data()

    if len(rows) < MIN_SAMPLES:
        logger.info("[ML] Only %d labeled samples, need %d -- skipping training", len(rows), MIN_SAMPLES)
        return {"status": "insufficient_data", "samples": len(rows)}

    # Binary target: pump/moon = 1, else = 0
    # Exclude rug-only samples for training — keep dump/neutral as negative class
    rows = [r for r in rows if r.get("outcome_label") != "rug"]
    if len(rows) < MIN_SAMPLES:
        logger.info("[ML] Only %d non-rug samples after filtering, need %d -- skipping", len(rows), MIN_SAMPLES)
        return {"status": "insufficient_data", "samples": len(rows)}

    X = [_to_row(r) for r in rows]
    y = [1 if r.get("outcome_label") in ("moon", "pump") else 0 for r in rows]

    import numpy as np
    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y)

    # Reject degenerate dataset (all one class)
    if len(set(y_arr)) < 2:
        logger.warning("[ML] Dataset is single-class (%s only) -- cannot train useful model", rows[0].get("outcome_label"))
        return {"status": "degenerate_dataset", "samples": len(rows)}

    # Replace NaN/inf
    X_arr = np.nan_to_num(X_arr, nan=0.0, posinf=0.0, neginf=0.0)

    X_train, X_test, y_train, y_test = train_test_split(X_arr, y_arr, test_size=0.2, random_state=42)

    pos_count = sum(y_arr)
    neg_count = len(y_arr) - pos_count
    scale_pos = neg_count / pos_count if pos_count > 0 else 1.0

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        scale_pos_weight=scale_pos,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=42,
        verbosity=0,
    )
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    accuracy = accuracy_score(y_test, y_pred)

    # Save model
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": model, "accuracy": accuracy, "trained_at": time.time()}, f)

    _model = model
    _model_loaded_at = time.time()
    _model_accuracy = accuracy

    pump_rate = pos_count / len(y_arr) * 100
    logger.info("[ML] Model trained: %d samples, %.1f%% accuracy, %.1f%% pump rate",
                len(rows), accuracy * 100, pump_rate)

    return {
        "status": "trained",
        "samples": len(rows),
        "accuracy": round(accuracy * 100, 1),
        "pump_rate": round(pump_rate, 1),
        "trained_at": time.time(),
    }


def load_model() -> bool:
    """Load saved model from disk. Returns True if loaded."""
    global _model, _model_loaded_at, _model_accuracy

    if not MODEL_PATH.exists():
        return False

    try:
        with open(MODEL_PATH, "rb") as f:
            data = pickle.load(f)
        
        # Reject stale models trained on degenerate data (100% accuracy = all-one-class)
        acc = data.get("accuracy", 0)
        pump_rate = data.get("pump_rate", None)
        if acc >= 0.99 and (pump_rate is None or pump_rate == 0):
            logger.warning("[ML] Discarding stale model (100%% accuracy, 0%% pump rate -- trained on rug-only data)")
            MODEL_PATH.unlink(missing_ok=True)
            return False

        _model = data["model"]
        _model_accuracy = acc
        _model_loaded_at = data.get("trained_at", 0)
        logger.info("[ML] Model loaded from disk (accuracy=%.1f%%)", _model_accuracy * 100)
        return True
    except Exception as e:
        logger.warning("[ML] Failed to load model: %s", e)
        return False


def predict_pump(features: dict) -> float | None:
    """Predict pump probability for a token (0.0 – 1.0).

    Returns None if no model is available.
    """
    global _model, _model_loaded_at

    if _model is None:
        # Try loading from disk first
        if not load_model():
            return None

    try:
        import numpy as np
        row = np.array([_to_row(features)], dtype=float)
        row = np.nan_to_num(row, nan=0.0, posinf=0.0, neginf=0.0)
        prob = float(_model.predict_proba(row)[0][1])
        return round(prob, 3)
    except Exception as e:
        logger.warning("[ML] Prediction failed: %s", e)
        return None


def should_retrain() -> bool:
    """True if model should be retrained (first time or stale)."""
    if _model is None and not MODEL_PATH.exists():
        import feature_logger
        stats = feature_logger.get_feature_stats()
        return stats.get("labeled", 0) >= MIN_SAMPLES
    if time.time() - _last_train_attempt > RETRAIN_INTERVAL:
        return True
    return False


def get_model_info() -> dict:
    """Get current model status for /report display."""
    if _model is None and not MODEL_PATH.exists():
        import feature_logger
        stats = feature_logger.get_feature_stats()
        labeled = stats.get("labeled", 0)
        return {
            "status": "not_trained",
            "labeled_samples": labeled,
            "needed": MIN_SAMPLES,
            "progress_pct": round(labeled / MIN_SAMPLES * 100),
        }

    return {
        "status": "active",
        "accuracy": round(_model_accuracy * 100, 1),
        "trained_at": _model_loaded_at,
    }
