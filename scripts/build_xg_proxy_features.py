#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.database import create_database_engine

MODEL_VERSION = "xg-proxy-v4"
ROLLING_MATCHES = 10
REQUIRED_STAT_COLUMNS = {
    "match_id",
    "team_id",
    "shots",
    "shots_on_target",
    "shots_inside_box",
    "shots_outside_box",
    "blocked_shots",
    "goalkeeper_saves",
    "corners",
    "possession",
    "passes_attempted",
    "passes_completed",
    "pass_accuracy",
}


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
    return max(minimum, min(maximum, value))


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _mean(values: list[float], default: float) -> float:
    return sum(values) / len(values) if values else default


def _pass_accuracy(row: dict[str, Any]) -> float | None:
    direct = _number(row.get("pass_accuracy"))
    if direct is not None:
        return direct
    attempted = _number(row.get("passes_attempted"))
    completed = _number(row.get("passes_completed"))
    if attempted and completed is not None:
        return 100.0 * completed / attempted
    return None


def calculate_chance_quality_rating(
    rows: list[dict[str, Any]],
    rolling_matches: int = ROLLING_MATCHES,
) -> dict[str, Any]:
    recent = rows[-rolling_matches:]
    shots = sum(_number(row.get("shots")) or 0.0 for row in recent)
    shots_on_target = sum(
        _number(row.get("shots_on_target")) or 0.0 for row in recent
    )
    shots_inside_box = sum(
        _number(row.get("shots_inside_box")) or 0.0 for row in recent
    )
    blocked_shots = sum(
        _number(row.get("blocked_shots")) or 0.0 for row in recent
    )
    matches = max(1, len(recent))
    shots_per_match = shots / matches
    box_shot_rate = _ratio(shots_inside_box, shots)
    shots_on_target_rate = _ratio(shots_on_target, shots)
    blocked_rate = _ratio(blocked_shots, shots)
    corners_per_match = sum(
        _number(row.get("corners")) or 0.0 for row in recent
    ) / matches
    possession = _mean(
        [
            value
            for row in recent
            if (value := _number(row.get("possession"))) is not None
        ],
        50.0,
    )
    pass_accuracy = _mean(
        [
            value
            for row in recent
            if (value := _pass_accuracy(row)) is not None
        ],
        75.0,
    )
    opponent_shots = sum(
        _number(row.get("opponent_shots")) or 0.0 for row in recent
    ) / matches
    opponent_sot = sum(
        _number(row.get("opponent_shots_on_target")) or 0.0
        for row in recent
    ) / matches
    goalkeeper_saves = sum(
        _number(row.get("goalkeeper_saves")) or 0.0 for row in recent
    ) / matches

    shot_volume_rating = _clamp(shots_per_match / 16.0 * 100.0)
    shot_quality_proxy = _clamp(
        100.0
        * (
            0.45 * min(1.0, shots_on_target_rate / 0.45)
            + 0.35 * min(1.0, box_shot_rate / 0.75)
            + 0.20 * min(1.0, (1.0 - blocked_rate) / 0.80)
        )
    )
    chance_creation_rating = _clamp(
        100.0
        * (
            0.35 * min(1.0, shots_per_match / 16.0)
            + 0.20 * min(1.0, corners_per_match / 8.0)
            + 0.20 * min(1.0, possession / 65.0)
            + 0.25 * min(1.0, pass_accuracy / 90.0)
        )
    )
    defensive_shot_suppression = _clamp(
        100.0 * (1.0 - opponent_shots / 18.0)
    )
    keeper_pressure_allowed = _clamp(
        (0.70 * opponent_sot + 0.30 * goalkeeper_saves) / 8.0 * 100.0
    )

    return {
        "sample_matches": len(recent),
        "shot_volume_rating": round(shot_volume_rating, 4),
        "shot_quality_proxy": round(shot_quality_proxy, 4),
        "box_shot_rate": round(box_shot_rate, 6),
        "shots_on_target_rate": round(shots_on_target_rate, 6),
        "chance_creation_rating": round(chance_creation_rating, 4),
        "defensive_shot_suppression": round(
            defensive_shot_suppression, 4
        ),
        "keeper_pressure_allowed": round(keeper_pressure_allowed, 4),
        "components": {
            "rolling_matches": rolling_matches,
            "shots_per_match": round(shots_per_match, 4),
            "corners_per_match": round(corners_per_match, 4),
            "possession": round(possession, 4),
            "pass_accuracy": round(pass_accuracy, 4),
            "blocked_shot_rate": round(blocked_rate, 6),
            "opponent_shots_per_match": round(opponent_shots, 4),
            "opponent_shots_on_target_per_match": round(opponent_sot, 4),
            "goalkeeper_saves_per_match": round(goalkeeper_saves, 4),
        },
    }


def build_team_ratings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_match[row.get("match_id")].append(row)
    enriched = []
    for match_rows in by_match.values():
        if len(match_rows) != 2:
            continue
        first, second = match_rows
        enriched.extend(
            [
                {
                    **first,
                    "opponent_shots": second.get("shots"),
                    "opponent_shots_on_target": second.get("shots_on_target"),
                },
                {
                    **second,
                    "opponent_shots": first.get("shots"),
                    "opponent_shots_on_target": first.get("shots_on_target"),
                },
            ]
        )
    by_team: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in enriched:
        by_team[row["team_id"]].append(row)
    ratings = []
    for team_id, team_rows in by_team.items():
        team_rows.sort(
            key=lambda row: (
                str(row.get("match_date") or row.get("captured_at") or ""),
                str(row.get("match_id")),
            )
        )
        ratings.append(
            {
                "team_id": team_id,
                **calculate_chance_quality_rating(team_rows),
            }
        )
    return ratings


class ChanceQualityRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()

    def _table(self, name: str) -> Table:
        return Table(
            name,
            self.metadata,
            schema=self.schema,
            autoload_with=self.engine,
            extend_existing=True,
        )

    def assert_schema(self) -> None:
        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names(schema=self.schema))
        required = {"matches", "team_match_stats", "team_chance_quality_ratings"}
        if missing := required - tables:
            raise RuntimeError(
                f"xG-proxy tables are missing: {sorted(missing)}. Apply "
                "supabase/migrations/202606110003_xg_proxy_v4.sql first."
            )
        columns = {
            column["name"]
            for column in inspector.get_columns(
                "team_match_stats", schema=self.schema
            )
        }
        if missing := REQUIRED_STAT_COLUMNS - columns:
            raise RuntimeError(
                f"team_match_stats is missing {sorted(missing)}. Apply "
                "supabase/migrations/202606110003_xg_proxy_v4.sql first."
            )

    def load_rows(self) -> list[dict[str, Any]]:
        stats = self._table("team_match_stats")
        matches = self._table("matches")
        date_column = next(
            (
                matches.c[name]
                for name in ("match_date", "kickoff")
                if name in matches.c
            ),
            None,
        )
        statement = select(stats)
        if date_column is not None:
            statement = select(stats, date_column.label("match_date")).join(
                matches, matches.c.id == stats.c.match_id
            )
        with self.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(statement).mappings()
            ]

    def store(self, ratings: list[dict[str, Any]]) -> None:
        table = self._table("team_chance_quality_ratings")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            for rating in ratings:
                values = {
                    **rating,
                    "rated_at": now,
                    "model_version": MODEL_VERSION,
                    "updated_at": now,
                }
                if not isinstance(table.c.components.type, JSON):
                    values["components"] = json.dumps(values["components"])
                existing = connection.execute(
                    select(table.c.id).where(
                        table.c.team_id == rating["team_id"],
                        table.c.model_version == MODEL_VERSION,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    connection.execute(table.insert().values(**values))
                else:
                    connection.execute(
                        table.update()
                        .where(table.c.id == existing)
                        .values(**values)
                    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        logging.error("[xg-proxy] DATABASE_URL is required")
        return 2
    engine = create_database_engine(database_url)
    try:
        repository = ChanceQualityRepository(engine)
        repository.assert_schema()
        ratings = build_team_ratings(repository.load_rows())
        if not ratings:
            logging.info("[xg-proxy] No complete team-stat matches are available")
            return 0
        repository.store(ratings)
        logging.info("[xg-proxy] Stored %d team ratings", len(ratings))
        return 0
    except Exception:
        logging.exception("[xg-proxy] Feature build failed")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
