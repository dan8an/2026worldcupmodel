#!/usr/bin/env python3
"""Pre-confirmation-only evaluation for experimental elo-context-v4.4."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.dixon_coles import result_probabilities  # noqa: E402
from modeling.src.evaluation.artifacts import write_json_atomic  # noqa: E402
from modeling.src.expected_goals_v44 import (  # noqa: E402
    PoissonGoalModel,
    TeamGoalHistory,
    amplify_goal_difference,
    competition_category,
    fit_poisson,
    opponent_adjusted_snapshot,
    opponent_adjusted_xg,
    update_histories_batched,
)
from modeling.src.features.context import HistoricalResult, load_historical_results  # noqa: E402
from scripts.evaluate_model import BacktestPrediction, replay_backtest  # noqa: E402
from scripts.evaluate_v43 import _current_result, current_prediction  # noqa: E402

VERSION = "elo-context-v4.4-opponent-adjusted-xg-experimental"
END = date(2024, 12, 31)
OUT = ROOT / "data/evaluation/elo_context_v44_preholdout.json"
PARAMETERS = ROOT / "data/evaluation/elo_context_v44_parameters.json"
PLAN = ROOT / "data/evaluation/elo_context_v44_experiment_plan.json"
FEATURE_DEFINITIONS = ROOT / "data/evaluation/elo_context_v44_feature_definitions.json"
CONFIRMATION_LEDGER = ROOT / "data/evaluation/elo_context_v44_confirmation_ledger.json"
READINESS = ROOT / "data/evaluation/elo_context_v44_readiness.json"
FEATURES = ("intercept", "elo_side_gap", "long_attack", "opponent_defense_weakness", "short_attack", "opponent_short_defense_weakness", "home_non_neutral", "qualification", "nations_league", "world_cup", "other_competitive")
CANDIDATES = ("current_v421", "opponent_adjusted", "opponent_plus_recency", "opponent_recency_separation", "opponent_recency_separation_competition", "ridge_poisson_glm")


@dataclass(frozen=True)
class FeatureMatch:
    played_on: date
    backtest: BacktestPrediction
    home: dict[str, float]
    away: dict[str, float]
    category: str


def build_feature_matches() -> list[FeatureMatch]:
    backtest, _ = replay_backtest(start_year=2018)
    index = {(r.played_on, r.home_team_id, r.away_team_id): r for r in backtest if r.played_on <= END}
    results = sorted(load_historical_results(), key=lambda r: (r.played_on, r.home_team_id, r.away_team_id))
    by_date: dict[date, list[HistoricalResult]] = defaultdict(list)
    for result in results:
        by_date[result.played_on].append(result)
    histories: dict[str, TeamGoalHistory] = {}
    output = []
    for played_on in sorted(by_date):
        day = by_date[played_on]
        for result in day:
            row = index.get((played_on, result.home_team_id, result.away_team_id))
            if row is not None:
                output.append(FeatureMatch(played_on, row, opponent_adjusted_snapshot(histories, result.home_team_id, played_on), opponent_adjusted_snapshot(histories, result.away_team_id, played_on), competition_category(result.tournament)))
        update_histories_batched(histories, day)
    return output


def _preserve_draw_layer(row: BacktestPrediction, xg: tuple[float, float]) -> tuple[float, float, float]:
    raw = result_probabilities(*xg, 0.0)
    current_result = _current_result(row)
    current = current_prediction(row)
    current_raw = result_probabilities(current_result["home_xg"], current_result["away_xg"], 0.0)
    target_draw = max(.185, min(.40, raw[1] + current[1] - current_raw[1]))
    side = raw[0] / (raw[0] + raw[2])
    return (1 - target_draw) * side, target_draw, (1 - target_draw) * (1 - side)


def _competition_multipliers(rows: list[FeatureMatch]) -> dict[str, float]:
    totals: dict[str, list[int]] = defaultdict(list)
    all_totals = [row.backtest.home_score + row.backtest.away_score for row in rows]
    overall = statistics.mean(all_totals)
    for row in rows:
        totals[row.category].append(row.backtest.home_score + row.backtest.away_score)
    return {category: max(.9, min(1.1, (sum(values) + 20 * overall) / (len(values) + 20) / overall)) for category, values in totals.items()}


def _glm_features(row: FeatureMatch, home_side: bool) -> tuple[float, ...]:
    result = _current_result(row.backtest)
    elo_gap = math.log(result["home_xg"] / result["away_xg"]) * 400
    own, opponent = (row.home, row.away) if home_side else (row.away, row.home)
    category = row.category
    return (1.0, (elo_gap if home_side else -elo_gap) / 400, own["long_attack"], opponent["long_defense_weakness"], own["short_attack"], opponent["short_defense_weakness"], float(home_side and not row.backtest.neutral), float(category == "qualification"), float(category == "nations_league"), float(category == "world_cup"), float(category == "continental_or_other_competitive"))


def _fit_glm(rows: list[FeatureMatch]) -> PoissonGoalModel:
    features, goals = [], []
    for row in rows:
        features.extend((_glm_features(row, True), _glm_features(row, False)))
        goals.extend((row.backtest.home_score, row.backtest.away_score))
    return fit_poisson(features, goals, FEATURES, ridge=1.0)


def predict_candidate(row: FeatureMatch, name: str, competition: dict[str, float], glm: PoissonGoalModel | None) -> tuple[tuple[float, float], tuple[float, float, float]]:
    current = _current_result(row.backtest)
    base = (current["home_xg"], current["away_xg"])
    if name == "current_v421":
        return base, current_prediction(row.backtest)
    if name == "ridge_poisson_glm":
        assert glm is not None
        xg = (glm.predict(_glm_features(row, True)), glm.predict(_glm_features(row, False)))
    else:
        short_weight = .3 if "recency" in name else 0.0
        xg = opponent_adjusted_xg(*base, row.home, row.away, weight=.25, short_weight=short_weight)
        if "separation" in name:
            xg = amplify_goal_difference(*xg, 1.15)
        if "competition" in name:
            multiplier = competition.get(row.category, 1.0)
            xg = (max(.2, min(4.5, xg[0] * multiplier)), max(.2, min(4.5, xg[1] * multiplier)))
    return xg, _preserve_draw_layer(row.backtest, xg)


def _metrics(rows, predictions):
    outcomes = [r.backtest.outcome for r in rows]
    probabilities = [p[1] for p in predictions]
    xgs = [p[0] for p in predictions]
    n = len(rows)
    brier = sum(sum((p[c] - int(y == c)) ** 2 for c in range(3)) for p, y in zip(probabilities, outcomes)) / n
    log_loss = sum(-math.log(max(1e-15, p[y])) for p, y in zip(probabilities, outcomes)) / n
    home_actual = statistics.mean(r.backtest.home_score for r in rows); away_actual = statistics.mean(r.backtest.away_score for r in rows)
    home_pred = statistics.mean(x[0] for x in xgs); away_pred = statistics.mean(x[1] for x in xgs)
    poisson_deviance = 0.0
    for row, xg in zip(rows, xgs):
        for actual, expected in ((row.backtest.home_score, xg[0]), (row.backtest.away_score, xg[1])):
            poisson_deviance += 2 * (actual * math.log(actual / expected) - (actual - expected)) if actual else 2 * expected
    confidence = {label: 0 for label in ("below_40", "40_50", "50_60", "60_70", "70_80", "above_80")}
    confidence_wins = {key: 0 for key in confidence}
    for p, y in zip(probabilities, outcomes):
        top = max(p); key = "below_40" if top < .4 else "40_50" if top < .5 else "50_60" if top < .6 else "60_70" if top < .7 else "70_80" if top < .8 else "above_80"
        confidence[key] += 1; confidence_wins[key] += int(max(range(3), key=lambda c: p[c]) == y)
    gaps = [abs(h-a) for h,a in xgs]
    predicted_goal_counts = {str(goal): 0.0 for goal in range(4)} | {"4_plus": 0.0}
    observed_goal_counts = {str(goal): 0 for goal in range(4)} | {"4_plus": 0}
    predicted_margins = {"underdog_by_2_plus": 0.0, "underdog_by_1": 0.0, "draw": 0.0, "favorite_by_1": 0.0, "favorite_by_2_plus": 0.0}
    observed_margins = {key: 0 for key in predicted_margins}
    for row, (home_xg, away_xg) in zip(rows, xgs):
        for actual, expected in ((row.backtest.home_score, home_xg), (row.backtest.away_score, away_xg)):
            key = str(actual) if actual < 4 else "4_plus"; observed_goal_counts[key] += 1
            probabilities_goal = [math.exp(-expected) * expected**goal / math.factorial(goal) for goal in range(4)]
            for goal, value in enumerate(probabilities_goal): predicted_goal_counts[str(goal)] += value
            predicted_goal_counts["4_plus"] += 1 - sum(probabilities_goal)
        favorite_home = home_xg >= away_xg
        actual_margin = (row.backtest.home_score - row.backtest.away_score) * (1 if favorite_home else -1)
        observed_key = "favorite_by_2_plus" if actual_margin >= 2 else "favorite_by_1" if actual_margin == 1 else "draw" if actual_margin == 0 else "underdog_by_1" if actual_margin == -1 else "underdog_by_2_plus"
        observed_margins[observed_key] += 1
        for home_goals in range(9):
            for away_goals in range(9):
                value = math.exp(-home_xg) * home_xg**home_goals / math.factorial(home_goals) * math.exp(-away_xg) * away_xg**away_goals / math.factorial(away_goals)
                margin = (home_goals-away_goals)*(1 if favorite_home else -1)
                key = "favorite_by_2_plus" if margin >= 2 else "favorite_by_1" if margin == 1 else "draw" if margin == 0 else "underdog_by_1" if margin == -1 else "underdog_by_2_plus"
                predicted_margins[key] += value
    def group_metrics(selected):
        if not selected: return None
        indices=[i for i,row in enumerate(rows) if row in selected]
        return {"matches":len(indices),"brier":sum(sum((probabilities[i][c]-int(outcomes[i]==c))**2 for c in range(3)) for i in indices)/len(indices),"log_loss":sum(-math.log(max(1e-15,probabilities[i][outcomes[i]])) for i in indices)/len(indices)}
    elo_edges=[abs(math.log(_current_result(row.backtest)["home_xg"]/_current_result(row.backtest)["away_xg"])*400) for row in rows]
    return {"matches": n, "multiclass_brier": brier, "log_loss": log_loss, "accuracy": sum(max(range(3), key=lambda c: p[c]) == y for p,y in zip(probabilities,outcomes))/n, "mean_home_xg": home_pred, "actual_home_goals": home_actual, "mean_away_xg": away_pred, "actual_away_goals": away_actual, "home_goal_mae": statistics.mean(abs(r.backtest.home_score-x[0]) for r,x in zip(rows,xgs)), "away_goal_mae": statistics.mean(abs(r.backtest.away_score-x[1]) for r,x in zip(rows,xgs)), "total_goal_error": home_pred+away_pred-home_actual-away_actual, "goal_difference_error": home_pred-away_pred-home_actual+away_actual, "poisson_deviance_per_team": poisson_deviance/(2*n), "mean_max_probability": statistics.mean(max(p) for p in probabilities), "predicted_class_rates":{"home":statistics.mean(p[0] for p in probabilities),"draw":statistics.mean(p[1] for p in probabilities),"away":statistics.mean(p[2] for p in probabilities)},"actual_class_rates":{"home":outcomes.count(0)/n,"draw":outcomes.count(1)/n,"away":outcomes.count(2)/n}, "confidence_buckets": {key:{"matches":count,"accuracy":confidence_wins[key]/count if count else None} for key,count in confidence.items()}, "xg_difference_distribution": {"below_0_10":sum(g<.1 for g in gaps),"below_0_25":sum(g<.25 for g in gaps),"below_0_50":sum(g<.5 for g in gaps),"above_0_75":sum(g>.75 for g in gaps),"above_1_00":sum(g>1 for g in gaps)},"goal_count_rates":{"predicted":{k:v/(2*n) for k,v in predicted_goal_counts.items()},"observed":{k:v/(2*n) for k,v in observed_goal_counts.items()}},"goal_margin_rates":{"predicted":{k:v/n for k,v in predicted_margins.items()},"observed":{k:v/n for k,v in observed_margins.items()}},"segments":{"neutral":group_metrics([r for r in rows if r.backtest.neutral]),"non_neutral":group_metrics([r for r in rows if not r.backtest.neutral]),"close_elo":group_metrics([r for r,e in zip(rows,elo_edges) if e<100]),"moderate_elo":group_metrics([r for r,e in zip(rows,elo_edges) if 100<=e<250]),"strong_favorite":group_metrics([r for r,e in zip(rows,elo_edges) if e>=250]),"by_competition":{category:group_metrics([r for r in rows if r.category==category]) for category in sorted({r.category for r in rows})},"confederation_matchup":None}}


def run():
    rows = build_feature_matches()
    folds = []
    for year in (2022, 2023, 2024):
        training = [r for r in rows if r.played_on.year < year]
        validation = [r for r in rows if r.played_on.year == year]
        competition = _competition_multipliers(training); glm = _fit_glm(training)
        candidate_predictions={name:[predict_candidate(row,name,competition,glm) for row in validation] for name in CANDIDATES}
        fold_metrics = {name:_metrics(validation,candidate_predictions[name]) for name in CANDIDATES}
        examples=[]
        for index,row in sorted(enumerate(validation),key=lambda item:abs(math.log(_current_result(item[1].backtest)["home_xg"]/_current_result(item[1].backtest)["away_xg"])),reverse=True)[:5]:
            examples.append({"date":row.played_on,"home":row.backtest.home_team_id,"away":row.backtest.away_team_id,"score":[row.backtest.home_score,row.backtest.away_score],"current_xg":candidate_predictions["current_v421"][index][0],"ridge_poisson_xg":candidate_predictions["ridge_poisson_glm"][index][0]})
        folds.append({"validation_year":year,"training_matches":len(training),"validation_matches":len(validation),"competition_multipliers":competition,"glm":glm.to_dict(),"metrics":fold_metrics,"representative_largest_predefined_elo_gap_examples":examples})
    aggregate = {name:{metric:statistics.mean(f["metrics"][name][metric] for f in folds) for metric in ("multiclass_brier","log_loss","accuracy","poisson_deviance_per_team","mean_max_probability")} for name in CANDIDATES}
    leading = min(CANDIDATES,key=lambda n:(aggregate[n]["multiclass_brier"],aggregate[n]["log_loss"]))
    final_glm = _fit_glm(rows); final_competition = _competition_multipliers(rows)
    parameter_payload = {"model_version":VERSION,"fitted_through":END.isoformat(),"glm":final_glm.to_dict(),"competition_multipliers":final_competition,"fixed_parameters":{"opponent_adjustment_weight":.25,"short_term_weight":.3,"long_half_life_days":730,"short_half_life_days":180,"small_sample_prior_matches":8,"goal_difference_amplification":1.15,"xg_bounds":[.2,4.5]},"dataset_sha256":hashlib.sha256((ROOT/"data/raw/international_results.csv").read_bytes()).hexdigest()}
    write_json_atomic(PARAMETERS, parameter_payload)
    write_json_atomic(FEATURE_DEFINITIONS,{"model_version":VERSION,"point_in_time_rule":"strictly before match date; same-day updates batched","features":{"elo_side_gap":"chronologically reconstructed Elo gap divided by 400","long_attack":"opponent-adjusted scoring residual, 730-day half-life, eight-match shrinkage prior","opponent_defense_weakness":"opponent-adjusted concession residual with identical shrinkage","short_attack":"180-day attack residual","opponent_short_defense_weakness":"180-day defense residual","home_non_neutral":"one only for non-neutral listed-home team","competition_indicators":"historical categories; coefficients ridge regularized","confederation":"not included because canonical historical confederation metadata is unavailable"},"margin_handling":"log1p absolute margin capped at 2.0 for diagnostics; raw capped goal residuals drive attack/defense histories","xg_bounds":[.2,4.5]})
    report = {"artifact_version":1,"generated_at":datetime.now(timezone.utc).isoformat(),"scope":"pre_2025_only_no_confirmation_holdout_access","candidate_version":VERSION,"current_production_model":"elo-context-v4.2.1","candidate_list":CANDIDATES,"folds":folds,"aggregate":aggregate,"leading_preholdout_candidate":leading,"holdout_availability":{"available":False,"reason":"All completed 2025-2026 matches were previously consumed by v4.3 experiments; no newer completed matches exist in the repository.","future_protocol":"Accumulate completed matches after 2026-06-08 without model changes, then lock stable match IDs before one confirmation run."},"promotion_recommendation":"keep_v4.2.1"}
    write_json_atomic(OUT, report)
    plan={"artifact_version":1,"locked_at":datetime.now(timezone.utc).isoformat(),"model_version":VERSION,"status":"locked_experimental_awaiting_new_data","selected_candidate":leading,"parameters_path":str(PARAMETERS.relative_to(ROOT)),"parameters_sha256":hashlib.sha256(PARAMETERS.read_bytes()).hexdigest(),"preholdout_path":str(OUT.relative_to(ROOT)),"preholdout_sha256":hashlib.sha256(OUT.read_bytes()).hexdigest(),"confirmation_eligibility":{"played_after":"2026-06-08","minimum_matches":150,"match_ids_must_be_locked_before_evaluation":True},"production_gate":"unchanged_from_v4.3.1","promotion_allowed_without_confirmation":False}
    plan["plan_sha256"]=hashlib.sha256(json.dumps(plan,sort_keys=True,separators=(",",":")).encode()).hexdigest(); write_json_atomic(PLAN, plan)
    write_json_atomic(CONFIRMATION_LEDGER,{"model_version":VERSION,"attempts":0,"status":"awaiting_genuinely_untouched_completed_matches","eligible_after":"2026-06-08","minimum_matches":150,"raw_metrics_persisted":False})
    write_json_atomic(READINESS,{"candidate_model_version":VERSION,"current_production_model_version":"elo-context-v4.2.1","gate":{"overall_status":"fail","conditions":[{"name":"new_confirmation_holdout_available","threshold":True,"measured_value":False,"passed":False,"required":True,"explanation":"No genuinely untouched completed matches remain."},{"name":"immutable_raw_confirmation_metrics","threshold":True,"measured_value":False,"passed":False,"required":True,"explanation":"Confirmation has not run."}]},"promotion_recommendation":"keep_current_experimental_candidate"})
    return report


if __name__=="__main__":
    result=run(); print(OUT); print("leading",result["leading_preholdout_candidate"])
