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
from sqlalchemy import (
    JSON,
    MetaData,
    Table,
    and_,
    delete,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.database import create_database_engine

TEAM_REQUIRED_COLUMNS = {
    "team_id",
    "model_run_id",
    "rated_at",
    "elo_rating",
    "attack_rating",
    "defense_rating",
    "form_rating",
    "matches_played",
    "goals_for",
    "goals_against",
    "updated_at",
}
PLAYER_REQUIRED_COLUMNS = {
    "player_id",
    "team_id",
    "model_run_id",
    "rated_at",
    "overall_rating",
    "goal_threat",
    "assist_threat",
    "shot_volume",
    "minutes_rating",
    "form_rating",
    "matches_played",
    "minutes_played",
    "updated_at",
}


def load_environment() -> dict[str, str]:
    """Load server-side env files without replacing explicitly exported values."""
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _integer(value: Any) -> int:
    return max(0, round(_number(value)))


def _score(goals_for: int, goals_against: int) -> float:
    if goals_for > goals_against:
        return 1.0
    if goals_for == goals_against:
        return 0.5
    return 0.0


def _row_order(row: dict[str, Any]) -> tuple[str, str]:
    timestamp = row.get("match_date") or row.get("captured_at") or row.get("created_at")
    return (str(timestamp or ""), str(row["match_id"]))


def calculate_team_ratings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build deterministic team ratings from complete two-team match-stat rows."""
    by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("team_id") is not None and row.get("goals") is not None:
            by_match[row["match_id"]].append(row)

    complete_matches = sorted(
        (match_rows for match_rows in by_match.values() if len(match_rows) == 2),
        key=lambda match_rows: _row_order(match_rows[0]),
    )
    elo: dict[Any, float] = defaultdict(lambda: 1500.0)
    history: dict[Any, list[dict[str, Any]]] = defaultdict(list)

    for first, second in complete_matches:
        first_goals = _integer(first["goals"])
        second_goals = _integer(second["goals"])
        first_id = first["team_id"]
        second_id = second["team_id"]
        expected_first = 1.0 / (1.0 + 10 ** ((elo[second_id] - elo[first_id]) / 400.0))
        actual_first = _score(first_goals, second_goals)

        # Standard Elo with K=20. Wider scorelines increase the update by 25%
        # per extra goal, keeping the formula easy to inspect and replay.
        margin_multiplier = 1.0 + 0.25 * max(0, abs(first_goals - second_goals) - 1)
        change = 20.0 * margin_multiplier * (actual_first - expected_first)
        elo[first_id] += change
        elo[second_id] -= change

        history[first_id].append({"for": first_goals, "against": second_goals})
        history[second_id].append({"for": second_goals, "against": first_goals})

    ratings = []
    for team_id, matches in history.items():
        goals_for = sum(match["for"] for match in matches)
        goals_against = sum(match["against"] for match in matches)
        matches_played = len(matches)
        goals_for_per_match = goals_for / matches_played
        goals_against_per_match = goals_against / matches_played
        recent = matches[-5:]

        # Attack reaches 100 at 3 goals per match. Defense is 100 for no goals
        # conceded and falls linearly to 0 at 3 conceded per match.
        attack_rating = min(100.0, 100.0 * goals_for_per_match / 3.0)
        defense_rating = max(0.0, 100.0 * (1.0 - goals_against_per_match / 3.0))
        # Form is the percentage of available points won in the latest 5 games.
        form_rating = 100.0 * sum(
            3.0 if match["for"] > match["against"] else
            1.0 if match["for"] == match["against"] else 0.0
            for match in recent
        ) / (3.0 * len(recent))
        ratings.append(
            {
                "team_id": team_id,
                "elo_rating": round(elo[team_id], 2),
                "attack_rating": round(attack_rating, 2),
                "defense_rating": round(defense_rating, 2),
                "form_rating": round(form_rating, 2),
                "matches_played": matches_played,
                "goals_for": goals_for,
                "goals_against": goals_against,
            }
        )
    return ratings


def calculate_player_ratings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build capped per-90 player ratings from appearances with recorded minutes."""
    by_player: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("player_id") is not None and _integer(row.get("minutes_played")) > 0:
            by_player[row["player_id"]].append(row)

    ratings = []
    for player_id, appearances in by_player.items():
        appearances.sort(key=_row_order)
        minutes = sum(_integer(row.get("minutes_played")) for row in appearances)
        goals = sum(_integer(row.get("goals")) for row in appearances)
        assists = sum(_integer(row.get("assists")) for row in appearances)
        shots = sum(_integer(row.get("shots")) for row in appearances)
        per_90 = 90.0 / minutes

        # Each per-90 component is capped at 100 at an intentionally strong
        # benchmark: 0.75 goals, 0.60 assists, or 5 shots per full match.
        goal_threat = min(100.0, goals * per_90 / 0.75 * 100.0)
        assist_threat = min(100.0, assists * per_90 / 0.60 * 100.0)
        shot_volume = min(100.0, shots * per_90 / 5.0 * 100.0)
        minutes_rating = min(
            100.0,
            100.0 * minutes / (90.0 * len(appearances)),
        )

        # Recent form averages the latest 5 appearance scores. A full match is
        # worth 20 points, with goals, assists, and shots on target adding the rest.
        recent_scores = []
        for row in appearances[-5:]:
            appearance_score = (
                20.0 * min(90, _integer(row.get("minutes_played"))) / 90.0
                + 40.0 * _integer(row.get("goals"))
                + 30.0 * _integer(row.get("assists"))
                + 5.0 * _integer(row.get("shots_on_target"))
            )
            recent_scores.append(min(100.0, appearance_score))
        form_rating = sum(recent_scores) / len(recent_scores)

        # Overall favors direct scoring and creation while retaining workload
        # and recent form: 35/25/15/15/10 percent respectively.
        overall_rating = (
            0.35 * goal_threat
            + 0.25 * assist_threat
            + 0.15 * shot_volume
            + 0.15 * minutes_rating
            + 0.10 * form_rating
        )
        ratings.append(
            {
                "player_id": player_id,
                "team_id": appearances[-1].get("team_id"),
                "overall_rating": round(overall_rating, 2),
                "goal_threat": round(goal_threat, 2),
                "assist_threat": round(assist_threat, 2),
                "shot_volume": round(shot_volume, 2),
                "minutes_rating": round(minutes_rating, 2),
                "form_rating": round(form_rating, 2),
                "matches_played": len(appearances),
                "minutes_played": minutes,
            }
        )
    return ratings


class RatingRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

    def _table(self, name: str) -> Table:
        if name not in self.tables:
            self.tables[name] = Table(
                name,
                self.metadata,
                schema=self.schema,
                autoload_with=self.engine,
            )
        return self.tables[name]

    def assert_schema(self) -> None:
        inspector = inspect(self.engine)
        required_tables = {
            "team_match_stats",
            "player_match_stats",
            "team_ratings",
            "player_ratings",
        }
        existing = set(inspector.get_table_names(schema=self.schema))
        missing_tables = required_tables - existing
        if missing_tables:
            raise RuntimeError(f"Daily pipeline tables are missing: {sorted(missing_tables)}")

        for table_name, required_columns in (
            ("team_ratings", TEAM_REQUIRED_COLUMNS),
            ("player_ratings", PLAYER_REQUIRED_COLUMNS),
        ):
            columns = {
                column["name"]
                for column in inspector.get_columns(table_name, schema=self.schema)
            }
            missing = required_columns - columns
            if missing:
                raise RuntimeError(
                    f"{table_name} is missing columns {sorted(missing)}. Apply "
                    "supabase/migrations/202606100002_rating_updates.sql first."
                )

    def load_team_stats(self) -> list[dict[str, Any]]:
        stats = self._table("team_match_stats")
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(stats)).mappings()]
        return self._add_match_dates(rows)

    def load_player_stats(self) -> list[dict[str, Any]]:
        stats = self._table("player_match_stats")
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(stats)).mappings()]
        return self._add_match_dates(rows)

    def _add_match_dates(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        inspector = inspect(self.engine)
        if "matches" not in inspector.get_table_names(schema=self.schema):
            return rows
        matches = self._table("matches")
        date_column = next(
            (matches.c[name] for name in ("match_date", "kickoff") if name in matches.c),
            None,
        )
        if date_column is None:
            return rows
        with self.engine.connect() as connection:
            dates = dict(
                connection.execute(select(matches.c.id, date_column)).tuples().all()
            )
        for row in rows:
            row["match_date"] = dates.get(row["match_id"])
        return rows

    def upsert_ratings(
        self,
        team_ratings: list[dict[str, Any]],
        player_ratings: list[dict[str, Any]],
    ) -> None:
        team_table = self._table("team_ratings")
        player_table = self._table("player_ratings")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(text("select pg_advisory_xact_lock(hashtext('rating-update'))"))
            for rating in team_ratings:
                self._upsert_current(
                    connection,
                    team_table,
                    "team_id",
                    rating,
                    now,
                    compatibility={
                        "sample_matches": rating["matches_played"],
                        "components": {
                            "formula_version": "transparent-v1",
                            "goals_for": rating["goals_for"],
                            "goals_against": rating["goals_against"],
                        },
                    },
                )
            for rating in player_ratings:
                self._upsert_current(
                    connection,
                    player_table,
                    "player_id",
                    rating,
                    now,
                    compatibility={
                        "attacking_rating": rating["goal_threat"],
                        "creative_rating": rating["assist_threat"],
                        "availability_rating": rating["minutes_rating"],
                        "projected_minutes": min(
                            90.0,
                            rating["minutes_played"] / rating["matches_played"],
                        ),
                        "components": {
                            "formula_version": "transparent-v1",
                            "shot_volume": rating["shot_volume"],
                            "form_rating": rating["form_rating"],
                        },
                    },
                )

    @staticmethod
    def _upsert_current(
        connection: Connection,
        table: Table,
        entity_column: str,
        rating: dict[str, Any],
        now: datetime,
        compatibility: dict[str, Any],
    ) -> None:
        entity_id = rating[entity_column]
        current_filter = and_(
            table.c[entity_column] == entity_id,
            table.c.model_run_id.is_(None),
        )
        existing_ids = list(connection.execute(select(table.c.id).where(current_filter)).scalars())
        values = {
            **rating,
            "model_run_id": None,
            "rated_at": now,
            "updated_at": now,
        }
        for key, value in compatibility.items():
            if key in table.c:
                values[key] = (
                    value
                    if key != "components" or isinstance(table.c[key].type, JSON)
                    else json.dumps(value)
                )
        values = {key: value for key, value in values.items() if key in table.c}

        if existing_ids:
            connection.execute(
                update(table).where(table.c.id == existing_ids[0]).values(**values)
            )
            if len(existing_ids) > 1:
                connection.execute(delete(table).where(table.c.id.in_(existing_ids[1:])))
        else:
            connection.execute(table.insert().values(**values))


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("update_ratings")
    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        logger.error("[rating-update] FAILED: DATABASE_URL is required")
        return 2

    try:
        engine = create_database_engine(database_url)
    except Exception:
        logger.exception("[rating-update] FAILED: could not initialize database")
        return 1

    try:
        logger.info("[rating-update] START")
        repository = RatingRepository(engine)
        repository.assert_schema()
        team_rows = repository.load_team_stats()
        player_rows = repository.load_player_stats()
        if not team_rows and not player_rows:
            logger.info("[rating-update] SUCCESS: no team or player match data yet")
            return 0

        team_ratings = calculate_team_ratings(team_rows)
        player_ratings = calculate_player_ratings(player_rows)
        repository.upsert_ratings(team_ratings, player_ratings)
        logger.info(
            "[rating-update] SUCCESS: %d team ratings and %d player ratings updated",
            len(team_ratings),
            len(player_ratings),
        )
        return 0
    except Exception:
        logger.exception("[rating-update] FAILED: unexpected rating update error")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
