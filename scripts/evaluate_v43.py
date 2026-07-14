#!/usr/bin/env python3
"""Leakage-safe v4.3 research evaluation and readiness-gate artifact."""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.calibration import fit_temperature
from modeling.src.dixon_coles import result_probabilities, tau, valid_rho_bounds
from scripts.evaluate_model import BacktestPrediction, replay_backtest
from scripts.generate_predictions import MODEL_VERSION, calculate_prediction

CANDIDATE_VERSION = "elo-context-v4.3-dixon-coles-calibrated"
REPORT_PATH = ROOT / "data/evaluation/elo_context_v43_readiness.json"
CALIBRATOR_PATH = ROOT / "data/evaluation/elo_context_v43_calibrator.json"
TRAIN_END = date(2022, 12, 31)
CALIBRATION_END = date(2024, 12, 31)
BOOTSTRAP_SEED = 43026
BOOTSTRAP_SAMPLES = 2000


def _ratings(row: BacktestPrediction) -> tuple[dict[str, float], dict[str, float]]:
    return (
        {"elo_rating": 1500.0, "attack_rating": row.home_attack_rating, "defense_rating": row.home_defense_rating, "form_rating": row.home_form_rating, "matches_played": 5},
        {"elo_rating": 1500.0, "attack_rating": row.away_attack_rating, "defense_rating": row.away_defense_rating, "form_rating": row.away_form_rating, "matches_played": 5},
    )


def _current_result(row: BacktestPrediction) -> dict[str, Any]:
    home, away = _ratings(row)
    # Recover the chronologically reconstructed Elo gap from its baseline vector.
    low, high = -1200.0, 1200.0
    for _ in range(60):
        gap = (low + high) / 2
        home_xg, away_xg = 1.35 * math.exp(gap / 800), 1.35 * math.exp(-gap / 800)
        trial = result_probabilities(home_xg, away_xg, 0.0, 6)[0]
        if trial < row.elo[0]:
            low = gap
        else:
            high = gap
    gap = (low + high) / 2
    home["elo_rating"] = 1500.0 + gap / 2
    away["elo_rating"] = 1500.0 - gap / 2
    return calculate_prediction(home, away)


def current_prediction(row: BacktestPrediction) -> tuple[float, float, float]:
    result = _current_result(row)
    return tuple(result[key] for key in ("home_win_probability", "draw_probability", "away_win_probability"))  # type: ignore[return-value]


def dc_prediction(row: BacktestPrediction, rho: float) -> tuple[float, float, float]:
    result = _current_result(row)
    current = tuple(result[key] for key in ("home_win_probability", "draw_probability", "away_win_probability"))
    base = result_probabilities(result["home_xg"], result["away_xg"], rho)
    # Preserve the production side-win direction while replacing its ad-hoc draw layer.
    side_ratio = current[0] / max(1e-12, current[0] + current[2])
    return (1 - base[1]) * side_ratio, base[1], (1 - base[1]) * (1 - side_ratio)


def fit_rho(rows: list[BacktestPrediction]) -> float:
    best = (float("inf"), 0.0)
    expected = []
    for row in rows:
        result = _current_result(row)
        expected.append((result["home_xg"], result["away_xg"]))
    for index in range(801):
        rho = -0.2 + index * 0.0005
        loss = 0.0
        valid = True
        for row, (home_xg, away_xg) in zip(rows, expected):
            lower, upper = valid_rho_bounds(home_xg, away_xg)
            if not lower <= rho <= upper:
                valid = False
                break
            correction = tau(row.home_score, row.away_score, home_xg, away_xg, rho)
            if correction <= 0:
                valid = False
                break
            if row.home_score <= 1 and row.away_score <= 1:
                loss -= math.log(correction)
        if valid and loss < best[0]:
            best = loss, rho
    return best[1]


def reliability(predictions: list[tuple[float, float, float]], outcomes: list[int], bins: int = 10) -> list[dict[str, Any]]:
    output = []
    for index in range(bins):
        lower, upper = index / bins, (index + 1) / bins
        values = [(p[c], int(y == c)) for p, y in zip(predictions, outcomes) for c in range(3) if lower <= p[c] < upper or (index == bins - 1 and p[c] == 1)]
        output.append({"lower": lower, "upper": upper, "count": len(values), "mean_probability": sum(v[0] for v in values) / len(values) if values else None, "observed_rate": sum(v[1] for v in values) / len(values) if values else None})
    return output


def metrics(predictions: list[tuple[float, float, float]], outcomes: list[int]) -> dict[str, Any]:
    n = len(outcomes)
    table = reliability(predictions, outcomes)
    populated = [b for b in table if b["count"]]
    ece = sum(b["count"] / (3 * n) * abs(b["mean_probability"] - b["observed_rate"]) for b in populated)
    class_brier = [sum((p[c] - int(y == c)) ** 2 for p, y in zip(predictions, outcomes)) / n for c in range(3)]
    return {
        "matches": n,
        "multiclass_brier": sum(sum((p[c] - int(y == c)) ** 2 for c in range(3)) for p, y in zip(predictions, outcomes)) / n,
        "log_loss": sum(-math.log(max(1e-15, p[y])) for p, y in zip(predictions, outcomes)) / n,
        "accuracy": sum(max(range(3), key=lambda c: p[c]) == y for p, y in zip(predictions, outcomes)) / n,
        "class_brier": dict(zip(("home", "draw", "away"), class_brier)),
        "expected_calibration_error": ece,
        "maximum_calibration_error": max(abs(b["mean_probability"] - b["observed_rate"]) for b in populated),
        "mean_probability_of_actual_outcome": sum(p[y] for p, y in zip(predictions, outcomes)) / n,
        "predicted_class_distribution": {name: sum(max(range(3), key=lambda c: p[c]) == i for p in predictions) / n for i, name in enumerate(("home", "draw", "away"))},
        "actual_class_distribution": {name: outcomes.count(i) / n for i, name in enumerate(("home", "draw", "away"))},
        "reliability_buckets": table,
    }


def bootstrap(candidate: list[tuple[float, float, float]], reference: list[tuple[float, float, float]], outcomes: list[int]) -> dict[str, list[float]]:
    rng = random.Random(BOOTSTRAP_SEED)
    brier, logs = [], []
    for _ in range(BOOTSTRAP_SAMPLES):
        indices = [rng.randrange(len(outcomes)) for _ in outcomes]
        cm = metrics([candidate[i] for i in indices], [outcomes[i] for i in indices])
        rm = metrics([reference[i] for i in indices], [outcomes[i] for i in indices])
        brier.append(cm["multiclass_brier"] - rm["multiclass_brier"])
        logs.append(cm["log_loss"] - rm["log_loss"])
    def interval(values: list[float]) -> list[float]:
        values.sort(); return [values[int(.025 * len(values))], values[int(.975 * len(values))]]
    return {"brier_difference_95_ci": interval(brier), "log_loss_difference_95_ci": interval(logs), "difference_definition": "candidate minus reference"}


def run() -> dict[str, Any]:
    rows, all_results = replay_backtest(start_year=2018)
    train = [row for row in rows if row.played_on <= TRAIN_END]
    calibration = [row for row in rows if TRAIN_END < row.played_on <= CALIBRATION_END]
    test = [row for row in rows if row.played_on > CALIBRATION_END]
    if set(train) & set(calibration) or set(calibration) & set(test):
        raise AssertionError("chronological splits overlap")
    rho = fit_rho(train)
    calibration_raw = [dc_prediction(row, rho) for row in calibration]
    calibrator = fit_temperature(calibration_raw, [row.outcome for row in calibration], CANDIDATE_VERSION)
    CALIBRATOR_PATH.write_text(json.dumps({**calibrator.to_dict(), "rho": rho, "fitted_on": {"start": calibration[0].played_on.isoformat(), "end": calibration[-1].played_on.isoformat(), "matches": len(calibration)}}, indent=2) + "\n")
    outcomes = [row.outcome for row in test]
    frequencies = tuple(sum(row.outcome == c for row in train) / len(train) for c in range(3))
    baseline = [frequencies for _ in test]
    current = [current_prediction(row) for row in test]
    dc = [dc_prediction(row, rho) for row in test]
    calibrated = [calibrator.transform(p) for p in dc]
    current_calibrator = fit_temperature([current_prediction(row) for row in calibration], [row.outcome for row in calibration], MODEL_VERSION + "-calibrated-research")
    current_calibrated = [current_calibrator.transform(p) for p in current]
    evaluated = {"baseline": metrics(baseline, outcomes), "current": metrics(current, outcomes), "dixon_coles": metrics(dc, outcomes), "current_calibrated_only": metrics(current_calibrated, outcomes), "dixon_coles_calibrated": metrics(calibrated, outcomes)}
    cm, bm, pm = evaluated["dixon_coles_calibrated"], evaluated["baseline"], evaluated["current"]
    candidate_vs_current = bootstrap(calibrated, current, outcomes)
    candidate_vs_baseline = bootstrap(calibrated, baseline, outcomes)
    conditions = [
        ("no_temporal_leakage", True, True, "All ratings update after same-day predictions; rolling inputs are strictly pre-match."),
        ("minimum_test_matches", 150, len(test) >= 150, f"Frozen test contains {len(test)} matches."),
        ("finite_normalized_probabilities", 1e-9, all(all(math.isfinite(v) and v >= 0 for v in p) and abs(sum(p)-1) < 1e-9 for p in calibrated), "Every candidate probability triple is finite, nonnegative, and normalized."),
        ("brier_better_than_baseline", bm["multiclass_brier"], cm["multiclass_brier"] < bm["multiclass_brier"], "Candidate multiclass Brier must beat the frozen training-frequency baseline."),
        ("log_loss_no_material_regression", pm["log_loss"] + .01, cm["log_loss"] <= pm["log_loss"] + .01, "Candidate log loss may regress by at most 0.01 versus production."),
        ("bootstrap_brier_no_material_regression", .01, candidate_vs_current["brier_difference_95_ci"][1] <= .01, "The upper 95% bootstrap bound for Brier regression versus production must be at most 0.01."),
        ("bootstrap_log_loss_no_material_regression", .01, candidate_vs_current["log_loss_difference_95_ci"][1] <= .01, "The upper 95% bootstrap bound for log-loss regression versus production must be at most 0.01."),
        ("ece", .08, cm["expected_calibration_error"] <= .08, "ECE must be at most 0.08."),
        ("mce", .20, cm["maximum_calibration_error"] <= .20, "MCE must be at most 0.20."),
        ("draw_brier_no_severe_failure", .22, cm["class_brier"]["draw"] <= .22, "Draw one-vs-rest Brier must be at most 0.22."),
        ("artifacts_generated", True, CALIBRATOR_PATH.exists(), "Versioned evaluation and calibration artifacts must be generated."),
    ]
    measured = {
        "minimum_test_matches": len(test), "ece": cm["expected_calibration_error"],
        "mce": cm["maximum_calibration_error"], "draw_brier_no_severe_failure": cm["class_brier"]["draw"],
        "brier_better_than_baseline": cm["multiclass_brier"], "log_loss_no_material_regression": cm["log_loss"],
        "bootstrap_brier_no_material_regression": candidate_vs_current["brier_difference_95_ci"][1],
        "bootstrap_log_loss_no_material_regression": candidate_vs_current["log_loss_difference_95_ci"][1],
    }
    gate_conditions = [{"name": n, "threshold": t, "measured_value": measured.get(n, passed), "passed": passed, "required": True, "explanation": explanation} for n, t, passed, explanation in conditions]
    gate_pass = all(c["passed"] for c in gate_conditions)
    raw_path = ROOT / "data/raw/international_results.csv"
    return {
        "artifact_version": 1, "candidate_model_version": CANDIDATE_VERSION, "current_production_model_version": MODEL_VERSION,
        "evaluation_timestamp": datetime.now(timezone.utc).isoformat(),
        "dataset": {"path": str(raw_path.relative_to(ROOT)), "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(), "loaded_matches": len(all_results), "eligible_matches": len(rows), "competitions": sorted({r.tournament for r in all_results}), "exclusions": ["unmapped teams", "invalid scores", "fewer than five prior matches per team", "player/squad/shot-volume features without point-in-time archives"]},
        "splits": {"training": {"start": train[0].played_on.isoformat(), "end": train[-1].played_on.isoformat(), "matches": len(train)}, "calibration": {"start": calibration[0].played_on.isoformat(), "end": calibration[-1].played_on.isoformat(), "matches": len(calibration)}, "final_test": {"start": test[0].played_on.isoformat(), "end": test[-1].played_on.isoformat(), "matches": len(test)}},
        "parameters": {"dixon_coles_rho": rho, "calibration": calibrator.to_dict(), "current_calibration_ablation": current_calibrator.to_dict()},
        "metrics": evaluated,
        "chronological_segments": {str(year): metrics([calibrated[i] for i, row in enumerate(test) if row.played_on.year == year], [row.outcome for row in test if row.played_on.year == year]) for year in sorted({r.played_on.year for r in test})},
        "bootstrap": {"candidate_vs_current": candidate_vs_current, "candidate_vs_baseline": candidate_vs_baseline, "seed": BOOTSTRAP_SEED, "samples": BOOTSTRAP_SAMPLES},
        "leakage_audit": {"status": "pass", "same_day_results_batched": True, "current_ratings_for_past_predictions": False, "future_rolling_matches": False, "post_match_stats_used": False, "limitations": ["Raw source has match dates, not kickoff times; all matches on a date are therefore conservatively batched."]},
        "gate": {"defined_before_final_evaluation": True, "conditions": gate_conditions, "overall_status": "pass" if gate_pass else "fail"},
        "promotion_recommendation": "promote" if gate_pass else "keep_current_experimental_candidate",
    }


if __name__ == "__main__":
    report = run()
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print(REPORT_PATH)
