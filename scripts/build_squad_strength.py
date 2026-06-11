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
REPORT_PATH = ROOT / "data" / "evaluation" / "squad_v41_coverage_latest.json"
REQUIRED_TABLES = {
    "players",
    "player_ratings",
    "player_availability_reports",
    "projected_lineups",
    "squad_strength_ratings",
}
REQUIRED_STRENGTH_COLUMNS = {
    "squad_size",
    "available_players",
    "unavailable_players",
    "known_position_counts",
    "goalkeeper_count",
    "defender_count",
    "midfielder_count",
    "attacker_count",
    "squad_depth_score",
    "availability_score",
    "data_completeness_score",
    "rating_source",
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


POSITION_BUCKETS = {
    "goalkeeper": "goalkeeper",
    "gk": "goalkeeper",
    "defender": "defender",
    "def": "defender",
    "midfielder": "midfielder",
    "mid": "midfielder",
    "attacker": "attacker",
    "forward": "attacker",
    "att": "attacker",
    "fw": "attacker",
}
POSITION_TARGETS = {
    "goalkeeper": 3,
    "defender": 7,
    "midfielder": 7,
    "attacker": 4,
}


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, value))


def _position_bucket(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    return POSITION_BUCKETS.get(normalized)


def player_strength(row: dict[str, Any]) -> tuple[float | None, bool]:
    overall = _number(row.get("overall_rating"))
    provider_rating = _number(row.get("provider_rating") or row.get("rating"))
    lineup_strength = _number(row.get("lineup_strength"))
    observed = any(
        value is not None for value in (overall, provider_rating, lineup_strength)
    )
    if not observed:
        return None, False
    raw = (
        overall
        if overall is not None
        else provider_rating * 10.0
        if provider_rating is not None and provider_rating <= 10.0
        else provider_rating
        if provider_rating is not None
        else lineup_strength
    )
    minutes = max(0.0, _number(row.get("minutes_played")) or 0.0)
    reliability = min(1.0, minutes / 900.0) if minutes else 0.5
    return max(0.0, min(100.0, 50.0 + reliability * (raw - 50.0))), observed


def calculate_squad_strength(players: list[dict[str, Any]]) -> dict[str, Any]:
    if not players:
        return {
            "squad_strength": None,
            "available_squad_strength": None,
            "projected_lineup_strength": None,
            "unavailable_player_penalty": 0.0,
            "depth_strength": None,
            "squad_size": 0,
            "available_players": 0,
            "unavailable_players": 0,
            "known_position_counts": 0,
            "goalkeeper_count": 0,
            "defender_count": 0,
            "midfielder_count": 0,
            "attacker_count": 0,
            "squad_depth_score": 0.0,
            "availability_score": 0.0,
            "data_completeness_score": 0.0,
            "rating_source": "squad_depth_only",
            "player_count": 0,
            "available_player_count": 0,
            "lineup_player_count": 0,
            "coverage_level": 0.0,
            "components": {"formula_version": MODEL_VERSION, "players": []},
        }

    unique_players: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(players):
        if row.get("player_id") is not None:
            identity = str(row["player_id"])
        elif row.get("provider_player_id") is not None:
            identity = f"provider:{row['provider_player_id']}"
        else:
            identity = (
                f"name:{row.get('player_name') or row.get('display_name') or index}"
            )
        unique_players[identity] = row

    enriched = []
    observed_count = 0
    position_counts = {name: 0 for name in POSITION_TARGETS}
    for row in unique_players.values():
        strength, observed = player_strength(row)
        observed_count += int(observed)
        status = str(row.get("status") or "unknown").casefold()
        position_bucket = _position_bucket(
            row.get("position") or row.get("primary_position")
        )
        if position_bucket:
            position_counts[position_bucket] += 1
        enriched.append(
            {
                **row,
                "strength": strength,
                "status_normalized": status,
                "available": status == "available",
                "unavailable": status in {"injured", "suspended"},
                "in_lineup": bool(row.get("in_lineup")),
                "position_bucket": position_bucket,
            }
        )
    ranked = sorted(
        enriched,
        key=lambda row: (
            row["strength"] is not None,
            row["strength"] if row["strength"] is not None else -1.0,
        ),
        reverse=True,
    )
    available = [row for row in ranked if row["available"]]
    unavailable = [row for row in ranked if row["unavailable"]]
    confirmed_lineup = [
        row for row in ranked if row["available"] and row["in_lineup"]
    ]

    def mean(rows: list[dict[str, Any]]) -> float | None:
        observed_rows = [row for row in rows if row["strength"] is not None]
        return (
            sum(float(row["strength"]) for row in observed_rows) / len(observed_rows)
            if observed_rows
            else None
        )

    squad_size = len(ranked)
    known_position_counts = sum(position_counts.values())
    known_availability = sum(
        row["status_normalized"] in {"available", "injured", "suspended"}
        for row in ranked
    )
    roster_completeness = min(1.0, squad_size / 26.0)
    position_completeness = known_position_counts / squad_size
    availability_completeness = known_availability / squad_size
    position_depth = sum(
        min(1.0, position_counts[name] / target)
        for name, target in POSITION_TARGETS.items()
    ) / len(POSITION_TARGETS)
    squad_depth_score = _clamp_score(
        100.0 * (0.5 * roster_completeness + 0.5 * position_depth)
    )
    availability_score = _clamp_score(100.0 * len(available) / squad_size)
    data_completeness_score = _clamp_score(
        100.0
        * (
            0.4 * roster_completeness
            + 0.3 * position_completeness
            + 0.3 * availability_completeness
        )
    )

    observed_ranked = [row for row in ranked if row["strength"] is not None]
    observed_available = [row for row in available if row["strength"] is not None]
    first_eleven = observed_ranked[:11]
    quality_depth = observed_ranked[11:18]
    observed_squad_strength = mean(first_eleven)
    quality_depth_strength = mean(quality_depth)
    if observed_squad_strength is not None and quality_depth_strength is not None:
        observed_squad_strength = (
            0.8 * observed_squad_strength + 0.2 * quality_depth_strength
        )

    rating_source = "player_ratings" if observed_count else "squad_depth_only"
    squad_strength = (
        observed_squad_strength
        if observed_count
        else (
            0.7 * squad_depth_score
            + 0.2 * availability_score
            + 0.1 * data_completeness_score
        )
    )
    available_strength = (
        mean(observed_available[:11])
        if observed_count
        else squad_depth_score * availability_score / 100.0
    )
    lineup_strength = mean(confirmed_lineup[:11])
    if observed_count:
        unavailable_penalty = min(
            25.0,
            sum(
                max(0.0, float(row["strength"]) - 50.0) / 11.0
                for row in unavailable
                if row["strength"] is not None
            ),
        )
    else:
        unavailable_penalty = 25.0 * len(unavailable) / squad_size

    return {
        "squad_strength": round(squad_strength, 4) if squad_strength is not None else None,
        "available_squad_strength": (
            round(available_strength, 4) if available_strength is not None else None
        ),
        "projected_lineup_strength": (
            round(lineup_strength, 4) if lineup_strength is not None else None
        ),
        "unavailable_player_penalty": round(unavailable_penalty, 4),
        "depth_strength": round(squad_depth_score, 4),
        "squad_size": squad_size,
        "available_players": len(available),
        "unavailable_players": len(unavailable),
        "known_position_counts": known_position_counts,
        "goalkeeper_count": position_counts["goalkeeper"],
        "defender_count": position_counts["defender"],
        "midfielder_count": position_counts["midfielder"],
        "attacker_count": position_counts["attacker"],
        "squad_depth_score": round(squad_depth_score, 4),
        "availability_score": round(availability_score, 4),
        "data_completeness_score": round(data_completeness_score, 4),
        "rating_source": rating_source,
        "player_count": squad_size,
        "available_player_count": len(available),
        "lineup_player_count": len(confirmed_lineup[:11]),
        "coverage_level": round(data_completeness_score / 100.0, 6),
        "components": {
            "formula_version": MODEL_VERSION,
            "lineup_source": (
                "provider_confirmed" if confirmed_lineup else "unavailable"
            ),
            "rating_source": rating_source,
            "quality_rating_coverage": round(observed_count / squad_size, 6),
            "position_counts": position_counts,
            "unknown_availability_players": squad_size - known_availability,
            "strength_source_priority": [
                "player_ratings.overall_rating",
                "provider rating scaled to 0-100",
                "squad depth/availability/completeness fallback",
            ],
            "players": [
                {
                    "player_id": str(row.get("player_id") or ""),
                    "provider_player_id": row.get("provider_player_id"),
                    "name": row.get("player_name") or row.get("display_name"),
                    "position": row.get("position") or row.get("primary_position"),
                    "status": row.get("status") or "unknown",
                    "strength": (
                        round(row["strength"], 4)
                        if row["strength"] is not None
                        else None
                    ),
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
        inspector = inspect(self.engine)
        existing = set(inspector.get_table_names(schema=self.schema))
        missing = REQUIRED_TABLES - existing
        if missing:
            raise RuntimeError(
                f"Squad v4.1 tables are missing: {sorted(missing)}. Apply "
                "supabase/migrations/202606110004_squad_v41_research.sql first."
            )
        strength_columns = {
            column["name"]
            for column in inspector.get_columns(
                "squad_strength_ratings", schema=self.schema
            )
        }
        missing_columns = REQUIRED_STRENGTH_COLUMNS - strength_columns
        if missing_columns:
            raise RuntimeError(
                f"Squad v4.1 strength columns are missing: "
                f"{sorted(missing_columns)}. Apply "
                "supabase/migrations/202606110006_squad_v41_strength_features.sql "
                "first."
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
        player_by_id = {row.get("id"): row for row in player_rows}

        def fixture_key(row: dict[str, Any]) -> tuple[str, Any]:
            if row.get("fixture_id") is not None:
                return ("database", row["fixture_id"])
            return ("provider", row.get("provider_fixture_id"))

        def team_key(row: dict[str, Any]) -> Any:
            return row.get("team_id") or row.get("team_code")

        def player_key(row: dict[str, Any]) -> Any:
            return (
                row.get("player_id")
                or f"provider:{row.get('provider_player_id')}"
                or f"name:{row.get('player_name')}"
            )

        latest_availability = {}
        for row in sorted(
            availability_rows, key=lambda item: str(item.get("collected_at") or "")
        ):
            latest_availability[(fixture_key(row), team_key(row), player_key(row))] = row
        latest_lineup = {}
        for row in sorted(lineup_rows, key=lambda item: str(item.get("collected_at") or "")):
            latest_lineup[(fixture_key(row), team_key(row), player_key(row))] = row

        output = []
        base_keys = set(latest_availability) | set(latest_lineup)
        for key in base_keys:
            availability_row = latest_availability.get(key, {})
            lineup_row = latest_lineup.get(key, {})
            source_row = availability_row or lineup_row
            player_id = source_row.get("player_id")
            player = player_by_id.get(player_id, {})
            raw_payload = _json_object(availability_row.get("raw_payload"))
            provider_statistics = _json_object(raw_payload.get("statistics"))
            output.append(
                {
                    **player,
                    **latest_rating.get(player_id, {}),
                    "fixture_id": source_row.get("fixture_id"),
                    "provider_fixture_id": source_row.get("provider_fixture_id"),
                    "canonical_home_team_code": source_row.get(
                        "canonical_home_team_code"
                    ),
                    "canonical_away_team_code": source_row.get(
                        "canonical_away_team_code"
                    ),
                    "player_id": player_id,
                    "team_id": source_row.get("team_id"),
                    "team_code": source_row.get("team_code"),
                    "status": availability_row.get("status", "unknown"),
                    "provider_rating": provider_statistics.get("rating"),
                    "lineup_strength": lineup_row.get("player_strength"),
                    "minutes_played": (
                        latest_rating.get(player_id, {}).get("minutes_played")
                        or provider_statistics.get("minutes")
                    ),
                    "provider_player_id": source_row.get("provider_player_id"),
                    "player_name": (
                        source_row.get("player_name") or player.get("display_name")
                    ),
                    "position": (
                        source_row.get("position") or player.get("primary_position")
                    ),
                    "in_lineup": bool(lineup_row),
                    "availability_source": availability_row.get("source"),
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
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in players:
        grouped[
            (
                row.get("fixture_id"),
                row.get("provider_fixture_id"),
                row.get("team_id"),
                row.get("team_code"),
                row.get("canonical_home_team_code"),
                row.get("canonical_away_team_code"),
            )
        ].append(row)
    return [
        {
            "fixture_id": fixture_id,
            "provider_fixture_id": provider_fixture_id,
            "team_id": team_id,
            "team_code": team_code,
            "canonical_home_team_code": canonical_home_team_code,
            "canonical_away_team_code": canonical_away_team_code,
            **calculate_squad_strength(team_players),
        }
        for (
            fixture_id,
            provider_fixture_id,
            team_id,
            team_code,
            canonical_home_team_code,
            canonical_away_team_code,
        ), team_players in grouped.items()
        if team_id is not None or team_code is not None
    ]


def build_coverage_report(
    rows: list[dict[str, Any]],
    players: list[dict[str, Any]],
) -> dict[str, Any]:
    team_rows = sorted(
        rows,
        key=lambda row: (
            str(row.get("team_code") or row.get("team_id") or ""),
            int(row.get("provider_fixture_id") or 0),
        ),
    )
    availability_teams = {
        row.get("team_code") or str(row.get("team_id"))
        for row in players
        if row.get("availability_source")
    }
    injury_teams = {
        row.get("team_code") or str(row.get("team_id"))
        for row in players
        if str(row.get("status") or "").casefold() in {"injured", "suspended"}
    }
    lineup_teams = {
        row.get("team_code") or str(row.get("team_id"))
        for row in players
        if row.get("in_lineup")
    }
    rating_sources: dict[str, int] = defaultdict(int)
    for row in team_rows:
        rating_sources[str(row["rating_source"])] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "status": "research_only",
        "production_predictions_changed": False,
        "production_simulation_changed": False,
        "teams_with_squad_ratings": len(team_rows),
        "teams_with_availability_data": len(availability_teams),
        "teams_with_injury_data": len(availability_teams),
        "teams_with_unavailable_players": len(injury_teams),
        "teams_with_lineup_data": len(lineup_teams),
        "rating_source_counts": dict(sorted(rating_sources.items())),
        "feature_generation_usable": len(team_rows) >= 2,
        "chronological_validation_usable": False,
        "chronological_validation_note": (
            "Current provider fixture snapshots are future research data, not "
            "historical point-in-time holdout coverage."
        ),
        "teams": [
            {
                key: row.get(key)
                for key in (
                    "team_id",
                    "team_code",
                    "fixture_id",
                    "provider_fixture_id",
                    "rating_source",
                    "squad_size",
                    "available_players",
                    "unavailable_players",
                    "known_position_counts",
                    "goalkeeper_count",
                    "defender_count",
                    "midfielder_count",
                    "attacker_count",
                    "squad_depth_score",
                    "availability_score",
                    "data_completeness_score",
                    "squad_strength",
                )
            }
            for row in team_rows
        ],
    }


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
        players = repository.load_players()
        rows = build_rows(players)
        repository.store(rows)
        report = build_coverage_report(rows, players)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"Squad strength: wrote {len(rows)} research ratings")
        print(
            "Coverage: "
            f"teams={report['teams_with_squad_ratings']} "
            f"availability={report['teams_with_availability_data']} "
            f"injuries={report['teams_with_injury_data']} "
            f"lineups={report['teams_with_lineup_data']}"
        )
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
