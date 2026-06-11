#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from modeling.src.data import load_teams
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    SHOT_VOLUME_WEIGHT,
    V3_DRAW_MULTIPLIER,
    PredictionRepository,
    _normalize_probabilities,
    calculate_prediction,
    canonical_prior_elo,
    load_environment,
)
from scripts.validate_calibrated_v2 import chronological_split
from scripts.validate_xg_proxy_v4 import (
    XgProxyValidationRow,
    _apply_signal,
    build_validation_rows,
    load_database_matches,
)

REPORT_PATH = (
    ROOT / "data" / "evaluation" / "mexico_south_africa_disagreement.json"
)
TEAM_IDS = ("MEX", "RSA")
MARKET = (0.6717002554906989, 0.21600719791535955, 0.1122925465939416)
BLENDS = {
    "db_elo_only": 0.0,
    "canonical_prior_only": 1.0,
    "canonical_70_db_30": 0.7,
    "canonical_50_db_50": 0.5,
}


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _source_matches(
    engine: Engine,
    database_team_ids: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    schema = None if engine.dialect.name == "sqlite" else "public"
    metadata = MetaData()
    matches = Table("matches", metadata, schema=schema, autoload_with=engine)
    stats = Table(
        "team_match_stats", metadata, schema=schema, autoload_with=engine
    )
    target = {
        database_team_ids[team_id]: team_id
        for team_id in TEAM_IDS
    }
    with engine.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                select(
                    matches.c.id,
                    matches.c.match_date,
                    matches.c.home_team,
                    matches.c.away_team,
                    matches.c.home_score,
                    matches.c.away_score,
                    matches.c.tournament_stage,
                    matches.c.provider_name,
                    matches.c.provider_payload,
                    stats.c.team_id,
                    stats.c.is_home,
                    stats.c.goals,
                    stats.c.shots,
                    stats.c.shots_on_target,
                    stats.c.source_name,
                    stats.c.source_match_key,
                )
                .join(stats, matches.c.id == stats.c.match_id)
                .where(stats.c.team_id.in_(target))
                .order_by(stats.c.team_id, matches.c.match_date)
            ).mappings()
        ]
    output: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        payload = row.pop("provider_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        league = payload.get("league", {}) if isinstance(payload, dict) else {}
        team_id = target[row.pop("team_id")]
        output[team_id].append(
            {
                **{key: _json_value(value) for key, value in row.items()},
                "competition": league.get("name") or row["tournament_stage"],
            }
        )
    return dict(output)


def _rating_payload(rating: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": rating["_rating_source"],
        "elo": float(rating["elo_rating"]),
        "attack_rating": float(rating["attack_rating"]),
        "defense_rating": float(rating["defense_rating"]),
        "form_rating": float(rating["form_rating"]),
        "matches_played": int(rating["matches_played"]),
        "goals_for": int(rating["goals_for"]),
        "goals_against": int(rating["goals_against"]),
        "rated_at": _json_value(rating.get("rated_at")),
        "formula_version": (rating.get("components") or {}).get(
            "formula_version"
        ),
    }


def _fixture_prediction(
    ratings: dict[str, dict[str, Any]],
    shot_volume: dict[str, dict[str, Any]],
    latest_dates: dict[str, date],
    canonical_weight: float,
) -> dict[str, Any]:
    kickoff = date(2026, 6, 11)
    blended = {}
    ranks = {"MEX": 15, "RSA": 61}
    for team_id in TEAM_IDS:
        db_elo = float(ratings[team_id]["elo_rating"])
        prior = canonical_prior_elo(ranks[team_id])
        blended[team_id] = {
            **ratings[team_id],
            "elo_rating": canonical_weight * prior
            + (1.0 - canonical_weight) * db_elo,
        }
    result = calculate_prediction(
        blended["MEX"],
        blended["RSA"],
        home_rest_days=(kickoff - latest_dates["MEX"]).days,
        away_rest_days=(kickoff - latest_dates["RSA"]).days,
        home_team_name="Mexico",
        away_team_name="South Africa",
        home_shot_volume_rating=shot_volume["MEX"]["shot_volume_rating"],
        away_shot_volume_rating=shot_volume["RSA"]["shot_volume_rating"],
    )
    return {
        "canonical_weight": canonical_weight,
        "effective_elo": {
            team_id: round(float(blended[team_id]["elo_rating"]), 4)
            for team_id in TEAM_IDS
        },
        "effective_elo_gap_mexico_minus_south_africa": round(
            float(blended["MEX"]["elo_rating"])
            - float(blended["RSA"]["elo_rating"]),
            4,
        ),
        "probability": {
            "mexico": result["final_home_probability"],
            "draw": result["final_draw_probability"],
            "south_africa": result["final_away_probability"],
        },
        "components": {
            key: result[key]
            for key in (
                "elo_base_home_probability",
                "elo_base_draw_probability",
                "elo_base_away_probability",
                "attack_defense_adjustment",
                "draw_calibration_adjustment",
                "context_adjustment_total",
            )
        },
    }


def _pregame_elo_gaps(
    matches: list[dict[str, Any]],
) -> dict[Any, float]:
    ratings: dict[Any, float] = defaultdict(lambda: 1500.0)
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for match in matches:
        by_date[match["played_on"]].append(match)
    gaps = {}
    for played_on in sorted(by_date):
        day_matches = sorted(
            by_date[played_on], key=lambda match: str(match["match_id"])
        )
        for match in day_matches:
            gaps[match["match_id"]] = (
                ratings[match["home_team_id"]]
                - ratings[match["away_team_id"]]
            )
        for match in day_matches:
            home_id = match["home_team_id"]
            away_id = match["away_team_id"]
            expected_home = 1.0 / (
                1.0
                + 10
                ** ((ratings[away_id] - ratings[home_id]) / 400.0)
            )
            actual_home = (
                1.0
                if match["home_goals"] > match["away_goals"]
                else 0.5
                if match["home_goals"] == match["away_goals"]
                else 0.0
            )
            margin = 1.0 + 0.25 * max(
                0, abs(match["home_goals"] - match["away_goals"]) - 1
            )
            change = 20.0 * margin * (actual_home - expected_home)
            ratings[home_id] += change
            ratings[away_id] -= change
    return gaps


def _elo_probabilities(gap: float) -> ProbabilityVector:
    home_xg = 1.35 * math.exp(gap / 800.0)
    away_xg = 1.35 * math.exp(-gap / 800.0)
    scores = []
    for home_goals in range(7):
        for away_goals in range(7):
            probability = (
                math.exp(-home_xg)
                * home_xg**home_goals
                / math.factorial(home_goals)
                * math.exp(-away_xg)
                * away_xg**away_goals
                / math.factorial(away_goals)
            )
            scores.append((home_goals, away_goals, probability))
    total = sum(row[2] for row in scores)
    home = sum(row[2] for row in scores if row[0] > row[1]) / total
    draw = sum(row[2] for row in scores if row[0] == row[1]) / total
    return home, draw, 1.0 - home - draw


def _context_tilt(row: XgProxyValidationRow, db_gap: float) -> float:
    base = _elo_probabilities(db_gap)
    return 0.5 * math.log(
        (row.v3[0] / row.v3[2]) / (base[0] / base[2])
    )


def _blend_probability(
    row: XgProxyValidationRow,
    db_gap: float,
    prior_gap: float,
    canonical_weight: float,
) -> ProbabilityVector:
    gap = canonical_weight * prior_gap + (1.0 - canonical_weight) * db_gap
    base = _elo_probabilities(gap)
    tilt = _context_tilt(row, db_gap)
    v3 = _normalize_probabilities(
        (
            base[0] * math.exp(tilt),
            base[1] * V3_DRAW_MULTIPLIER,
            base[2] * math.exp(-tilt),
        )
    )
    return _apply_signal(v3, row.shot_volume_signal, SHOT_VOLUME_WEIGHT)


def _holdout_evaluation(
    engine: Engine,
    database_team_ids: dict[str, Any],
) -> dict[str, Any]:
    matches, coverage = load_database_matches(engine)
    validation_rows = build_validation_rows(matches)
    _, holdout = chronological_split(validation_rows, 0.22)
    gaps = _pregame_elo_gaps(matches)
    ranks = {team.id: team.rank for team in load_teams()}
    database_priors = {
        database_team_ids[team_id]: canonical_prior_elo(rank)
        for team_id, rank in ranks.items()
        if database_team_ids.get(team_id) is not None
    }
    eligible = [
        row
        for row in holdout
        if row.home_team_id in database_priors
        and row.away_team_id in database_priors
        and row.match_id in gaps
    ]
    time_aligned = [
        row for row in eligible if row.played_on >= date(2025, 12, 1)
    ]

    def metrics(rows: list[XgProxyValidationRow], weight: float) -> dict[str, Any]:
        probabilities = [
            _blend_probability(
                row,
                gaps[row.match_id],
                database_priors[row.home_team_id]
                - database_priors[row.away_team_id],
                weight,
            )
            for row in rows
        ]
        return evaluate(probabilities, [row.outcome for row in rows])

    return {
        "protocol": {
            "base": "existing xg-proxy-v4 chronological holdout",
            "holdout_start": min(row.played_on for row in holdout).isoformat(),
            "holdout_end": max(row.played_on for row in holdout).isoformat(),
            "all_holdout_matches": len(holdout),
            "canonical_prior_eligible_matches": len(eligible),
            "time_aligned_matches_after_2025_12_01": len(time_aligned),
            "features_held_constant": [
                "attack_rating",
                "defense_rating",
                "rest",
                "draw_multiplier",
                "shot_volume_rating",
            ],
            "important_limitation": (
                "The canonical priors use the repo's November 2025 FIFA ranks. "
                "Results before December 2025 are look-ahead diagnostics and "
                "must not be used for production promotion."
            ),
            "source_coverage": coverage,
        },
        "lookahead_diagnostic_all_eligible_holdout": {
            name: metrics(eligible, weight) for name, weight in BLENDS.items()
        },
        "time_aligned_holdout": {
            name: metrics(time_aligned, weight) for name, weight in BLENDS.items()
        }
        if time_aligned
        else {},
    }


def _historical_summary() -> dict[str, Any]:
    results = []
    with (
        ROOT / "data" / "raw" / "international_results.csv"
    ).open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            if not row["home_score"].isdigit() or not row["away_score"].isdigit():
                continue
            results.append(
                {
                    "played_on": date.fromisoformat(row["date"]),
                    "home_team": row["home_team"].strip(),
                    "away_team": row["away_team"].strip(),
                    "home_score": int(row["home_score"]),
                    "away_score": int(row["away_score"]),
                    "tournament": row["tournament"].strip(),
                }
            )
    output = {}
    for team_id in TEAM_IDS:
        team_name = "Mexico" if team_id == "MEX" else "South Africa"
        world_cup = [
            row
            for row in results
            if team_name in (row["home_team"], row["away_team"])
            and row["tournament"] == "FIFA World Cup"
        ]
        continental_names = (
            {"Gold Cup", "CONCACAF Gold Cup", "Copa América"}
            if team_id == "MEX"
            else {"African Cup of Nations"}
        )
        continental = [
            row
            for row in results
            if team_name in (row["home_team"], row["away_team"])
            and row["tournament"] in continental_names
        ]

        def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
            wins = draws = losses = goals_for = goals_against = 0
            for row in rows:
                home = row["home_team"] == team_name
                gf = row["home_score"] if home else row["away_score"]
                ga = row["away_score"] if home else row["home_score"]
                goals_for += gf
                goals_against += ga
                wins += gf > ga
                draws += gf == ga
                losses += gf < ga
            return {
                "matches": len(rows),
                "wins": wins,
                "draws": draws,
                "losses": losses,
                "goals_for": goals_for,
                "goals_against": goals_against,
                "first_date": min(
                    (row["played_on"] for row in rows), default=None
                ),
                "last_date": max(
                    (row["played_on"] for row in rows), default=None
                ),
            }

        output[team_id] = {
            "world_cup": {
                key: _json_value(value)
                for key, value in summarize(world_cup).items()
            },
            "continental": {
                key: _json_value(value)
                for key, value in summarize(continental).items()
            },
        }
    return output


def _market_target(
    ratings: dict[str, dict[str, Any]],
    shot_volume: dict[str, dict[str, Any]],
    latest_dates: dict[str, date],
) -> dict[str, Any]:
    best_gap = None
    best_error = float("inf")
    best_probability = None
    for gap in range(-500, 501):
        synthetic = {
            "MEX": {**ratings["MEX"], "elo_rating": 1500.0 + gap / 2},
            "RSA": {**ratings["RSA"], "elo_rating": 1500.0 - gap / 2},
        }
        result = calculate_prediction(
            synthetic["MEX"],
            synthetic["RSA"],
            home_rest_days=(date(2026, 6, 11) - latest_dates["MEX"]).days,
            away_rest_days=(date(2026, 6, 11) - latest_dates["RSA"]).days,
            home_team_name="Mexico",
            away_team_name="South Africa",
            home_shot_volume_rating=shot_volume["MEX"]["shot_volume_rating"],
            away_shot_volume_rating=shot_volume["RSA"]["shot_volume_rating"],
        )
        probability = (
            result["final_home_probability"],
            result["final_draw_probability"],
            result["final_away_probability"],
        )
        error = sum((actual - target) ** 2 for actual, target in zip(probability, MARKET))
        if error < best_error:
            best_gap = gap
            best_error = error
            best_probability = probability
    return {
        "purpose": "diagnostic comparison only; not a training target",
        "market_probability": {
            "mexico": MARKET[0],
            "draw": MARKET[1],
            "south_africa": MARKET[2],
        },
        "elo_gap_needed_with_other_live_features_held_constant": best_gap,
        "closest_model_probability": {
            "mexico": best_probability[0],
            "draw": best_probability[1],
            "south_africa": best_probability[2],
        },
        "squared_error": best_error,
    }


def build_report(engine: Engine) -> dict[str, Any]:
    repository = PredictionRepository(engine)
    database_team_ids = repository.load_database_team_ids()
    ratings = repository.load_current_team_ratings(database_team_ids)
    shot_volume = repository.load_current_shot_volume_details(database_team_ids)
    latest_dates = repository.load_latest_team_match_dates(database_team_ids)
    sources = _source_matches(engine, database_team_ids)
    fixture_alternatives = {
        name: _fixture_prediction(
            ratings, shot_volume, latest_dates, canonical_weight
        )
        for name, canonical_weight in BLENDS.items()
    }
    canonical = {
        "MEX": canonical_prior_elo(15),
        "RSA": canonical_prior_elo(61),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": {
            "canonical_match_id": "WC26-001",
            "home": "Mexico",
            "away": "South Africa",
            "kickoff_date": "2026-06-11",
        },
        "production_changed": False,
        "market_used_for_training": False,
        "executive_conclusion": (
            "The disagreement is caused primarily by sparse and asymmetric "
            "database match coverage, unshrunk small-sample goal-rate features, "
            "and an uncapped rest feature. South Africa is represented by recent "
            "qualifiers while Mexico's database series stops in July 2024."
        ),
        "input_audit": {
            team_id: {
                "database_team_id": str(database_team_ids[team_id]),
                "team_rating": _rating_payload(ratings[team_id]),
                "canonical_fifa_rank": 15 if team_id == "MEX" else 61,
                "canonical_prior_elo": canonical[team_id],
                "shot_volume": {
                    key: _json_value(value)
                    for key, value in shot_volume[team_id].items()
                },
                "latest_database_match_date": latest_dates[team_id].isoformat(),
                "rest_days_to_fixture": (
                    date(2026, 6, 11) - latest_dates[team_id]
                ).days,
                "source_matches": sources[team_id],
            }
            for team_id in TEAM_IDS
        },
        "rating_explanation": {
            "database_elo_gap_mexico_minus_south_africa": round(
                float(ratings["MEX"]["elo_rating"])
                - float(ratings["RSA"]["elo_rating"]),
                2,
            ),
            "canonical_prior_gap_mexico_minus_south_africa": round(
                canonical["MEX"] - canonical["RSA"], 2
            ),
            "why": [
                "Every database team starts at 1500; current FIFA rank is not used when a database rating exists.",
                "The updater processes only matches with complete two-team stat rows and applies K=20 Elo without competition, venue, or opponent-coverage correction.",
                "Mexico has 10 included matches: the 2018 and 2022 World Cups plus the 2024 Copa America. Its later Gold Cup, Nations League, friendly, and 2026 results are absent.",
                "South Africa has nine included matches, all 2023-2025 African World Cup qualifiers, including wins over Benin, Zimbabwe, Lesotho, and Rwanda.",
                "Attack and defense are raw goals-per-match transforms over these tiny, unequal samples; Mexico's 6 goals in 10 matches becomes attack 20.0, while South Africa's 15 in nine becomes 55.56.",
            ],
        },
        "context_audit": {
            "availability_reports": 0,
            "squad_selection_adjustments": 0,
            "player_ratings_used_in_v4_result_probability": False,
            "rest_signal": {
                "mexico_days": (
                    date(2026, 6, 11) - latest_dates["MEX"]
                ).days,
                "south_africa_days": (
                    date(2026, 6, 11) - latest_dates["RSA"]
                ).days,
                "normalized_signal": 1.0,
                "weight": -0.15,
                "effect": (
                    "The difference is capped at +1 and the negative fitted "
                    "weight penalizes Mexico for having the older/staler last "
                    "match date. This is data staleness masquerading as rest."
                ),
            },
        },
        "historical_performance_in_repo": _historical_summary(),
        "fixture_rating_blends": fixture_alternatives,
        "market_informed_diagnostic": _market_target(
            ratings, shot_volume, latest_dates
        ),
        "historical_holdout": _holdout_evaluation(
            engine, database_team_ids
        ),
        "recommendation": {
            "production_v4_change_recommended": True,
            "preferred_change": "input shrinkage plus freshness gating",
            "rating_prior_blending": (
                "Use canonical-prior blending when database samples are sparse "
                "or stale, with reliability determined from effective sample "
                "size, freshness, competition mix, and connected opponent coverage. "
                "Do not promote a fixed blend from this holdout: only 19 matches "
                "have both priors and only one is time-aligned to the rank snapshot."
            ),
            "attack_defense": (
                "Shrink goal-rate attack/defense toward 50 and use a rolling, "
                "time-decayed, opponent-adjusted window instead of lifetime "
                "provider coverage."
            ),
            "rest": (
                "Cap meaningful rest at a short football window and treat long "
                "gaps as missing/stale data, not additional rest."
            ),
            "shot_volume": (
                "Keep only with sample shrinkage and opponent-strength adjustment; "
                "it currently helps Mexico and is not the cause of this reversal."
            ),
            "market_policy": (
                "Do not train production probabilities on this market snapshot. "
                "Use it only to trigger audits and monitor disagreement."
            ),
        },
    }


def main() -> int:
    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        report = build_report(engine)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, default=_json_value) + "\n"
        )
        print(json.dumps(report["recommendation"], indent=2))
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
