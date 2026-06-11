#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from modeling.src.data import build_fixtures
from scripts.database import create_database_engine
from scripts.market_odds import (
    average_absolute_disagreement,
    disagreement_bucket,
    odds_to_market_probabilities,
)

MODEL_VERSION = "market-comparison-v5-research"
REPORT_PATH = ROOT / "data" / "evaluation" / "market_comparison_latest.json"
REQUIRED_TABLES = {
    "market_odds_snapshots",
    "market_implied_probabilities",
    "market_comparison_reports",
    "predictions",
    "matches",
}
CANONICAL_FIXTURES = {fixture.id: fixture for fixture in build_fixtures()}


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _model_probabilities(row: dict[str, Any]) -> ProbabilityVector | None:
    values = (
        row.get("final_home_probability")
        if row.get("final_home_probability") is not None
        else row.get("home_win_probability"),
        row.get("final_draw_probability")
        if row.get("final_draw_probability") is not None
        else row.get("draw_probability"),
        row.get("final_away_probability")
        if row.get("final_away_probability") is not None
        else row.get("away_win_probability"),
    )
    if any(value is None for value in values):
        return None
    probabilities = tuple(float(value) for value in values)
    if any(value < 0 or value > 1 for value in probabilities):
        return None
    total = sum(probabilities)
    if total <= 0:
        return None
    home = probabilities[0] / total
    draw = probabilities[1] / total
    return home, draw, 1.0 - home - draw


def build_comparison(
    snapshot: dict[str, Any],
    prediction: dict[str, Any],
    outcome: int | None = None,
    kickoff: datetime | None = None,
) -> dict[str, Any] | None:
    canonical_match_id = str(
        snapshot.get("canonical_match_id")
        or prediction.get("canonical_match_id")
        or ""
    )
    canonical_fixture = CANONICAL_FIXTURES.get(canonical_match_id)
    if canonical_fixture is None:
        return None
    expected_home = canonical_fixture.home_team_id
    expected_away = canonical_fixture.away_team_id
    snapshot_home = snapshot.get("canonical_home_team_code")
    snapshot_away = snapshot.get("canonical_away_team_code")
    prediction_home = prediction.get("canonical_home_team_code")
    prediction_away = prediction.get("canonical_away_team_code")
    if (
        snapshot_home != expected_home
        or snapshot_away != expected_away
        or prediction_home != expected_home
        or prediction_away != expected_away
    ):
        return None
    model = _model_probabilities(prediction)
    if model is None:
        return None
    converted = odds_to_market_probabilities(
        snapshot["home_decimal_odds"],
        snapshot["draw_decimal_odds"],
        snapshot["away_decimal_odds"],
    )
    raw = converted["raw"]
    market = converted["devigged"]
    assert isinstance(raw, tuple)
    assert isinstance(market, tuple)
    differences: ProbabilityVector = tuple(
        model[index] - market[index] for index in range(3)
    )  # type: ignore[assignment]
    disagreement = average_absolute_disagreement(model, market)
    collected_at = _as_datetime(snapshot.get("collected_at"))
    prediction_at = _as_datetime(
        prediction.get("prediction_timestamp")
        or prediction.get("data_cutoff")
        or prediction.get("created_at")
    )
    pre_match = (
        outcome is not None
        and kickoff is not None
        and collected_at is not None
        and prediction_at is not None
        and collected_at < kickoff
        and prediction_at < kickoff
    )
    return {
        "snapshot_id": snapshot.get("id"),
        "match_id": snapshot.get("match_id"),
        "canonical_match_id": canonical_match_id,
        "canonical_home_team_code": expected_home,
        "canonical_away_team_code": expected_away,
        "provider_home_team_id": snapshot.get("provider_home_team_id"),
        "provider_away_team_id": snapshot.get("provider_away_team_id"),
        "provider_home_team_name": snapshot.get("provider_home_team_name"),
        "provider_away_team_name": snapshot.get("provider_away_team_name"),
        "provider_fixture_id": snapshot.get("provider_fixture_id"),
        "bookmaker": snapshot.get("bookmaker"),
        "source": snapshot.get("source"),
        "collected_at": collected_at.isoformat() if collected_at else None,
        "model_run_id": prediction.get("model_run_id"),
        "model_version": prediction.get("model_version"),
        "decimal_odds": {
            "home": float(snapshot["home_decimal_odds"]),
            "draw": float(snapshot["draw_decimal_odds"]),
            "away": float(snapshot["away_decimal_odds"]),
        },
        "raw_implied_probabilities": {
            "home": raw[0],
            "draw": raw[1],
            "away": raw[2],
        },
        "overround": float(converted["overround"]),
        "market_probability": {
            "home": market[0],
            "draw": market[1],
            "away": market[2],
        },
        "model_probability": {
            "home": model[0],
            "draw": model[1],
            "away": model[2],
        },
        "model_vs_market_difference": {
            "home": differences[0],
            "draw": differences[1],
            "away": differences[2],
        },
        "average_absolute_disagreement": disagreement,
        "disagreement_bucket": disagreement_bucket(disagreement),
        "outcome": outcome if pre_match else None,
        "historical_validation_eligible": pre_match,
    }


def build_report(comparisons: list[dict[str, Any]], snapshot_count: int) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in comparisons:
        identity = str(
            row.get("canonical_match_id")
            or row.get("match_id")
            or row.get("provider_fixture_id")
        )
        grouped.setdefault(identity, []).append(row)
    consensus_comparisons = []
    for rows in grouped.values():
        first = rows[0]
        market = {
            outcome: sum(row["market_probability"][outcome] for row in rows)
            / len(rows)
            for outcome in ("home", "draw", "away")
        }
        model = first["model_probability"]
        differences = {
            outcome: model[outcome] - market[outcome]
            for outcome in ("home", "draw", "away")
        }
        disagreement = sum(abs(value) for value in differences.values()) / 3.0
        consensus_comparisons.append(
            {
                "match_id": first.get("match_id"),
                "canonical_match_id": first.get("canonical_match_id"),
                "provider_fixture_id": first.get("provider_fixture_id"),
                "bookmakers": sorted(
                    {
                        str(row.get("bookmaker"))
                        for row in rows
                        if row.get("bookmaker")
                    }
                ),
                "bookmaker_count": len(rows),
                "market_probability": market,
                "model_probability": model,
                "model_vs_market_difference": differences,
                "average_absolute_disagreement": disagreement,
                "disagreement_bucket": disagreement_bucket(disagreement),
                "outcome": first.get("outcome"),
                "historical_validation_eligible": all(
                    row.get("historical_validation_eligible") for row in rows
                ),
            }
        )
    consensus_comparisons.sort(
        key=lambda row: str(row.get("canonical_match_id") or row.get("match_id") or "")
    )
    historical = [
        row
        for row in consensus_comparisons
        if row.get("historical_validation_eligible")
        and row.get("outcome") in (0, 1, 2)
    ]
    model_metrics = None
    market_metrics = None
    if historical:
        outcomes = [int(row["outcome"]) for row in historical]
        model_metrics = evaluate(
            [
                (
                    row["model_probability"]["home"],
                    row["model_probability"]["draw"],
                    row["model_probability"]["away"],
                )
                for row in historical
            ],
            outcomes,
        )
        market_metrics = evaluate(
            [
                (
                    row["market_probability"]["home"],
                    row["market_probability"]["draw"],
                    row["market_probability"]["away"],
                )
                for row in historical
            ],
            outcomes,
        )

    buckets = Counter(
        row["disagreement_bucket"] for row in consensus_comparisons
    )
    average_disagreement = (
        sum(
            row["average_absolute_disagreement"]
            for row in consensus_comparisons
        )
        / len(consensus_comparisons)
        if consensus_comparisons
        else None
    )
    status = (
        "historical_validation_complete"
        if historical
        else "current_comparison_only"
        if consensus_comparisons
        else "insufficient_coverage"
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "status": status,
        "purpose": "model evaluation and market benchmarking only",
        "production_predictions_changed": False,
        "production_simulation_changed": False,
        "coverage": {
            "market_snapshots": snapshot_count,
            "snapshots_with_model_comparison": len(comparisons),
            "comparison_matches": len(consensus_comparisons),
            "historical_validation_matches": len(historical),
            "historical_validation_available": bool(historical),
        },
        "model_metrics": model_metrics,
        "market_metrics": market_metrics,
        "model_vs_market": (
            {
                "brier_score_delta": round(
                    model_metrics["brier_score"] - market_metrics["brier_score"],
                    6,
                ),
                "log_loss_delta": round(
                    model_metrics["log_loss"] - market_metrics["log_loss"],
                    6,
                ),
            }
            if model_metrics and market_metrics
            else None
        ),
        "average_absolute_disagreement": (
            round(average_disagreement, 6)
            if average_disagreement is not None
            else None
        ),
        "disagreement_buckets": {
            name: buckets.get(name, 0)
            for name in ("0-2%", "2-5%", "5-10%", "10%+")
        },
        "calibration": {
            "model": model_metrics["calibration_bins"] if model_metrics else None,
            "market": market_metrics["calibration_bins"] if market_metrics else None,
        },
        "validation_note": (
            "Historical outperformance is not evaluated because complete "
            "pre-match odds and model snapshots are unavailable."
            if not historical
            else "Metrics use only complete pre-kickoff model and market snapshots."
        ),
        "sample_comparisons": consensus_comparisons[:10],
        "sample_bookmaker_snapshots": comparisons[:10],
    }


class MarketComparisonRepository:
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
        missing = REQUIRED_TABLES - tables
        if missing:
            raise RuntimeError(
                f"Market v5 tables are missing: {sorted(missing)}. Apply "
                "supabase/migrations/202606110007_market_comparison_v5.sql first."
            )

    def load_comparisons(self) -> tuple[list[dict[str, Any]], int]:
        snapshots_table = self._table("market_odds_snapshots")
        predictions_table = self._table("predictions")
        matches_table = self._table("matches")
        with self.engine.connect() as connection:
            snapshots = [
                dict(row)
                for row in connection.execute(select(snapshots_table)).mappings()
            ]
            predictions = [
                dict(row)
                for row in connection.execute(select(predictions_table)).mappings()
            ]
            matches = [
                dict(row)
                for row in connection.execute(select(matches_table)).mappings()
            ]

        predictions_by_canonical: dict[str, list[dict[str, Any]]] = {}
        predictions_by_match: dict[Any, list[dict[str, Any]]] = {}
        for row in predictions:
            if row.get("canonical_match_id"):
                canonical_fixture = CANONICAL_FIXTURES.get(
                    str(row["canonical_match_id"])
                )
                if canonical_fixture is not None:
                    row["canonical_home_team_code"] = (
                        canonical_fixture.home_team_id
                    )
                    row["canonical_away_team_code"] = (
                        canonical_fixture.away_team_id
                    )
                predictions_by_canonical.setdefault(
                    str(row["canonical_match_id"]), []
                ).append(row)
            if row.get("match_id") is not None:
                predictions_by_match.setdefault(row["match_id"], []).append(row)
        matches_by_id = {row.get("id"): row for row in matches}

        output = []
        for snapshot in snapshots:
            candidates = predictions_by_canonical.get(
                str(snapshot.get("canonical_match_id")), []
            ) or predictions_by_match.get(snapshot.get("match_id"), [])
            if not candidates:
                continue
            prediction = max(
                candidates,
                key=lambda row: _as_datetime(
                    row.get("prediction_timestamp")
                    or row.get("data_cutoff")
                    or row.get("created_at")
                )
                or datetime.min.replace(tzinfo=timezone.utc),
            )
            match = matches_by_id.get(snapshot.get("match_id"), {})
            kickoff = _as_datetime(
                match.get("kickoff") or match.get("match_date")
            )
            home_score = match.get("home_score")
            away_score = match.get("away_score")
            outcome = None
            if home_score is not None and away_score is not None:
                outcome = (
                    0
                    if int(home_score) > int(away_score)
                    else 1
                    if int(home_score) == int(away_score)
                    else 2
                )
            comparison = build_comparison(
                snapshot,
                prediction,
                outcome=outcome,
                kickoff=kickoff,
            )
            if comparison:
                output.append(comparison)
        output.sort(
            key=lambda row: (
                str(row.get("canonical_match_id") or ""),
                str(row.get("bookmaker") or ""),
                str(row.get("collected_at") or ""),
            )
        )
        return output, len(snapshots)

    def store_comparisons(self, comparisons: list[dict[str, Any]]) -> int:
        table = self._table("market_implied_probabilities")
        written = 0
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            for row in comparisons:
                existing = connection.execute(
                    select(table.c.id).where(
                        table.c.snapshot_id == row["snapshot_id"],
                        table.c.model_run_id == row.get("model_run_id"),
                    )
                ).first()
                if existing:
                    continue
                raw = row["raw_implied_probabilities"]
                market = row["market_probability"]
                model = row["model_probability"]
                difference = row["model_vs_market_difference"]
                values = {
                    "snapshot_id": row["snapshot_id"],
                    "match_id": row.get("match_id"),
                    "canonical_match_id": row.get("canonical_match_id"),
                    "model_run_id": row.get("model_run_id"),
                    "model_version": row.get("model_version"),
                    "calculated_at": now,
                    "raw_home_probability": raw["home"],
                    "raw_draw_probability": raw["draw"],
                    "raw_away_probability": raw["away"],
                    "overround": row["overround"],
                    "devig_home_probability": market["home"],
                    "devig_draw_probability": market["draw"],
                    "devig_away_probability": market["away"],
                    "model_home_probability": model["home"],
                    "model_draw_probability": model["draw"],
                    "model_away_probability": model["away"],
                    "home_probability_difference": difference["home"],
                    "draw_probability_difference": difference["draw"],
                    "away_probability_difference": difference["away"],
                    "average_absolute_disagreement": row[
                        "average_absolute_disagreement"
                    ],
                    "disagreement_bucket": row["disagreement_bucket"],
                }
                connection.execute(
                    table.insert().values(
                        **{
                            key: value
                            for key, value in values.items()
                            if key in table.c
                        }
                    )
                )
                written += 1
        return written

    def store_report(self, report: dict[str, Any]) -> None:
        table = self._table("market_comparison_reports")
        model_metrics = report["model_metrics"]
        market_metrics = report["market_metrics"]
        json_safe_report = json.loads(json.dumps(report, default=str))
        values = {
            "model_version": MODEL_VERSION,
            "generated_at": _as_datetime(report["generated_at"]),
            "status": report["status"],
            "snapshot_count": report["coverage"]["market_snapshots"],
            "comparison_match_count": report["coverage"][
                "comparison_matches"
            ],
            "historical_match_count": report["coverage"][
                "historical_validation_matches"
            ],
            "model_brier_score": (
                model_metrics["brier_score"] if model_metrics else None
            ),
            "model_log_loss": model_metrics["log_loss"] if model_metrics else None,
            "market_brier_score": (
                market_metrics["brier_score"] if market_metrics else None
            ),
            "market_log_loss": (
                market_metrics["log_loss"] if market_metrics else None
            ),
            "average_absolute_disagreement": report[
                "average_absolute_disagreement"
            ],
            "disagreement_buckets": report["disagreement_buckets"],
            "calibration": report["calibration"],
            "coverage": report["coverage"],
            "report": json_safe_report,
        }
        for key, value in list(values.items()):
            if (
                key in table.c
                and isinstance(value, (dict, list))
                and not isinstance(table.c[key].type, JSON)
            ):
                values[key] = json.dumps(value)
        with self.engine.begin() as connection:
            connection.execute(
                table.insert().values(
                    **{
                        key: value
                        for key, value in values.items()
                        if key in table.c
                    }
                )
            )


def print_summary(report: dict[str, Any], rows_written: int) -> None:
    coverage = report["coverage"]
    print("Market comparison v5 research")
    print(
        "Coverage: "
        f"snapshots={coverage['market_snapshots']} "
        f"comparison_matches={coverage['comparison_matches']} "
        f"historical={coverage['historical_validation_matches']}"
    )
    print(
        "Average absolute disagreement: "
        f"{report['average_absolute_disagreement']:.4%}"
        if report["average_absolute_disagreement"] is not None
        else "Average absolute disagreement: unavailable"
    )
    print(f"Implied probability rows written: {rows_written}")
    print(f"Validation status: {report['status']}")


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        report = build_report([], 0)
        rows_written = 0
    else:
        engine = create_database_engine(database_url)
        try:
            repository = MarketComparisonRepository(engine)
            repository.assert_schema()
            comparisons, snapshot_count = repository.load_comparisons()
            rows_written = repository.store_comparisons(comparisons)
            report = build_report(comparisons, snapshot_count)
            repository.store_report(report)
        finally:
            engine.dispose()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print_summary(report, rows_written)
    print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
