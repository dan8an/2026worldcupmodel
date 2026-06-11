#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select, update
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data_ingestion.providers import SportsProvider, create_sports_provider
from scripts.database import create_database_engine

REQUIRED_TABLES = {
    "teams",
    "matches",
    "players",
    "player_availability_reports",
    "projected_lineups",
}


class SquadAvailabilityRepository:
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

    def load_targets(self, max_fixtures: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        teams = self._table("teams")
        matches = self._table("matches")
        now = datetime.now(timezone.utc)
        date_column = next(
            (matches.c[name] for name in ("match_date", "kickoff") if name in matches.c),
            None,
        )
        with self.engine.connect() as connection:
            team_rows = [
                dict(row)
                for row in connection.execute(select(teams)).mappings()
                if row.get("api_football_team_id") is not None
            ]
            if date_column is None or "api_football_fixture_id" not in matches.c:
                return team_rows, []
            fixture_rows = [
                dict(row)
                for row in connection.execute(
                    select(matches)
                    .where(date_column >= now)
                    .where(matches.c.api_football_fixture_id.is_not(None))
                    .order_by(date_column)
                    .limit(max_fixtures)
                ).mappings()
            ]
        return team_rows, fixture_rows

    def _upsert_player(
        self,
        connection: Connection,
        team_id: Any,
        provider_player_id: int,
        name: str,
        position: str | None,
    ) -> Any:
        players = self._table("players")
        provider_key = f"api_football:{provider_player_id}"
        existing = connection.execute(
            select(players).where(players.c.provider_key == provider_key)
        ).mappings().first()
        values = {
            "team_id": team_id,
            "provider_key": provider_key,
            "display_name": name,
            "primary_position": position,
            "updated_at": datetime.now(timezone.utc),
        }
        values = {key: value for key, value in values.items() if key in players.c}
        if existing:
            connection.execute(
                update(players).where(players.c.id == existing["id"]).values(**values)
            )
            return existing["id"]
        return connection.execute(
            players.insert().values(**values).returning(players.c.id)
        ).scalar_one()

    @staticmethod
    def _json_value(table: Table, column: str, value: Any) -> Any:
        return value if isinstance(table.c[column].type, JSON) else json.dumps(value)

    def store(
        self,
        teams: list[dict[str, Any]],
        fixtures: list[dict[str, Any]],
        squads: dict[int, list[dict[str, Any]]],
        statistics: dict[int, list[dict[str, Any]]],
        injuries: dict[int, list[dict[str, Any]]],
        lineups: dict[int, list[dict[str, Any]]],
    ) -> dict[str, int]:
        availability_table = self._table("player_availability_reports")
        lineup_table = self._table("projected_lineups")
        team_by_provider = {
            int(team["api_football_team_id"]): team for team in teams
        }
        team_by_id = {team["id"]: team for team in teams}
        fixture_by_provider = {
            int(fixture["api_football_fixture_id"]): fixture for fixture in fixtures
        }
        provider_stats = {
            int(row["player"]["provider_id"]): row
            for rows in statistics.values()
            for row in rows
        }
        counts = {"players": 0, "availability_reports": 0, "lineup_players": 0}
        collected_at = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            for provider_team_id, rows in squads.items():
                team = team_by_provider.get(provider_team_id)
                if not team:
                    continue
                for row in rows:
                    stats = provider_stats.get(row["player"]["provider_id"], {})
                    position = row.get("position") or stats.get("position")
                    self._upsert_player(
                        connection,
                        team["id"],
                        row["player"]["provider_id"],
                        row["player"]["name"],
                        position,
                    )
                    counts["players"] += 1

            for provider_fixture_id, injury_rows in injuries.items():
                fixture = fixture_by_provider.get(provider_fixture_id)
                if not fixture:
                    continue
                injuries_by_player = {
                    int(row["player"]["provider_id"]): row for row in injury_rows
                }
                fixture_teams = [
                    team_by_id.get(fixture.get(column))
                    for column in ("home_team_id", "away_team_id")
                ]
                stored_provider_players = set()
                for team in (team for team in fixture_teams if team is not None):
                    provider_team_id = int(team["api_football_team_id"])
                    for squad_row in squads.get(provider_team_id, []):
                        provider_player_id = int(
                            squad_row["player"]["provider_id"]
                        )
                        injury = injuries_by_player.get(provider_player_id)
                        stats = provider_stats.get(provider_player_id, {})
                        position = (
                            squad_row.get("position")
                            or stats.get("position")
                            or (injury or {}).get("position")
                        )
                        player_id = self._upsert_player(
                            connection,
                            team["id"],
                            provider_player_id,
                            squad_row["player"]["name"],
                            position,
                        )
                        raw_payload = {
                            "squad": squad_row.get("raw", {}),
                            "statistics": stats,
                            "availability": (injury or {}).get("raw", {}),
                        }
                        values = {
                            "team_id": team["id"],
                            "team_code": team.get("code"),
                            "player_id": player_id,
                            "provider_player_id": provider_player_id,
                            "player_name": squad_row["player"]["name"],
                            "position": position,
                            "status": (
                                injury["status"] if injury else "available"
                            ),
                            "reason": (
                                injury.get("reason")
                                if injury
                                else "not listed by provider as unavailable"
                            ),
                            "fixture_id": fixture["id"],
                            "provider_fixture_id": provider_fixture_id,
                            "expected_return": (
                                injury.get("expected_return") if injury else None
                            ),
                            "source": (
                                "api_football"
                                if injury
                                else "api_football_inferred_available"
                            ),
                            "collected_at": collected_at,
                            "raw_payload": self._json_value(
                                availability_table, "raw_payload", raw_payload
                            ),
                        }
                        connection.execute(
                            availability_table.insert().values(**values)
                        )
                        counts["availability_reports"] += 1
                        stored_provider_players.add(provider_player_id)

                for row in injury_rows:
                    provider_player_id = int(row["player"]["provider_id"])
                    if provider_player_id in stored_provider_players:
                        continue
                    team = team_by_provider.get(row["team"]["provider_id"])
                    if not team:
                        continue
                    player_id = self._upsert_player(
                        connection,
                        team["id"],
                        provider_player_id,
                        row["player"]["name"],
                        row.get("position"),
                    )
                    values = {
                        "team_id": team["id"],
                        "team_code": team.get("code"),
                        "player_id": player_id,
                        "provider_player_id": provider_player_id,
                        "player_name": row["player"]["name"],
                        "position": row.get("position"),
                        "status": row["status"],
                        "reason": row.get("reason"),
                        "fixture_id": fixture["id"],
                        "provider_fixture_id": provider_fixture_id,
                        "expected_return": row.get("expected_return"),
                        "source": "api_football",
                        "collected_at": collected_at,
                        "raw_payload": self._json_value(
                            availability_table, "raw_payload", row.get("raw", {})
                        ),
                    }
                    connection.execute(availability_table.insert().values(**values))
                    counts["availability_reports"] += 1

            for provider_fixture_id, rows in lineups.items():
                fixture = fixture_by_provider.get(provider_fixture_id)
                if not fixture:
                    continue
                for lineup in rows:
                    team = team_by_provider.get(lineup["team"]["provider_id"])
                    if not team:
                        continue
                    for lineup_status, players in (
                        ("confirmed", lineup["starters"]),
                        ("substitute", lineup["substitutes"]),
                    ):
                        for row in players:
                            player_id = self._upsert_player(
                                connection,
                                team["id"],
                                row["provider_id"],
                                row["name"],
                                row.get("position"),
                            )
                            stats = provider_stats.get(row["provider_id"], {})
                            rating = stats.get("rating")
                            values = {
                                "team_id": team["id"],
                                "team_code": team.get("code"),
                                "fixture_id": fixture["id"],
                                "provider_fixture_id": provider_fixture_id,
                                "player_id": player_id,
                                "provider_player_id": row["provider_id"],
                                "player_name": row["name"],
                                "position": row.get("position"),
                                "lineup_status": lineup_status,
                                "formation": lineup.get("formation"),
                                "projected_minutes": (
                                    90.0 if lineup_status == "confirmed" else 0.0
                                ),
                                "player_strength": (
                                    float(rating) * 10.0 if rating is not None else None
                                ),
                                "source": "api_football",
                                "collected_at": collected_at,
                                "raw_payload": self._json_value(
                                    lineup_table, "raw_payload", lineup.get("raw", {})
                                ),
                            }
                            connection.execute(lineup_table.insert().values(**values))
                            counts["lineup_players"] += 1
        return counts


def collect(
    provider: SportsProvider,
    teams: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    season: int,
    league_id: int,
) -> tuple[
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
]:
    squads = {}
    statistics = {}
    for team in teams:
        provider_team_id = int(team["api_football_team_id"])
        squads[provider_team_id] = provider.get_squad(provider_team_id)
        statistics[provider_team_id] = provider.get_player_statistics(
            season=season,
            league_id=league_id,
            team_id=provider_team_id,
        )
    injuries = {}
    lineups = {}
    for fixture in fixtures:
        provider_fixture_id = int(fixture["api_football_fixture_id"])
        injuries[provider_fixture_id] = provider.get_injuries(
            fixture_id=provider_fixture_id
        )
        lineups[provider_fixture_id] = provider.get_lineups(provider_fixture_id)
    return squads, statistics, injuries, lineups


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect research-only squad, injury, suspension, and lineup data."
    )
    parser.add_argument("--max-fixtures", type=int, default=20)
    parser.add_argument("--sample", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    env = dict(os.environ)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        repository = SquadAvailabilityRepository(engine)
        repository.assert_schema()
        teams, fixtures = repository.load_targets(args.max_fixtures)
        provider = create_sports_provider(env, force_sample=args.sample)
        season = int(env.get("API_FOOTBALL_SEASON", "2026"))
        league_id = int(env.get("API_FOOTBALL_LEAGUE_ID", "1"))
        squads, statistics, injuries, lineups = collect(
            provider, teams, fixtures, season, league_id
        )
        counts = repository.store(
            teams, fixtures, squads, statistics, injuries, lineups
        )
        print(
            "Squad availability: "
            f"teams={len(teams)} fixtures={len(fixtures)} "
            f"players={counts['players']} "
            f"reports={counts['availability_reports']} "
            f"lineup_players={counts['lineup_players']}"
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
