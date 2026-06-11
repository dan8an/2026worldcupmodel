#!/usr/bin/env python3
from __future__ import annotations

import json
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

MODEL_VERSION = "squad-v4.1-research"
REQUIRED_TABLES = {
    "players",
    "player_ratings",
    "player_availability_reports",
    "projected_lineups",
    "squad_strength_ratings",
}


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def player_strength(row: dict[str, Any]) -> tuple[float, bool]:
    overall = _number(row.get("overall_rating"))
    provider_rating = _number(row.get("provider_rating") or row.get("rating"))
    lineup_strength = _number(row.get("lineup_strength"))
    observed = any(
        value is not None for value in (overall, provider_rating, lineup_strength)
    )
    raw = (
        overall
        if overall is not None
        else provider_rating * 10.0
        if provider_rating is not None
        else lineup_strength
        if lineup_strength is not None
        else 50.0
    )
    minutes = max(0.0, _number(row.get("minutes_played")) or 0.0)
    reliability = min(1.0, minutes / 900.0) if minutes else (0.5 if observed else 0.0)
    return max(0.0, min(100.0, 50.0 + reliability * (raw - 50.0))), observed


def calculate_squad_strength(players: list[dict[str, Any]]) -> dict[str, Any]:
    if not players:
        return {
            "squad_strength": None,
            "available_squad_strength": None,
            "projected_lineup_strength": None,
            "unavailable_player_penalty": 0.0,
            "depth_strength": None,
            "player_count": 0,
            "available_player_count": 0,
            "lineup_player_count": 0,
            "coverage_level": 0.0,
            "components": {"formula_version": MODEL_VERSION, "players": []},
        }

    enriched = []
    observed_count = 0
    for row in players:
        strength, observed = player_strength(row)
        observed_count += int(observed)
        status = str(row.get("status") or "available").casefold()
        enriched.append(
            {
                **row,
                "strength": strength,
                "available": status not in {"injured", "suspended"},
                "in_lineup": bool(row.get("in_lineup")),
            }
        )
    ranked = sorted(enriched, key=lambda row: row["strength"], reverse=True)
    available = [row for row in ranked if row["available"]]
    confirmed_lineup = [
        row for row in ranked if row["available"] and row["in_lineup"]
    ]
    lineup = confirmed_lineup[:11] or available[:11]

    def mean(rows: list[dict[str, Any]]) -> float | None:
        return (
            sum(row["strength"] for row in rows) / len(rows)
            if rows
            else None
        )

    first_eleven = ranked[:11]
    depth = ranked[11:18]
    squad_strength = mean(first_eleven)
    depth_strength = mean(depth)
    if squad_strength is not None and depth_strength is not None:
        squad_strength = 0.8 * squad_strength + 0.2 * depth_strength
    available_strength = mean(available[:11])
    lineup_strength = mean(lineup[:11])
    unavailable_penalty = min(
        25.0,
        sum(
            max(0.0, row["strength"] - 50.0) / 11.0
            for row in ranked
            if not row["available"]
        ),
    )
    return {
        "squad_strength": round(squad_strength, 4) if squad_strength is not None else None,
        "available_squad_strength": (
            round(available_strength, 4) if available_strength is not None else None
        ),
        "projected_lineup_strength": (
            round(lineup_strength, 4) if lineup_strength is not None else None
        ),
        "unavailable_player_penalty": round(unavailable_penalty, 4),
        "depth_strength": round(depth_strength, 4) if depth_strength is not None else None,
        "player_count": len(ranked),
        "available_player_count": len(available),
        "lineup_player_count": len(lineup),
        "coverage_level": round(observed_count / len(ranked), 6),
        "components": {
            "formula_version": MODEL_VERSION,
            "lineup_source": (
                "provider_confirmed" if confirmed_lineup else "top_available_projection"
            ),
            "strength_source_priority": [
                "player_ratings.overall_rating",
                "provider rating scaled to 0-100",
                "neutral 50 fallback",
            ],
            "players": [
                {
                    "player_id": str(row.get("player_id") or ""),
                    "provider_player_id": row.get("provider_player_id"),
                    "name": row.get("player_name") or row.get("display_name"),
                    "position": row.get("position") or row.get("primary_position"),
                    "status": row.get("status") or "available",
                    "strength": round(row["strength"], 4),
                    "in_lineup": row["in_lineup"],
                }
                for row in ranked
            ],
        },
    }


class SquadStrengthRepository:
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
        existing = set(inspect(self.engine).get_table_names(schema=self.schema))
        missing = REQUIRED_TABLES - existing
        if missing:
            raise RuntimeError(
                f"Squad v4.1 tables are missing: {sorted(missing)}. Apply "
                "supabase/migrations/202606110004_squad_v41_research.sql first."
            )

    def load_players(self) -> list[dict[str, Any]]:
        players = self._table("players")
        ratings = self._table("player_ratings")
        availability = self._table("player_availability_reports")
        lineups = self._table("projected_lineups")
        with self.engine.connect() as connection:
            player_rows = [dict(row) for row in connection.execute(select(players)).mappings()]
            rating_rows = [dict(row) for row in connection.execute(select(ratings)).mappings()]
            availability_rows = [
                dict(row) for row in connection.execute(select(availability)).mappings()
            ]
            lineup_rows = [dict(row) for row in connection.execute(select(lineups)).mappings()]

        latest_rating = {}
        for row in sorted(rating_rows, key=lambda item: str(item.get("rated_at") or "")):
            latest_rating[row.get("player_id")] = row
        latest_availability = {}
        for row in sorted(
            availability_rows, key=lambda item: str(item.get("collected_at") or "")
        ):
            latest_availability[
                (row.get("fixture_id"), row.get("team_id"), row.get("player_id"))
            ] = row
        latest_lineup = {}
        for row in sorted(lineup_rows, key=lambda item: str(item.get("collected_at") or "")):
            latest_lineup[
                (row.get("fixture_id"), row.get("team_id"), row.get("player_id"))
            ] = row

        output = []
        fixture_keys = {
            (fixture_id, team_id)
            for fixture_id, team_id, _ in (*latest_availability.keys(), *latest_lineup.keys())
        } or {(None, row.get("team_id")) for row in player_rows}
        for fixture_id, team_id in fixture_keys:
            for player in player_rows:
                if player.get("team_id") != team_id:
                    continue
                player_id = player.get("id")
                availability_row = latest_availability.get(
                    (fixture_id, team_id, player_id)
                ) or latest_availability.get((None, team_id, player_id), {})
                lineup_row = latest_lineup.get((fixture_id, team_id, player_id), {})
                raw_payload = _json_object(availability_row.get("raw_payload"))
                provider_statistics = _json_object(
                    raw_payload.get("statistics")
                )
                output.append(
                    {
                        **player,
                        **latest_rating.get(player_id, {}),
                        "fixture_id": fixture_id,
                        "player_id": player_id,
                        "team_id": team_id,
                        "status": availability_row.get("status", "unknown"),
                        "provider_rating": provider_statistics.get("rating"),
                        "lineup_strength": lineup_row.get("player_strength"),
                        "minutes_played": (
                            latest_rating.get(player_id, {}).get("minutes_played")
                            or provider_statistics.get("minutes")
                        ),
                        "provider_player_id": availability_row.get(
                            "provider_player_id"
                        ) or lineup_row.get("provider_player_id"),
                        "player_name": player.get("display_name"),
                        "position": player.get("primary_position"),
                        "in_lineup": bool(lineup_row),
                    }
                )
        return output

    def store(self, rows: list[dict[str, Any]]) -> int:
        table = self._table("squad_strength_ratings")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            for row in rows:
                values = {
                    **row,
                    "model_version": MODEL_VERSION,
                    "source": "squad-v4.1-research",
                    "rated_at": now,
                }
                if "components" in table.c and not isinstance(table.c.components.type, JSON):
                    values["components"] = json.dumps(values["components"])
                connection.execute(
                    table.insert().values(
                        **{key: value for key, value in values.items() if key in table.c}
                    )
                )
        return len(rows)


def build_rows(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in players:
        grouped[(row.get("fixture_id"), row.get("team_id"))].append(row)
    return [
        {
            "fixture_id": fixture_id,
            "team_id": team_id,
            **calculate_squad_strength(team_players),
        }
        for (fixture_id, team_id), team_players in grouped.items()
        if team_id is not None
    ]


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        repository = SquadStrengthRepository(engine)
        repository.assert_schema()
        rows = build_rows(repository.load_players())
        repository.store(rows)
        print(f"Squad strength: wrote {len(rows)} research ratings")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
