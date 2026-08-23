"""Prediction promotion scorecard built from forward and operational evidence."""

import time

import ml_model
import storage

MODEL_GATES = (
    ("test_samples", ">=", 100, "test samples"),
    ("predicted_trades", ">=", 30, "predicted trades"),
    ("precision_pct", ">=", 50.0, "precision"),
    ("brier", "<", 0.20, "Brier"),
    ("expectancy_pct", ">", 5.0, "net expectancy"),
    ("max_drawdown_pct", ">", -25.0, "max drawdown"),
)


def _passes(metrics: dict, field: str, operator: str, threshold: float) -> bool:
    value = metrics.get(field)
    if value is None:
        return False
    return value >= threshold if operator == ">=" else value > threshold if operator == ">" else value < threshold


def _all_model_gates(metrics: dict | None) -> bool:
    return bool(metrics) and all(_passes(metrics, field, op, target)
                                 for field, op, target, _ in MODEL_GATES)


def _independent_previous_period(current: dict, previous: dict | None) -> bool:
    """Require the older test set to finish before the current test set starts."""
    if not previous or not _all_model_gates(previous):
        return False
    current_start = float(current.get("test_start") or 0)
    previous_end = float(previous.get("test_end") or 0)
    return current_start > 0 and previous_end > 0 and previous_end <= current_start


def _coverage(days: int, now: float) -> tuple[int, int, float]:
    rows = storage.get_outcomes_for_report(days)
    matured = [row for row in rows if now - float(row.get("alerted_at") or now) >= 3600]
    valid = [row for row in matured if row.get("checked_1h") and row.get("price_1h")
             and row.get("price_at_alert")]
    pct = len(valid) / len(matured) * 100 if matured else 0.0
    return len(valid), len(matured), pct


def _execution(days: int, now: float) -> dict:
    rows = storage.get_execution_attempts_since(now - days * 86400)
    confirmed = [row for row in rows if row.get("status") in {"confirmed", "finalized"}]
    failed = [row for row in rows if row.get("status") in {"failed", "dropped", "blocked"}]
    submitted = [row for row in rows if row.get("signature")]
    slipped = []
    for row in confirmed:
        expected, realized = int(row.get("expected_out") or 0), row.get("realized_out")
        if expected > 0 and realized is not None:
            slipped.append((int(realized) - expected) / expected * 100)
    return {
        "attempts": len(rows),
        "confirmation_pct": len(confirmed) / len(submitted) * 100 if submitted else None,
        "failure_pct": len(failed) / len(rows) * 100 if rows else None,
        "dropped_pct": sum(row.get("status") == "dropped" for row in rows) / len(rows) * 100 if rows else None,
        "realized_vs_quote_pct": sum(slipped) / len(slipped) if slipped else None,
    }


def build(days: int = 7, now: float | None = None) -> str:
    now = now or time.time()
    info = ml_model.get_model_info()
    lines = [f"🎯 PREDICTION PROGRESS · {days}D", "━━━━━━━━━━━━━━━━━━"]
    if info.get("status") != "active":
        lines += ["State: COLLECTING ⚪",
                  f"Valid 1h samples: {info.get('labeled_samples', 0)}/{info.get('needed', 200)}",
                  "Model promotion cannot be assessed until chronological training completes."]
        return "\n".join(lines)

    trained_at = float(info.get("trained_at") or now)
    metrics_by_cohort = info.get("metrics") or {}
    storage.save_model_evaluations(trained_at, metrics_by_cohort)
    runs = storage.get_model_evaluation_runs(2)
    previous = runs[1].get("metrics", {}).get("all") if len(runs) > 1 else None
    current = metrics_by_cohort.get("all", {})
    current_pass = _all_model_gates(current)
    repeated = current_pass and _independent_previous_period(current, previous)
    state = "CANDIDATE 🟢" if repeated else "SHADOW 🧪"
    lines += [f"State: {state}", "Target: +10% at 1h · untouched chronological test"]

    valid, matured, coverage = _coverage(days, now)
    lines += ["", "DATA QUALITY", f"{'✅' if matured >= 100 and coverage >= 80 else '❌'} 1h coverage {valid}/{matured} ({coverage:.0f}%) · need ≥100 and ≥80%"]

    lines += ["", "MODEL PROMOTION GATES"]
    for field, operator, threshold, label in MODEL_GATES:
        value = current.get(field)
        icon = "✅" if _passes(current, field, operator, threshold) else "❌"
        suffix = "%" if field in {"precision_pct", "expectancy_pct", "max_drawdown_pct"} else ""
        shown = f"{value}{suffix}" if value is not None else "N/A"
        lines.append(f"{icon} {label}: {shown} · need {operator}{threshold}{suffix}")
    previous_status = (
        "passed independently" if _independent_previous_period(current, previous)
        else "overlaps current test" if previous and _all_model_gates(previous)
        else "not passed" if previous else "not available yet"
    )
    lines.append(f"{'✅' if _independent_previous_period(current, previous) else '❌'} previous forward period: {previous_status}")

    lines += ["", "CURRENT VS PREVIOUS"]
    if not previous:
        lines.append("⚪ No earlier evaluation run stored yet")
    else:
        for field, label, lower_is_better in (("precision_pct", "precision", False),
                                               ("brier", "Brier", True),
                                               ("expectancy_pct", "net expectancy", False),
                                               ("max_drawdown_pct", "max drawdown", False)):
            current_value, previous_value = current.get(field), previous.get(field)
            if current_value is None or previous_value is None:
                lines.append(f"⚪ {label}: comparison unavailable")
                continue
            delta = current_value - previous_value
            improved = delta < 0 if lower_is_better else delta > 0
            icon = "↗️" if improved else "➡️" if delta == 0 else "↘️"
            lines.append(f"{icon} {label}: {previous_value:.2f} → {current_value:.2f} ({delta:+.2f})")

    lines += ["", "BY SOURCE"]
    for source in ("scan", "pool", "wallet"):
        metrics = metrics_by_cohort.get(source) or metrics_by_cohort.get(f"solana:{source}")
        if not metrics:
            lines.append(f"⚪ {source.title()}: insufficient model samples")
            continue
        expectancy = metrics.get("expectancy_pct")
        lines.append(
            f"{'✅' if _all_model_gates(metrics) else '❌'} {source.title()}: test {metrics.get('test_samples', 0)} · "
            f"trades {metrics.get('predicted_trades', 0)} · precision {metrics.get('precision_pct', 0):.0f}% · "
            f"exp {f'{expectancy:+.1f}%' if expectancy is not None else 'N/A'}"
        )

    execution = _execution(days, now)
    lines += ["", "EXECUTION READINESS"]
    if not execution["attempts"]:
        lines.append("⚪ No execution attempts in this period")
    else:
        for key, target, label in (("confirmation_pct", 95, "confirmation"),
                                   ("failure_pct", 5, "failures"), ("dropped_pct", 1, "dropped"),
                                   ("realized_vs_quote_pct", -3, "realized vs quote")):
            value = execution[key]
            passed = value is not None and (value >= target if key in {"confirmation_pct", "realized_vs_quote_pct"} else value < target)
            lines.append(f"{'✅' if passed else '❌'} {label}: {f'{value:.1f}%' if value is not None else 'N/A'}")

    lines += ["", "Promotion remains manual; this dashboard never enables trading."]
    return "\n".join(lines)
