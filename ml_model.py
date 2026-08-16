"""Chronological shadow models for P(return >= +10% at one hour)."""

import logging
import os
import pickle
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODEL_PATH = Path(os.getenv("DB_PATH", "alerts.db")).parent / "ml_model.pkl"
ARTIFACT_VERSION = 2
MIN_SAMPLES = 200
MIN_SOURCE_SAMPLES = 100
MIN_POSITIVES = 15
RETRAIN_INTERVAL = 86400
TARGET_RETURN_PCT = 10.0

_models: dict[str, dict[str, Any]] = {}
_metadata: dict[str, Any] = {}
_model_loaded_at = 0.0
_last_train_attempt = 0.0

FEATURES = [
    "score_total", "score_setup", "entry_risk_penalty",
    "score_liquidity", "score_market_cap", "score_pair_age",
    "score_vol_liq", "score_price_change", "score_buy_sell", "score_velocity",
    "liquidity_usd", "market_cap", "volume_24h", "volume_1h",
    "price_change_5m", "price_change_1h", "price_change_6h",
    "txns_1h_buys", "txns_1h_sells", "pair_age_hours", "vol_liq_ratio",
    "buy_sell_ratio_1h", "rugcheck_score", "lp_locked_pct", "risk_count",
    "hour_utc", "day_of_week", "is_us_hours",
]


def _to_row(record: dict) -> list[float]:
    aliases = {
        "score_liquidity": "liquidity", "score_market_cap": "market_cap",
        "score_pair_age": "pair_age", "score_vol_liq": "vol_liq_ratio",
        "score_price_change": "price_change", "score_buy_sell": "buy_sell_ratio",
        "score_velocity": "velocity", "score_setup": "setup_score",
    }
    values = []
    for feature in FEATURES:
        value = record.get(feature)
        if value is None and feature in aliases:
            value = record.get(aliases[feature])
        values.append(float(value or 0))
    return values


def _realized_return(row: dict) -> float | None:
    if row.get("rugged") and row.get("rug_verified"):
        return -100.0
    entry = float(row.get("price_usd") or 0)
    after = float(row.get("price_1h") or 0)
    if entry <= 0 or after <= 0:
        return None
    return (after - entry) / entry * 100


def _training_rows() -> list[dict]:
    import feature_logger
    rows = []
    for row in feature_logger.export_training_data():
        if row.get("outcome_label") == "invalid":
            continue
        realized = _realized_return(row)
        if realized is None:
            continue
        item = dict(row)
        item["realized_return_1h"] = realized
        item["target_1h"] = int(realized >= TARGET_RETURN_PCT)
        rows.append(item)
    return sorted(rows, key=lambda row: float(row.get("alerted_at") or 0))


def _chronological_split(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Oldest 60% train, next 20% calibration, newest 20% untouched test."""
    train_end = max(1, int(len(rows) * 0.60))
    calibration_end = max(train_end + 1, int(len(rows) * 0.80))
    calibration_end = min(calibration_end, len(rows) - 1)
    return rows[:train_end], rows[train_end:calibration_end], rows[calibration_end:]


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= max(0.0, 1 + value / 100)
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity - peak) / peak * 100)
    return round(worst, 1)


def _fit_cohort(rows: list[dict], name: str) -> dict[str, Any] | None:
    import numpy as np
    import xgboost as xgb
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import accuracy_score, brier_score_loss, precision_score, recall_score

    train_rows, calibration_rows, test_rows = _chronological_split(rows)
    if min(len(train_rows), len(calibration_rows), len(test_rows)) < 10:
        return None
    y_train = np.array([row["target_1h"] for row in train_rows])
    if len(set(y_train)) < 2 or int(y_train.sum()) < MIN_POSITIVES:
        logger.info("[ML] %s cohort lacks positive training examples", name)
        return None

    def matrix(items):
        return np.nan_to_num(np.array([_to_row(row) for row in items], dtype=float),
                             nan=0.0, posinf=0.0, neginf=0.0)

    negative_count = len(y_train) - int(y_train.sum())
    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        scale_pos_weight=negative_count / max(1, int(y_train.sum())),
        eval_metric="logloss", random_state=42, verbosity=0,
    )
    model.fit(matrix(train_rows), y_train)

    raw_calibration = model.predict_proba(matrix(calibration_rows))[:, 1]
    y_calibration = np.array([row["target_1h"] for row in calibration_rows])
    calibrator = None
    if len(set(y_calibration)) == 2:
        calibrator = IsotonicRegression(out_of_bounds="clip")
        calibrator.fit(raw_calibration, y_calibration)

    raw_test = model.predict_proba(matrix(test_rows))[:, 1]
    probabilities = calibrator.predict(raw_test) if calibrator is not None else raw_test
    y_test = np.array([row["target_1h"] for row in test_rows])
    predictions = (probabilities >= 0.5).astype(int)
    import config
    selected_returns = [float(row["realized_return_1h"]) - config.ML_ESTIMATED_TRADING_COST_PCT
                        for row, selected in zip(test_rows, predictions) if selected]
    metrics = {
        "samples": len(rows), "train_samples": len(train_rows),
        "calibration_samples": len(calibration_rows), "test_samples": len(test_rows),
        "positive_rate_pct": round(sum(row["target_1h"] for row in rows) / len(rows) * 100, 1),
        "accuracy_pct": round(accuracy_score(y_test, predictions) * 100, 1),
        "precision_pct": round(precision_score(y_test, predictions, zero_division=0) * 100, 1),
        "recall_pct": round(recall_score(y_test, predictions, zero_division=0) * 100, 1),
        "brier": round(brier_score_loss(y_test, probabilities), 4),
        "predicted_trades": len(selected_returns),
        "expectancy_pct": round(sum(selected_returns) / len(selected_returns), 1) if selected_returns else None,
        "max_drawdown_pct": _max_drawdown(selected_returns) if selected_returns else None,
        "estimated_cost_pct": config.ML_ESTIMATED_TRADING_COST_PCT,
        "test_start": float(test_rows[0].get("alerted_at") or 0),
        "test_end": float(test_rows[-1].get("alerted_at") or 0),
    }
    return {"model": model, "calibrator": calibrator, "metrics": metrics}


def train() -> dict:
    """Train global and eligible source models using untouched chronological tests."""
    global _models, _metadata, _model_loaded_at, _last_train_attempt
    _last_train_attempt = time.time()
    try:
        rows = _training_rows()
        if len(rows) < MIN_SAMPLES:
            return {"status": "insufficient_data", "samples": len(rows), "needed": MIN_SAMPLES}

        cohorts: dict[str, list[dict]] = {"all": rows}
        for source in ("scan", "pool", "wallet"):
            source_rows = [row for row in rows if (row.get("alert_source") or "legacy") == source]
            if len(source_rows) >= MIN_SOURCE_SAMPLES:
                cohorts[source] = source_rows
        chains = sorted({(row.get("chain_id") or "unknown").lower() for row in rows})
        for chain in chains:
            chain_rows = [row for row in rows if (row.get("chain_id") or "unknown").lower() == chain]
            if len(chain_rows) >= MIN_SOURCE_SAMPLES:
                cohorts[chain] = chain_rows
            for source in ("scan", "pool", "wallet"):
                cohort = [row for row in chain_rows if (row.get("alert_source") or "legacy") == source]
                if len(cohort) >= MIN_SOURCE_SAMPLES:
                    cohorts[f"{chain}:{source}"] = cohort

        fitted = {name: result for name, cohort in cohorts.items()
                  if (result := _fit_cohort(cohort, name)) is not None}
        if "all" not in fitted:
            return {"status": "degenerate_dataset", "samples": len(rows)}

        trained_at = time.time()
        metadata = {
            "artifact_version": ARTIFACT_VERSION, "trained_at": trained_at,
            "target": f"return_1h >= {TARGET_RETURN_PCT:.0f}%", "shadow_mode": True,
            "metrics": {name: item["metrics"] for name, item in fitted.items()},
        }
        with open(MODEL_PATH, "wb") as file:
            pickle.dump({"artifact_version": ARTIFACT_VERSION, "models": fitted,
                         "metadata": metadata}, file)
        _models = fitted
        _metadata = metadata
        _model_loaded_at = trained_at
        logger.info("[ML] Shadow models trained: %s", ", ".join(fitted))
        return {"status": "trained", "models": list(fitted), **metadata}
    except ImportError:
        logger.exception("[ML] Training dependencies unavailable")
        return {"status": "missing_deps"}
    except Exception as exc:
        logger.exception("[ML] Training failed")
        return {"status": "failed", "error": str(exc)}


def load_model() -> bool:
    global _models, _metadata, _model_loaded_at
    if not MODEL_PATH.exists():
        return False
    try:
        with open(MODEL_PATH, "rb") as file:
            artifact = pickle.load(file)
        if artifact.get("artifact_version") != ARTIFACT_VERSION:
            logger.warning("[ML] Ignoring legacy model artifact; chronological retraining required")
            return False
        _models = artifact["models"]
        _metadata = artifact["metadata"]
        _model_loaded_at = float(_metadata.get("trained_at") or 0)
        return "all" in _models
    except Exception:
        logger.exception("[ML] Failed to load model artifact")
        return False


def predict_pump(features: dict, alert_source: str | None = None) -> float | None:
    """Predict calibrated P(+10% at 1h), using a source model when available."""
    if not _models and not load_model():
        return None
    try:
        import numpy as np
        source = alert_source or features.get("alert_source") or "scan"
        chain = (features.get("chain_id") or "").lower()
        artifact = (_models.get(f"{chain}:{source}") or _models.get(chain)
                    or _models.get(source) or _models["all"])
        row = np.nan_to_num(np.array([_to_row(features)], dtype=float),
                            nan=0.0, posinf=0.0, neginf=0.0)
        raw = float(artifact["model"].predict_proba(row)[0][1])
        calibrator = artifact.get("calibrator")
        probability = float(calibrator.predict([raw])[0]) if calibrator is not None else raw
        return round(max(0.0, min(1.0, probability)), 3)
    except Exception:
        logger.exception("[ML] Prediction failed")
        return None


def should_retrain() -> bool:
    global _last_train_attempt
    if time.time() - _last_train_attempt < 3600:
        return False
    if _models:
        return time.time() - float(_metadata.get("trained_at") or 0) > RETRAIN_INTERVAL
    if load_model():
        return time.time() - float(_metadata.get("trained_at") or 0) > RETRAIN_INTERVAL
    return len(_training_rows()) >= MIN_SAMPLES


def get_model_info() -> dict:
    if not _models:
        load_model()
    if not _models:
        samples = len(_training_rows())
        return {"status": "not_trained", "labeled_samples": samples, "needed": MIN_SAMPLES,
                "progress_pct": min(100, round(samples / MIN_SAMPLES * 100)),
                "shadow_mode": True}
    return {"status": "active", **_metadata, "models": list(_models)}
