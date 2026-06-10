#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from modeling.src.features.context import HistoricalResult, load_historical_results
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    LEGACY_MODEL_VERSION,
    calculate_poisson_ratings_v1,
)

DEFAULT_START_YEAR = 2022
MINIMUM_PRIOR_MATCHES = 5
REPORT_PATH = ROOT / "data" / "evaluation" / "current_model_latest.json"
ELO_INITIAL = 1500.0
ELO_SCALE = 400.0
MODEL_VERSION = LEGACY_MODEL_VERSION


@dataclass
class TeamHistory:
    elo: float = ELO_INITIAL
    goals_for: int = 0
    goals_against: int = 0
    matches: int = 0
    recent_scores: deque[float] = field(default_factory=lambda: deque(maxlen=5))
    last_played_on: date | None = None

    def rating(self) -> dict[str, float | int]:
        if not self.matches:
            return {
                "elo_rating": self.elo,
                "attack_rating": 50.0,
                "defense_rating": 50.0,
                "form_rating": 50.0,
                "matches_played": 0,
            }
        goals_for_per_match = self.goals_for / self.matches
        goals_against_per_match = self.goals_against / self.matches
        return {
            "elo_rating": round(self.elo, 2),
            "attack_rating": round(min(100.0, 100.0 * goals_for_per_match / 3.0), 2),
            "defense_rating": round(
                max(0.0, 100.0 * (1.0 - goals_against_per_match / 3.0)), 2
            ),
            "form_rating": round(
                100.0 * sum(self.recent_scores) / (3.0 * len(self.recent_scores)), 2
            ),
            "matches_played": self.matches,
        }


@dataclass(frozen=True)
class BacktestPrediction:
    played_on: date
    home_team_id: str
    away_team_id: str
    home_score: int
    away_score: int
    tournament: str
    neutral: bool
    outcome: int
    model: ProbabilityVector
    elo: ProbabilityVector
    no_form: ProbabilityVector
    home_xg: float
    away_xg: float
    home_attack_rating: float
    away_attack_rating: float
    home_defense_rating: float
    away_defense_rating: float
    home_form_rating: float
    away_form_rating: float
    home_rest_days: int | None
    away_rest_days: int | None
    home_player_strength: float | None
    away_player_strength: float | None
    home_travel_km: float | None
    away_travel_km: float | None
    home_availability_adjustment: float | None
    away_availability_adjustment: float | None
    confidence_score: float
    confidence_tier: str
    market: ProbabilityVector | None = None


def load_environment() -> dict[str, str]:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def _outcome(result: HistoricalResult) -> int:
    if result.home_score > result.away_score:
        return 0
    if result.home_score == result.away_score:
        return 1
    return 2


def _confidence_tier(score: float) -> str:
    if score >= 0.7:
        return "High"
    if score >= 0.5:
        return "Medium"
    return "Low"


def _elo_probabilities(home_elo: float, away_elo: float) -> ProbabilityVector:
    gap = home_elo - away_elo
    home_xg = 1.35 * math.exp(gap / 800.0)
    away_xg = 1.35 * math.exp(-gap / 800.0)
    scores = []
    for home_goals in range(7):
        for away_goals in range(7):
            probability = (
                math.exp(-home_xg) * home_xg**home_goals / math.factorial(home_goals)
                * math.exp(-away_xg) * away_xg**away_goals / math.factorial(away_goals)
            )
            scores.append((home_goals, away_goals, probability))
    total = sum(score[2] for score in scores)
    home = sum(score[2] for score in scores if score[0] > score[1]) / total
    draw = sum(score[2] for score in scores if score[0] == score[1]) / total
    return home, draw, 1.0 - home - draw


def _update_histories(
    histories: dict[str, TeamHistory],
    result: HistoricalResult,
) -> None:
    home = histories[result.home_team_id]
    away = histories[result.away_team_id]
    expected_home = 1.0 / (1.0 + 10 ** ((away.elo - home.elo) / ELO_SCALE))
    actual_home = 1.0 if result.home_score > result.away_score else (
        0.5 if result.home_score == result.away_score else 0.0
    )
    margin_multiplier = 1.0 + 0.25 * max(
        0, abs(result.home_score - result.away_score) - 1
    )
    change = 20.0 * margin_multiplier * (actual_home - expected_home)
    home.elo += change
    away.elo -= change

    home.goals_for += result.home_score
    home.goals_against += result.away_score
    away.goals_for += result.away_score
    away.goals_against += result.home_score
    home.matches += 1
    away.matches += 1
    home.last_played_on = result.played_on
    away.last_played_on = result.played_on
    home.recent_scores.append(actual_home * 3.0 if actual_home != 0.5 else 1.0)
    away.recent_scores.append(
        3.0 if actual_home == 0.0 else 1.0 if actual_home == 0.5 else 0.0
    )


def _confidence_accuracy(predictions: list[BacktestPrediction]) -> list[dict[str, Any]]:
    output = []
    for tier in ("Low", "Medium", "High"):
        rows = [prediction for prediction in predictions if prediction.confidence_tier == tier]
        correct = sum(
            max(range(3), key=lambda index: row.model[index]) == row.outcome
            for row in rows
        )
        output.append(
            {
                "tier": tier,
                "matches": len(rows),
                "accuracy": round(correct / len(rows), 6) if rows else None,
                "mean_confidence_score": (
                    round(sum(row.confidence_score for row in rows) / len(rows), 6)
                    if rows else None
                ),
                "mean_top_probability": (
                    round(sum(max(row.model) for row in rows) / len(rows), 6)
                    if rows else None
                ),
            }
        )
    return output


def _comparison(primary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    return {
        "brier_score_delta": round(
            primary["brier_score"] - baseline["brier_score"], 6
        ),
        "log_loss_delta": round(primary["log_loss"] - baseline["log_loss"], 6),
        "accuracy_delta": round(primary["accuracy"] - baseline["accuracy"], 6),
    }


def replay_backtest(
    results: list[HistoricalResult] | None = None,
    start_year: int = DEFAULT_START_YEAR,
    market_probabilities: dict[tuple[date, str, str], ProbabilityVector] | None = None,
) -> tuple[list[BacktestPrediction], list[HistoricalResult]]:
    ordered = sorted(
        results if results is not None else load_historical_results(),
        key=lambda result: (
            result.played_on,
            result.home_team_id,
            result.away_team_id,
        ),
    )
    if not ordered:
        raise ValueError("No historical results are available")

    market_probabilities = market_probabilities or {}
    histories: dict[str, TeamHistory] = defaultdict(TeamHistory)
    by_date: dict[date, list[HistoricalResult]] = defaultdict(list)
    for result in ordered:
        by_date[result.played_on].append(result)

    predictions: list[BacktestPrediction] = []
    for played_on in sorted(by_date):
        day_results = by_date[played_on]
        if played_on.year >= start_year:
            for result in day_results:
                home = histories[result.home_team_id]
                away = histories[result.away_team_id]
                if (
                    home.matches < MINIMUM_PRIOR_MATCHES
                    or away.matches < MINIMUM_PRIOR_MATCHES
                ):
                    continue
                home_rating = home.rating()
                away_rating = away.rating()
                calculated = calculate_poisson_ratings_v1(home_rating, away_rating)
                no_form_home = {**home_rating, "form_rating": 50.0}
                no_form_away = {**away_rating, "form_rating": 50.0}
                no_form_calculated = calculate_poisson_ratings_v1(
                    no_form_home, no_form_away
                )
                model = (
                    calculated["home_win_probability"],
                    calculated["draw_probability"],
                    calculated["away_win_probability"],
                )
                no_form = (
                    no_form_calculated["home_win_probability"],
                    no_form_calculated["draw_probability"],
                    no_form_calculated["away_win_probability"],
                )
                confidence = calculated["confidence_score"]
                predictions.append(
                    BacktestPrediction(
                        played_on=played_on,
                        home_team_id=result.home_team_id,
                        away_team_id=result.away_team_id,
                        home_score=result.home_score,
                        away_score=result.away_score,
                        tournament=result.tournament,
                        neutral=result.neutral,
                        outcome=_outcome(result),
                        model=model,
                        elo=_elo_probabilities(home.elo, away.elo),
                        no_form=no_form,
                        home_xg=calculated["home_xg"],
                        away_xg=calculated["away_xg"],
                        home_attack_rating=float(home_rating["attack_rating"]),
                        away_attack_rating=float(away_rating["attack_rating"]),
                        home_defense_rating=float(home_rating["defense_rating"]),
                        away_defense_rating=float(away_rating["defense_rating"]),
                        home_form_rating=float(home_rating["form_rating"]),
                        away_form_rating=float(away_rating["form_rating"]),
                        home_rest_days=(
                            (played_on - home.last_played_on).days
                            if home.last_played_on else None
                        ),
                        away_rest_days=(
                            (played_on - away.last_played_on).days
                            if away.last_played_on else None
                        ),
                        home_player_strength=None,
                        away_player_strength=None,
                        home_travel_km=None,
                        away_travel_km=None,
                        home_availability_adjustment=None,
                        away_availability_adjustment=None,
                        confidence_score=confidence,
                        confidence_tier=_confidence_tier(confidence),
                        market=market_probabilities.get(
                            (played_on, result.home_team_id, result.away_team_id)
                        ),
                    )
                )
        for result in day_results:
            _update_histories(histories, result)

    if not predictions:
        raise ValueError("No matches met the backtest eligibility requirements")
    return predictions, ordered


def run_backtest(
    results: list[HistoricalResult] | None = None,
    start_year: int = DEFAULT_START_YEAR,
    market_probabilities: dict[tuple[date, str, str], ProbabilityVector] | None = None,
) -> dict[str, Any]:
    predictions, ordered = replay_backtest(
        results=results,
        start_year=start_year,
        market_probabilities=market_probabilities,
    )

    outcomes = [prediction.outcome for prediction in predictions]
    model_metrics = evaluate([prediction.model for prediction in predictions], outcomes)
    elo_metrics = evaluate([prediction.elo for prediction in predictions], outcomes)
    market_rows = [prediction for prediction in predictions if prediction.market is not None]
    market_metrics = (
        evaluate(
            [prediction.market for prediction in market_rows if prediction.market is not None],
            [prediction.outcome for prediction in market_rows],
        )
        if market_rows
        else None
    )
    yearly = {}
    for year in sorted({prediction.played_on.year for prediction in predictions}):
        rows = [prediction for prediction in predictions if prediction.played_on.year == year]
        yearly[str(year)] = {
            "model": evaluate([row.model for row in rows], [row.outcome for row in rows]),
            "elo": evaluate([row.elo for row in rows], [row.outcome for row in rows]),
        }

    return {
        "model_version": MODEL_VERSION,
        "protocol": {
            "method": "walk_forward_replay",
            "start_year": start_year,
            "end_date": max(result.played_on for result in ordered).isoformat(),
            "minimum_prior_matches_per_team": MINIMUM_PRIOR_MATCHES,
            "same_day_updates": "batched_after_predictions",
            "rating_formula": "transparent-v1 from scripts/update_ratings.py",
            "prediction_formula": MODEL_VERSION,
            "player_ratings": "excluded; no historical point-in-time archive",
            "calibration": "one-vs-rest buckets across all three result probabilities",
            "market_odds": (
                "latest complete pre-match bookmaker 1X2 snapshots, "
                "de-vigged then averaged"
            ),
        },
        "model": model_metrics,
        "elo_baseline": elo_metrics,
        "market_baseline": market_metrics,
        "comparison": {
            "model_vs_elo": _comparison(model_metrics, elo_metrics),
            "model_vs_market": (
                _comparison(
                    evaluate(
                        [row.model for row in market_rows],
                        [row.outcome for row in market_rows],
                    ),
                    market_metrics,
                )
                if market_metrics is not None
                else None
            ),
        },
        "confidence_tiers": _confidence_accuracy(predictions),
        "years": yearly,
    }


def _normalize(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _selection_index(selection: Any, home_name: str, away_name: str) -> int | None:
    normalized = _normalize(selection)
    if normalized in {"home", "1", _normalize(home_name)}:
        return 0
    if normalized in {"draw", "x", "tie"}:
        return 1
    if normalized in {"away", "2", _normalize(away_name)}:
        return 2
    return None


class EvaluationRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

    def _table(self, name: str) -> Table:
        if name not in self.tables:
            self.tables[name] = Table(
                name, self.metadata, schema=self.schema, autoload_with=self.engine
            )
        return self.tables[name]

    def assert_schema(self) -> None:
        tables = set(inspect(self.engine).get_table_names(schema=self.schema))
        if "evaluation_results" not in tables:
            raise RuntimeError(
                "evaluation_results is missing. Apply "
                "supabase/migrations/202606100006_model_evaluation.sql first."
            )

    def load_market_probabilities(
        self,
    ) -> dict[tuple[date, str, str], ProbabilityVector]:
        tables = set(inspect(self.engine).get_table_names(schema=self.schema))
        if not {"matches", "teams", "odds_snapshots"}.issubset(tables):
            return {}
        matches = self._table("matches")
        teams = self._table("teams")
        odds = self._table("odds_snapshots")
        required_match_columns = {"id", "home_team_id", "away_team_id"}
        if not required_match_columns.issubset(matches.c.keys()):
            return {}
        date_column = next(
            (matches.c[name] for name in ("match_date", "kickoff") if name in matches.c),
            None,
        )
        if date_column is None or "name" not in teams.c:
            return {}

        with self.engine.connect() as connection:
            team_names = {
                row["id"]: row["name"]
                for row in connection.execute(teams.select()).mappings()
            }
            match_rows = [
                dict(row) for row in connection.execute(matches.select()).mappings()
            ]
            odds_rows = [dict(row) for row in connection.execute(odds.select()).mappings()]

        aliases_payload = json.loads(
            (ROOT / "data" / "seed" / "team_aliases.json").read_text()
        )
        aliases = {
            _normalize(alias): team_id
            for team_id, names in aliases_payload.items()
            for alias in names
        }
        odds_by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for row in odds_rows:
            if _normalize(row.get("market")) in {
                "1x2",
                "matchwinner",
                "fulltimeresult",
                "h2h",
            }:
                odds_by_match[row.get("match_id")].append(row)

        output: dict[tuple[date, str, str], ProbabilityVector] = {}
        for match in match_rows:
            home_name = team_names.get(match.get("home_team_id"))
            away_name = team_names.get(match.get("away_team_id"))
            home_id = aliases.get(_normalize(home_name))
            away_id = aliases.get(_normalize(away_name))
            played_on = _as_date(match.get(date_column.name))
            if not home_id or not away_id or played_on is None:
                continue
            grouped: dict[tuple[str, str], dict[int, float]] = defaultdict(dict)
            for row in odds_by_match.get(match["id"], []):
                captured = _as_datetime(row.get("captured_at"))
                kickoff = _as_datetime(match.get(date_column.name))
                if captured is None or (kickoff is not None and captured >= kickoff):
                    continue
                index = _selection_index(row.get("selection"), home_name, away_name)
                decimal_odds = _positive_number(row.get("decimal_odds"))
                if index is None or decimal_odds is None:
                    continue
                key = (str(row.get("bookmaker") or "unknown"), captured.isoformat())
                grouped[key][index] = 1.0 / decimal_odds
            complete = [
                (key, values)
                for key, values in grouped.items()
                if set(values) == {0, 1, 2}
            ]
            latest_by_bookmaker: dict[str, tuple[str, dict[int, float]]] = {}
            for (bookmaker, captured), values in complete:
                if (
                    bookmaker not in latest_by_bookmaker
                    or captured > latest_by_bookmaker[bookmaker][0]
                ):
                    latest_by_bookmaker[bookmaker] = (captured, values)
            fair = []
            for _, values in latest_by_bookmaker.values():
                total = sum(values.values())
                fair.append(tuple(values[index] / total for index in range(3)))
            if fair:
                output[(played_on, home_id, away_id)] = tuple(
                    sum(row[index] for row in fair) / len(fair) for index in range(3)
                )  # type: ignore[assignment]
        return output

    def store(self, report: dict[str, Any], evaluated_at: datetime) -> Any:
        table = self._table("evaluation_results")
        model = report["model"]
        elo = report["elo_baseline"]
        market = report["market_baseline"]
        values = {
            "id": str(uuid4()),
            "model_version": report["model_version"],
            "evaluated_at": evaluated_at,
            "evaluation_start": date(report["protocol"]["start_year"], 1, 1),
            "evaluation_end": date.fromisoformat(report["protocol"]["end_date"]),
            "match_count": model["matches"],
            "brier_score": model["brier_score"],
            "log_loss": model["log_loss"],
            "accuracy": model["accuracy"],
            "elo_brier_score": elo["brier_score"],
            "elo_log_loss": elo["log_loss"],
            "elo_accuracy": elo["accuracy"],
            "market_match_count": market["matches"] if market else 0,
            "market_brier_score": market["brier_score"] if market else None,
            "market_log_loss": market["log_loss"] if market else None,
            "market_accuracy": market["accuracy"] if market else None,
            "calibration": model["calibration_bins"],
            "confidence_tiers": report["confidence_tiers"],
            "report": report,
            "protocol": report["protocol"],
            "created_at": evaluated_at,
        }
        for key, value in list(values.items()):
            if key in table.c and isinstance(value, (dict, list)) and not isinstance(
                table.c[key].type, JSON
            ):
                values[key] = json.dumps(value)
        values = {key: value for key, value in values.items() if key in table.c}
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))
        return values["id"]


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 1.0 else None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_date(value: Any) -> date | None:
    parsed = _as_datetime(value)
    return parsed.date() if parsed else None


def write_report(report: dict[str, Any], path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest the current match model")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("evaluate_model")
    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        logger.error("[model-evaluation] FAILED: DATABASE_URL is required")
        return 2

    engine = create_database_engine(database_url)
    try:
        repository = EvaluationRepository(engine)
        repository.assert_schema()
        market = repository.load_market_probabilities()
        evaluated_at = datetime.now(timezone.utc)
        report = run_backtest(start_year=args.start_year, market_probabilities=market)
        report["evaluated_at"] = evaluated_at.isoformat()
        evaluation_id = repository.store(report, evaluated_at)
        write_report(report, args.output)
        logger.info(
            "[model-evaluation] SUCCESS: id=%s matches=%d market_matches=%d",
            evaluation_id,
            report["model"]["matches"],
            report["market_baseline"]["matches"] if report["market_baseline"] else 0,
        )
        return 0
    except Exception:
        logger.exception("[model-evaluation] FAILED")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
