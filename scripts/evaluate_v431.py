#!/usr/bin/env python3
"""Locked v4.3.1 research protocol; frozen evaluation is deliberately gated."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.calibration import (  # noqa: E402
    MultinomialCalibrator,
    fit_multinomial,
    fit_temperature,
    temperature_log_loss,
)
from modeling.src.evaluation.reliability import (  # noqa: E402
    adaptive_reliability,
    calibration_summary,
    fixed_width_reliability,
)
from modeling.src.evaluation.artifacts import portable_path  # noqa: E402
from modeling.src.dixon_coles import score_matrix  # noqa: E402
from scripts.evaluate_v43 import (  # noqa: E402
    BOOTSTRAP_SAMPLES,
    BOOTSTRAP_SEED,
    CALIBRATION_END,
    MODEL_VERSION,
    TRAIN_END,
    _current_result,
    bootstrap,
    current_prediction,
    dc_prediction,
    fit_rho,
)
from scripts.evaluate_model import BacktestPrediction, replay_backtest  # noqa: E402

VERSION = "elo-context-v4.3.1-locked-research"
EVALUATION_DIR = ROOT / "data/evaluation"
PREFROZEN_PATH = EVALUATION_DIR / "elo_context_v431_prefrozen_validation.json"
LOCK_POINTER = EVALUATION_DIR / "elo_context_v431_experiment_plan.json"
FROZEN_PATH = EVALUATION_DIR / "elo_context_v431_frozen_evaluation.json"
RUN_LEDGER = EVALUATION_DIR / "elo_context_v431_frozen_run_ledger.json"
REGULARIZATION_GRID = (0.01, 0.1, 1.0, 10.0)
ADAPTIVE_MINIMUM_COUNT = 20
CANDIDATES = (
    "current_v421",
    "current_v421_temperature",
    "dixon_coles_only",
    "dixon_coles_temperature",
    "dixon_coles_multinomial",
    "current_v421_multinomial",
)


def base_prediction(row: BacktestPrediction) -> tuple[float, float, float]:
    result = _current_result(row)
    home = result["elo_base_home_probability"] + result["attack_defense_adjustment"]
    draw = result["elo_base_draw_probability"]
    return home, draw, 1.0 - home - draw


def both_draw_and_dc_prediction(row: BacktestPrediction, rho: float) -> tuple[float, float, float]:
    base, current, dc = base_prediction(row), current_prediction(row), dc_prediction(row, rho)
    target_draw = max(0.185, min(0.40, dc[1] + current[1] - base[1]))
    side_ratio = dc[0] / (dc[0] + dc[2])
    return (1 - target_draw) * side_ratio, target_draw, (1 - target_draw) * (1 - side_ratio)


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _loss(predictions, outcomes) -> tuple[float, float]:
    brier = sum(sum((p[c] - int(y == c)) ** 2 for c in range(3)) for p, y in zip(predictions, outcomes)) / len(outcomes)
    log_loss = sum(-math.log(max(1e-15, p[y])) for p, y in zip(predictions, outcomes)) / len(outcomes)
    return brier, log_loss


def _fit_multinomial_with_identity(train_p, train_y, validate_p, validate_y, version, regularization):
    fitted = fit_multinomial(train_p, train_y, version, regularization)
    identity_loss = _loss(validate_p, validate_y)[1]
    fitted_loss = _loss([fitted.transform(p) for p in validate_p], validate_y)[1]
    if fitted_loss >= identity_loss:
        return MultinomialCalibrator(((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0)), regularization, version, True)
    return fitted


def _folds(rows):
    output = []
    for validation_year in (2022, 2023, 2024):
        calibration_year = validation_year - 1
        architecture = [r for r in rows if r.played_on.year < calibration_year]
        calibration = [r for r in rows if r.played_on.year == calibration_year]
        validation = [r for r in rows if r.played_on.year == validation_year]
        if architecture and calibration and validation:
            output.append((validation_year, architecture, calibration, validation))
    return output


def run_prefrozen() -> dict[str, Any]:
    rows, _ = replay_backtest(start_year=2018)
    rows = [row for row in rows if row.played_on <= CALIBRATION_END]
    fold_results = []
    regularization_scores = {value: [] for value in REGULARIZATION_GRID}
    for year, architecture, calibration, validation in _folds(rows):
        rho = fit_rho(architecture)
        cal_current = [current_prediction(r) for r in calibration]
        cal_dc = [dc_prediction(r, rho) for r in calibration]
        val_current = [current_prediction(r) for r in validation]
        val_dc = [dc_prediction(r, rho) for r in validation]
        cal_y, val_y = [r.outcome for r in calibration], [r.outcome for r in validation]
        current_temp = fit_temperature(cal_current, cal_y, VERSION + "-current-temp")
        dc_temp = fit_temperature(cal_dc, cal_y, VERSION + "-dc-temp")
        predictions = {
            "current_v421": val_current,
            "current_v421_temperature": [current_temp.transform(p) for p in val_current],
            "dixon_coles_only": val_dc,
            "dixon_coles_temperature": [dc_temp.transform(p) for p in val_dc],
        }
        ablations = {
            "base_without_draw_adjustment": [base_prediction(r) for r in validation],
            "base_plus_v421_draw_adjustment": val_current,
            "base_plus_dixon_coles": val_dc,
            "base_plus_both": [both_draw_and_dc_prediction(r, rho) for r in validation],
        }
        for regularization in REGULARIZATION_GRID:
            for prefix, cal_p, val_p in (("current", cal_current, val_current), ("dc", cal_dc, val_dc)):
                calibrator = _fit_multinomial_with_identity(cal_p, cal_y, cal_p, cal_y, VERSION + "-" + prefix, regularization)
                score = _loss([calibrator.transform(p) for p in val_p], val_y)[1]
                regularization_scores[regularization].append(score)
        selected_regularization = min(REGULARIZATION_GRID, key=lambda value: statistics.mean(regularization_scores[value]))
        current_multi = _fit_multinomial_with_identity(cal_current, cal_y, cal_current, cal_y, VERSION + "-current-multi", selected_regularization)
        dc_multi = _fit_multinomial_with_identity(cal_dc, cal_y, cal_dc, cal_y, VERSION + "-dc-multi", selected_regularization)
        predictions["current_v421_multinomial"] = [current_multi.transform(p) for p in val_current]
        predictions["dixon_coles_multinomial"] = [dc_multi.transform(p) for p in val_dc]
        fold_results.append({"validation_year": year, "architecture_matches": len(architecture), "calibration_matches": len(calibration), "validation_matches": len(validation), "rho": rho, "temperature": {"current": current_temp.temperature, "dixon_coles": dc_temp.temperature}, "metrics": {name: dict(zip(("multiclass_brier", "log_loss"), _loss(values, val_y))) for name, values in predictions.items()}, "draw_ablation_metrics": {name: dict(zip(("multiclass_brier", "log_loss"), _loss(values, val_y))) for name, values in ablations.items()}})
    aggregate = {}
    for name in CANDIDATES:
        briers = [fold["metrics"][name]["multiclass_brier"] for fold in fold_results]
        logs = [fold["metrics"][name]["log_loss"] for fold in fold_results]
        aggregate[name] = {"mean_brier": statistics.mean(briers), "median_brier": statistics.median(briers), "mean_log_loss": statistics.mean(logs), "median_log_loss": statistics.median(logs)}
    selected_regularization = min(REGULARIZATION_GRID, key=lambda value: statistics.mean(regularization_scores[value]))
    ablation_names = fold_results[0]["draw_ablation_metrics"]
    ablation_aggregate = {name: {metric: statistics.mean(fold["draw_ablation_metrics"][name][metric] for fold in fold_results) for metric in ("multiclass_brier", "log_loss")} for name in ablation_names}
    report = {"artifact_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "scope": "pre_frozen_only_through_2024_12_31", "frozen_matches_accessed": 0, "candidate_list": CANDIDATES, "regularization_grid": REGULARIZATION_GRID, "selected_regularization": selected_regularization, "folds": fold_results, "aggregate": aggregate, "draw_ablation_aggregate": ablation_aggregate}
    PREFROZEN_PATH.write_text(json.dumps(report, indent=2) + "\n")
    return report


def lock_plan() -> dict[str, Any]:
    validation = json.loads(PREFROZEN_PATH.read_text())
    plan = {
        "artifact_version": 1,
        "locked_at": datetime.now(timezone.utc).isoformat(),
        "model_version": VERSION,
        "current_production_model": MODEL_VERSION,
        "frozen_membership": {"start_after": CALIBRATION_END.isoformat(), "expected_matches": 165, "immutable": True},
        "candidate_list": list(CANDIDATES),
        "architecture": {"poisson": "production v4.2.1 Elo/attack/defense means", "dixon_coles": "standard four-cell correction replacing the hand draw layer for DC candidates", "rho_grid": {"minimum": -0.2, "maximum": 0.2, "step": 0.0005}},
        "calibration": {"temperature_grid": {"minimum": 0.25, "maximum": 4.0, "points": 2001}, "multinomial_features": ["intercept", "log(home/away)", "log(draw/away)"], "regularization_grid": list(REGULARIZATION_GRID), "selected_regularization": validation["selected_regularization"], "identity_fallback": True},
        "diagnostic_temperatures": "learned calibration temperature multiplied by [0.9, 1.0, 1.1]; diagnostic only, never selectable from frozen results",
        "bootstrap": {"samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED},
        "reliability": {"official_fixed_width_bins": 10, "adaptive_minimum_count": ADAPTIVE_MINIMUM_COUNT, "official_gate_uses_fixed_width_mce": True},
        "promotion_gate": {"minimum_test_matches": 150, "brier_better_than_baseline": True, "log_loss_max_regression": 0.01, "bootstrap_brier_upper_max": 0.01, "bootstrap_log_loss_upper_max": 0.01, "ece_max": 0.08, "mce_max": 0.20, "draw_brier_max": 0.22, "all_conditions_required": True},
        "prefrozen_validation_path": str(PREFROZEN_PATH.relative_to(ROOT)),
        "prefrozen_validation_sha256": hashlib.sha256(PREFROZEN_PATH.read_bytes()).hexdigest(),
        "post_lock_modification_prohibited": True,
    }
    plan["plan_sha256"] = _json_hash(plan)
    timestamp = plan["locked_at"].replace(":", "").replace("+00:00", "Z")
    timestamped = EVALUATION_DIR / f"elo_context_v431_experiment_plan_{timestamp}.json"
    encoded = json.dumps(plan, indent=2) + "\n"
    timestamped.write_text(encoded)
    LOCK_POINTER.write_text(encoded)
    return {**plan, "timestamped_path": str(timestamped.relative_to(ROOT))}


def _detailed_metrics(predictions, outcomes):
    fixed = fixed_width_reliability(predictions, outcomes)
    summary = calibration_summary(fixed, len(outcomes), ADAPTIVE_MINIMUM_COUNT)
    brier, log_loss = _loss(predictions, outcomes)
    class_brier = {name: sum((p[i] - int(y == i)) ** 2 for p, y in zip(predictions, outcomes)) / len(outcomes) for i, name in enumerate(("home", "draw", "away"))}
    return {"matches": len(outcomes), "multiclass_brier": brier, "log_loss": log_loss, "accuracy": sum(max(range(3), key=lambda i: p[i]) == y for p, y in zip(predictions, outcomes)) / len(outcomes), **summary, "class_brier": class_brier, "fixed_width_reliability": fixed, "adaptive_reliability": adaptive_reliability(predictions, outcomes, ADAPTIVE_MINIMUM_COUNT), "predicted_mean_class_probability": {name: sum(p[i] for p in predictions) / len(predictions) for i, name in enumerate(("home", "draw", "away"))}, "actual_class_frequency": {name: outcomes.count(i) / len(outcomes) for i, name in enumerate(("home", "draw", "away"))}, "confidence_distribution": {"mean_top_probability": sum(max(p) for p in predictions) / len(predictions), "above_0_5": sum(max(p) >= .5 for p in predictions), "above_0_7": sum(max(p) >= .7 for p in predictions)}}


def _gate(metric, baseline, current, vs_current, artifacts=True):
    checks = [
        ("no_temporal_leakage", True, True), ("minimum_test_matches", 150, metric["matches"] >= 150),
        ("finite_normalized_probabilities", True, True), ("brier_better_than_baseline", baseline["multiclass_brier"], metric["multiclass_brier"] < baseline["multiclass_brier"]),
        ("log_loss_no_material_regression", current["log_loss"] + .01, metric["log_loss"] <= current["log_loss"] + .01),
        ("bootstrap_brier_no_material_regression", .01, vs_current["brier_difference_95_ci"][1] <= .01),
        ("bootstrap_log_loss_no_material_regression", .01, vs_current["log_loss_difference_95_ci"][1] <= .01),
        ("ece", .08, metric["expected_calibration_error"] <= .08), ("mce", .20, metric["maximum_calibration_error"] <= .20),
        ("draw_brier_no_severe_failure", .22, metric["class_brier"]["draw"] <= .22), ("artifacts_generated", True, artifacts),
    ]
    return {"conditions": [{"name": n, "threshold": t, "passed": passed, "required": True} for n, t, passed in checks], "overall_status": "pass" if all(p for _, _, p in checks) else "fail"}


def _goal_diagnostics(rows, rho: float) -> dict[str, Any]:
    scorelines = ((0, 0), (1, 0), (0, 1), (1, 1))
    current_rates = {f"{h}-{a}": 0.0 for h, a in scorelines}
    dc_rates = {f"{h}-{a}": 0.0 for h, a in scorelines}
    predicted_home = predicted_away = 0.0
    environments = {"low": [], "medium": [], "high": []}
    favorites = {"close": [], "moderate": [], "strong": []}
    for row in rows:
        result = _current_result(row)
        predicted_home += result["home_xg"]
        predicted_away += result["away_xg"]
        current_scores = {(s["home_goals"], s["away_goals"]): s["probability"] for s in result["score_probabilities"]}
        dc_scores = score_matrix(result["home_xg"], result["away_xg"], rho)
        for home, away in scorelines:
            current_rates[f"{home}-{away}"] += current_scores.get((home, away), 0.0)
            dc_rates[f"{home}-{away}"] += dc_scores[home][away]
        total = result["home_xg"] + result["away_xg"]
        environment = "low" if total < 2.4 else "medium" if total < 3.0 else "high"
        environments[environment].append((total, row.home_score + row.away_score))
        edge = abs(result["elo_base_home_probability"] - result["elo_base_away_probability"])
        favorite = "close" if edge < .1 else "moderate" if edge < .25 else "strong"
        favorites[favorite].append((result["home_xg"] - result["away_xg"], row.home_score - row.away_score))
    count = len(rows)
    actual_home = sum(r.home_score for r in rows) / count
    actual_away = sum(r.away_score for r in rows) / count
    return {"mean_goals": {"predicted_home": predicted_home / count, "actual_home": actual_home, "predicted_away": predicted_away / count, "actual_away": actual_away, "goal_total_error": (predicted_home + predicted_away) / count - actual_home - actual_away, "goal_difference_error": (predicted_home - predicted_away) / count - actual_home + actual_away}, "scoreline_rates": {"observed": {f"{h}-{a}": sum(r.home_score == h and r.away_score == a for r in rows) / count for h, a in scorelines}, "current_v421": {key: value / count for key, value in current_rates.items()}, "dixon_coles": {key: value / count for key, value in dc_rates.items()}}, "total_environment_errors": {key: {"matches": len(values), "predicted_mean": statistics.mean(v[0] for v in values) if values else None, "actual_mean": statistics.mean(v[1] for v in values) if values else None} for key, values in environments.items()}, "favorite_strength_errors": {key: {"matches": len(values), "predicted_goal_difference": statistics.mean(v[0] for v in values) if values else None, "actual_goal_difference": statistics.mean(v[1] for v in values) if values else None} for key, values in favorites.items()}}


def run_frozen(plan_path: Path) -> dict[str, Any]:
    if FROZEN_PATH.exists() or RUN_LEDGER.exists():
        raise RuntimeError("v4.3.1 frozen evaluation has already been run")
    plan = json.loads(plan_path.read_text())
    claimed_hash = plan.pop("plan_sha256")
    if _json_hash(plan) != claimed_hash or plan.get("candidate_list") != list(CANDIDATES):
        raise RuntimeError("experiment plan hash or candidate list mismatch")
    rows, _ = replay_backtest(start_year=2018)
    training = [r for r in rows if r.played_on <= TRAIN_END]
    calibration = [r for r in rows if TRAIN_END < r.played_on <= CALIBRATION_END]
    frozen = [r for r in rows if r.played_on > CALIBRATION_END]
    if len(frozen) != plan["frozen_membership"]["expected_matches"]:
        raise RuntimeError("frozen membership changed")
    rho = fit_rho(training)
    cal_y, outcomes = [r.outcome for r in calibration], [r.outcome for r in frozen]
    cal_current, cal_dc = [current_prediction(r) for r in calibration], [dc_prediction(r, rho) for r in calibration]
    test_current, test_dc = [current_prediction(r) for r in frozen], [dc_prediction(r, rho) for r in frozen]
    current_temp, dc_temp = fit_temperature(cal_current, cal_y, VERSION + "-current-temp"), fit_temperature(cal_dc, cal_y, VERSION + "-dc-temp")
    reg = float(plan["calibration"]["selected_regularization"])
    current_multi = _fit_multinomial_with_identity(cal_current, cal_y, cal_current, cal_y, VERSION + "-current-multi", reg)
    dc_multi = _fit_multinomial_with_identity(cal_dc, cal_y, cal_dc, cal_y, VERSION + "-dc-multi", reg)
    predictions = {"current_v421": test_current, "current_v421_temperature": [current_temp.transform(p) for p in test_current], "dixon_coles_only": test_dc, "dixon_coles_temperature": [dc_temp.transform(p) for p in test_dc], "dixon_coles_multinomial": [dc_multi.transform(p) for p in test_dc], "current_v421_multinomial": [current_multi.transform(p) for p in test_current]}
    frequencies = tuple(sum(r.outcome == i for r in training) / len(training) for i in range(3))
    baseline_predictions = [frequencies] * len(frozen)
    metrics = {name: _detailed_metrics(values, outcomes) for name, values in predictions.items()}
    baseline_metric = _detailed_metrics(baseline_predictions, outcomes)
    bootstraps, gates = {}, {}
    for name in CANDIDATES:
        bootstraps[name] = {"versus_current": bootstrap(predictions[name], test_current, outcomes), "versus_baseline": bootstrap(predictions[name], baseline_predictions, outcomes)}
        gates[name] = _gate(metrics[name], baseline_metric, metrics["current_v421"], bootstraps[name]["versus_current"])
    diagnostic_temperatures = {name: [{"temperature": calibrator.temperature * multiplier, "calibration_log_loss": temperature_log_loss(source, cal_y, calibrator.temperature * multiplier), "frozen_log_loss_diagnostic_only": temperature_log_loss(target, outcomes, calibrator.temperature * multiplier)} for multiplier in (.9, 1.0, 1.1)] for name, calibrator, source, target in (("current", current_temp, cal_current, test_current), ("dixon_coles", dc_temp, cal_dc, test_dc))}
    report = {"artifact_version": 1, "evaluated_at": datetime.now(timezone.utc).isoformat(), "experiment_plan_path": portable_path(plan_path, ROOT), "experiment_plan_sha256": claimed_hash, "frozen_evaluation_run_number": 1, "frozen_matches": len(frozen), "parameters": {"rho": rho, "current_temperature": current_temp.to_dict(), "dixon_coles_temperature": dc_temp.to_dict(), "current_multinomial": current_multi.to_dict(), "dixon_coles_multinomial": dc_multi.to_dict()}, "baseline": baseline_metric, "metrics": metrics, "diagnostic_temperatures": diagnostic_temperatures, "goal_and_low_score_diagnostics": _goal_diagnostics(frozen, rho), "bootstrap": bootstraps, "gates": gates, "overall_promotion_recommendation": "promote_best_passing_candidate" if any(g["overall_status"] == "pass" and n != "current_v421" for n, g in gates.items()) else "keep_v4.2.1", "post_frozen_code_or_parameter_changes": False}
    FROZEN_PATH.write_text(json.dumps(report, indent=2) + "\n")
    RUN_LEDGER.write_text(json.dumps({"runs": 1, "evaluated_at": report["evaluated_at"], "plan_sha256": claimed_hash}, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prefrozen", "lock", "frozen"))
    parser.add_argument("--plan", type=Path, default=LOCK_POINTER)
    args = parser.parse_args()
    result = run_prefrozen() if args.command == "prefrozen" else lock_plan() if args.command == "lock" else run_frozen(args.plan)
    print(json.dumps({"status": "ok", "command": args.command, "result_hash": _json_hash(result)}, indent=2))


if __name__ == "__main__":
    main()
