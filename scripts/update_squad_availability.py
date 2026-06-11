#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select, update
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data_ingestion.providers import (
    RateLimitError,
    RequestLimitError,
    SportsProvider,
    create_sports_provider,
)
from modeling.src.data import load_teams
from scripts.database import create_database_engine

REQUIRED_TABLES = {
    "teams",
    "matches",
    "players",
    "player_availability_reports",
    "projected_lineups",
}
DEFAULT_MAX_FIXTURES = 1
DEFAULT_MAX_TEAMS = 2
DEFAULT_MAX_REQUESTS = 6
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0
DIAGNOSIS_PATH = (
    ROOT / "data" / "evaluation" / "squad_availability_diagnosis.json"
)
UPCOMING_FIXTURE_STATUSES = {"NS", "TBD"}


@dataclass
class CollectionResult:
    squads: dict[int, list[dict[str, Any]]]
    statistics: dict[int, list[dict[str, Any]]]
    injuries: dict[int, list[dict[str, Any]]]
    lineups: dict[int, list[dict[str, Any]]]
    request_limit_reached: bool = False
    rate_limited: bool = False


def _normalize_name(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_alias_lookup() -> dict[str, str]:
    aliases = json.loads(
        (ROOT / "data" / "seed" / "team_aliases.json").read_text()
    )
    lookup = {}
    for team in load_teams():
        for name in (team.name, *aliases.get(team.id, [])):
            lookup[_normalize_name(name)] = team.id
    return lookup


def build_canonical_team_mappings(
    provider_fixtures: list[dict[str, Any]],
    database_teams: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    aliases = canonical_alias_lookup()
    canonical_teams = {team.id: team for team in load_teams()}
    database_by_provider = {
        int(row["api_football_team_id"]): row
        for row in database_teams
        if row.get("api_football_team_id") is not None
    }
    database_by_canonical = {}
    for row in database_teams:
        canonical_id = aliases.get(_normalize_name(row.get("name")))
        if canonical_id:
            database_by_canonical[canonical_id] = row

    mappings = {}
    for fixture in provider_fixtures:
        for provider_team in (
            fixture.get("home_team") or {},
            fixture.get("away_team") or {},
        ):
            if provider_team.get("provider_id") is None:
                continue
            provider_team_id = int(provider_team["provider_id"])
            canonical_id = aliases.get(_normalize_name(provider_team.get("name")))
            if canonical_id is None:
                continue
            database_team = (
                database_by_provider.get(provider_team_id)
                or database_by_canonical.get(canonical_id)
            )
            canonical_team = canonical_teams[canonical_id]
            mappings[provider_team_id] = {
                "id": database_team.get("id") if database_team else None,
                "code": canonical_id,
                "name": canonical_team.name,
                "provider_name": provider_team.get("name"),
                "api_football_team_id": provider_team_id,
                "database_mapped": database_team is not None,
            }
    return mappings


def select_research_targets(
    provider_fixtures: list[dict[str, Any]],
    database_teams: list[dict[str, Any]],
    database_matches: list[dict[str, Any]],
    *,
    now: datetime,
    max_fixtures: int,
    max_teams: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    team_mappings = build_canonical_team_mappings(
        provider_fixtures,
        database_teams,
    )
    database_by_provider_fixture = {
        int(row["api_football_fixture_id"]): row
        for row in database_matches
        if row.get("api_football_fixture_id") is not None
    }
    database_teams_by_id = {
        row["id"]: row for row in database_teams if row.get("id") is not None
    }
    aliases = canonical_alias_lookup()
    candidates = []
    seen = set()
    for provider_fixture in sorted(
        provider_fixtures,
        key=lambda row: (
            _as_datetime(row.get("date"))
            or datetime.max.replace(tzinfo=timezone.utc),
            int(row["provider_fixture_id"]),
        ),
    ):
        provider_fixture_id = int(provider_fixture["provider_fixture_id"])
        if provider_fixture_id in seen:
            continue
        seen.add(provider_fixture_id)
        kickoff = _as_datetime(provider_fixture.get("date"))
        if kickoff is None or kickoff <= now:
            continue
        if provider_fixture.get("status") not in UPCOMING_FIXTURE_STATUSES:
            continue
        home_provider_id = int(provider_fixture["home_team"]["provider_id"])
        away_provider_id = int(provider_fixture["away_team"]["provider_id"])
        home_team = team_mappings.get(home_provider_id)
        away_team = team_mappings.get(away_provider_id)
        if home_team is None or away_team is None:
            continue
        database_match = database_by_provider_fixture.get(provider_fixture_id)
        candidates.append(
            {
                "id": database_match.get("id") if database_match else None,
                "fixture_id": database_match.get("id") if database_match else None,
                "api_football_fixture_id": provider_fixture_id,
                "provider_fixture_id": provider_fixture_id,
                "date": provider_fixture.get("date"),
                "status": provider_fixture.get("status"),
                "home_team_id": home_team.get("id"),
                "away_team_id": away_team.get("id"),
                "canonical_home_team_code": home_team["code"],
                "canonical_away_team_code": away_team["code"],
                "home_provider_team_id": home_provider_id,
                "away_provider_team_id": away_provider_id,
                "home_team_name": provider_fixture["home_team"]["name"],
                "away_team_name": provider_fixture["away_team"]["name"],
                "target_source": (
                    "database_match" if database_match else "provider_schedule"
                ),
            }
        )

    for provider_fixture_id, database_match in sorted(
        database_by_provider_fixture.items(),
        key=lambda item: (
            _as_datetime(
                item[1].get("match_date") or item[1].get("kickoff")
            )
            or datetime.max.replace(tzinfo=timezone.utc),
            item[0],
        ),
    ):
        if provider_fixture_id in seen:
            continue
        kickoff = _as_datetime(
            database_match.get("match_date") or database_match.get("kickoff")
        )
        if kickoff is None or kickoff <= now or database_match.get("completed"):
            continue
        home_database_team = database_teams_by_id.get(
            database_match.get("home_team_id")
        )
        away_database_team = database_teams_by_id.get(
            database_match.get("away_team_id")
        )
        if not home_database_team or not away_database_team:
            continue
        if (
            home_database_team.get("api_football_team_id") is None
            or away_database_team.get("api_football_team_id") is None
        ):
            continue
        home_code = aliases.get(
            _normalize_name(home_database_team.get("name"))
        )
        away_code = aliases.get(
            _normalize_name(away_database_team.get("name"))
        )
        if home_code is None or away_code is None:
            continue
        home_provider_id = int(home_database_team["api_football_team_id"])
        away_provider_id = int(away_database_team["api_football_team_id"])
        team_mappings.setdefault(
            home_provider_id,
            {
                "id": home_database_team["id"],
                "code": home_code,
                "name": home_database_team["name"],
                "provider_name": home_database_team["name"],
                "api_football_team_id": home_provider_id,
                "database_mapped": True,
            },
        )
        team_mappings.setdefault(
            away_provider_id,
            {
                "id": away_database_team["id"],
                "code": away_code,
                "name": away_database_team["name"],
                "provider_name": away_database_team["name"],
                "api_football_team_id": away_provider_id,
                "database_mapped": True,
            },
        )
        candidates.append(
            {
                "id": database_match["id"],
                "fixture_id": database_match["id"],
                "api_football_fixture_id": provider_fixture_id,
                "provider_fixture_id": provider_fixture_id,
                "date": kickoff.isoformat(),
                "status": "NS",
                "home_team_id": home_database_team["id"],
                "away_team_id": away_database_team["id"],
                "canonical_home_team_code": home_code,
                "canonical_away_team_code": away_code,
                "home_provider_team_id": home_provider_id,
                "away_provider_team_id": away_provider_id,
                "home_team_name": home_database_team["name"],
                "away_team_name": away_database_team["name"],
                "target_source": "database_match",
            }
        )

    candidates.sort(
        key=lambda row: (
            _as_datetime(row.get("date"))
            or datetime.max.replace(tzinfo=timezone.utc),
            row["provider_fixture_id"],
        )
    )
    selected_fixtures = []
    selected_provider_team_ids = []
    for fixture in candidates:
        fixture_team_ids = (
            fixture["home_provider_team_id"],
            fixture["away_provider_team_id"],
        )
        additional = [
            team_id
            for team_id in fixture_team_ids
            if team_id not in selected_provider_team_ids
        ]
        if len(selected_provider_team_ids) + len(additional) > max_teams:
            continue
        selected_fixtures.append(fixture)
        selected_provider_team_ids.extend(additional)
        if len(selected_fixtures) >= max_fixtures:
            break

    return (
        [team_mappings[team_id] for team_id in selected_provider_team_ids],
        selected_fixtures,
    )


def diagnose_provider_fixtures(
    provider_fixtures: list[dict[str, Any]],
    database_teams: list[dict[str, Any]],
    database_matches: list[dict[str, Any]],
    *,
    now: datetime,
    logger: logging.Logger,
) -> dict[str, Any]:
    aliases = canonical_alias_lookup()
    canonical_database_rows = {}
    for row in database_teams:
        canonical_id = aliases.get(_normalize_name(row.get("name")))
        if canonical_id:
            canonical_database_rows[canonical_id] = row

    database_fixtures = {
        int(row["api_football_fixture_id"]): row
        for row in database_matches
        if row.get("api_football_fixture_id") is not None
    }
    exclusion_counts = {
        "date_filtering": 0,
        "fixture_status": 0,
        "missing_team_mappings": 0,
        "provider_backed_team_requirements": 0,
        "duplicate_filtering": 0,
        "database_fixture_missing": 0,
    }
    mapping_audit: dict[int, dict[str, Any]] = {}
    fixture_diagnostics = []
    seen_fixture_ids = set()

    logger.info(
        "[diagnosis] Provider returned %d fixtures",
        len(provider_fixtures),
    )
    for fixture in provider_fixtures[:5]:
        logger.info(
            "[diagnosis] Provider fixture sample: id=%s %s vs %s "
            "date=%s status=%s",
            fixture.get("provider_fixture_id"),
            (fixture.get("home_team") or {}).get("name"),
            (fixture.get("away_team") or {}).get("name"),
            fixture.get("date"),
            fixture.get("status"),
        )

    for fixture in provider_fixtures:
        fixture_id = int(fixture["provider_fixture_id"])
        home = fixture["home_team"]
        away = fixture["away_team"]
        kickoff = _as_datetime(fixture.get("date"))
        reasons = []

        if fixture_id in seen_fixture_ids:
            reasons.append("duplicate_filtering")
        seen_fixture_ids.add(fixture_id)
        if kickoff is None or kickoff <= now:
            reasons.append("date_filtering")
        if fixture.get("status") not in UPCOMING_FIXTURE_STATUSES:
            reasons.append("fixture_status")

        fixture_mappings = {}
        for side, provider_team in (("home", home), ("away", away)):
            provider_team_id = int(provider_team["provider_id"])
            canonical_id = aliases.get(_normalize_name(provider_team["name"]))
            database_team = (
                canonical_database_rows.get(canonical_id)
                if canonical_id is not None
                else None
            )
            provider_backed = bool(
                database_team
                and database_team.get("api_football_team_id") is not None
                and int(database_team["api_football_team_id"])
                == provider_team_id
            )
            mapping = {
                "provider_team_id": provider_team_id,
                "provider_name": provider_team["name"],
                "canonical_team_id": canonical_id,
                "database_team_id": (
                    str(database_team["id"]) if database_team else None
                ),
                "database_team_name": (
                    database_team.get("name") if database_team else None
                ),
                "database_provider_team_id": (
                    database_team.get("api_football_team_id")
                    if database_team
                    else None
                ),
                "canonical_mapping_found": canonical_id is not None,
                "database_mapping_found": database_team is not None,
                "provider_backed": provider_backed,
            }
            fixture_mappings[side] = mapping
            mapping_audit[provider_team_id] = mapping

        if any(
            not mapping["canonical_mapping_found"]
            for mapping in fixture_mappings.values()
        ):
            reasons.append("missing_team_mappings")
        if any(
            not mapping["provider_backed"]
            for mapping in fixture_mappings.values()
        ):
            exclusion_counts["provider_backed_team_requirements"] += 1
        if fixture_id not in database_fixtures:
            exclusion_counts["database_fixture_missing"] += 1

        unique_reasons = list(dict.fromkeys(reasons))
        for reason in unique_reasons:
            exclusion_counts[reason] += 1
        if unique_reasons:
            logger.info(
                "[diagnosis] Excluded fixture id=%s %s vs %s: %s",
                fixture_id,
                home["name"],
                away["name"],
                ", ".join(unique_reasons),
            )
        else:
            logger.info(
                "[diagnosis] Eligible fixture id=%s %s vs %s",
                fixture_id,
                home["name"],
                away["name"],
            )
        fixture_diagnostics.append(
            {
                "provider_fixture_id": fixture_id,
                "date": fixture.get("date"),
                "status": fixture.get("status"),
                "home_team": fixture_mappings["home"],
                "away_team": fixture_mappings["away"],
                "database_fixture_found": fixture_id in database_fixtures,
                "excluded": bool(unique_reasons),
                "exclusion_reasons": unique_reasons,
            }
        )

    eligible = [
        row for row in fixture_diagnostics if not row["excluded"]
    ]
    mapping_rows = sorted(
        mapping_audit.values(),
        key=lambda row: (row["provider_name"], row["provider_team_id"]),
    )
    future_provider = [
        row
        for row in fixture_diagnostics
        if "date_filtering" not in row["exclusion_reasons"]
        and "fixture_status" not in row["exclusion_reasons"]
    ]
    missing_database_fixture = sum(
        not row["database_fixture_found"]
        for row in future_provider
    )
    diagnosis = (
        "No fixtures were returned by API-Football."
        if not provider_fixtures
        else (
            f"API-Football returned {len(provider_fixtures)} fixtures and "
            f"{len(future_provider)} pass date/status checks, but "
            f"{missing_database_fixture} of those are absent from the database "
            "as rows with api_football_fixture_id. Provider-only research "
            "targets remain eligible and use canonical team codes."
        )
        if missing_database_fixture
        else (
            f"API-Football returned {len(provider_fixtures)} fixtures; "
            f"{len(eligible)} satisfy all diagnostic requirements."
        )
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider_fixture_count": len(provider_fixtures),
        "first_five_provider_fixtures": [
            {
                "provider_fixture_id": row.get("provider_fixture_id"),
                "home_team": (row.get("home_team") or {}).get("name"),
                "away_team": (row.get("away_team") or {}).get("name"),
                "date": row.get("date"),
                "status": row.get("status"),
            }
            for row in provider_fixtures[:5]
        ],
        "exclusion_counts": exclusion_counts,
        "eligible_fixture_count": len(eligible),
        "canonical_mapping_summary": {
            "provider_teams": len(mapping_rows),
            "canonical_mappings_found": sum(
                row["canonical_mapping_found"] for row in mapping_rows
            ),
            "database_mappings_found": sum(
                row["database_mapping_found"] for row in mapping_rows
            ),
            "provider_backed_mappings": sum(
                row["provider_backed"] for row in mapping_rows
            ),
        },
        "canonical_mapping_audit": mapping_rows,
        "fixtures": fixture_diagnostics,
        "diagnosis": diagnosis,
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

    def load_diagnostic_snapshot(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        teams = self._table("teams")
        matches = self._table("matches")
        with self.engine.connect() as connection:
            return (
                [
                    dict(row)
                    for row in connection.execute(select(teams)).mappings()
                ],
                [
                    dict(row)
                    for row in connection.execute(select(matches)).mappings()
                ],
            )

    def load_targets(
        self,
        provider_fixtures: list[dict[str, Any]],
        max_fixtures: int,
        max_teams: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        database_teams, database_matches = self.load_diagnostic_snapshot()
        return select_research_targets(
            provider_fixtures,
            database_teams,
            database_matches,
            now=datetime.now(timezone.utc),
            max_fixtures=max_fixtures,
            max_teams=max_teams,
        )

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

    @staticmethod
    def _insert_values(table: Table, values: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in values.items() if key in table.c}

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
        team_by_code = {team["code"]: team for team in teams}
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
                    team_by_code.get(fixture.get(column))
                    for column in (
                        "canonical_home_team_code",
                        "canonical_away_team_code",
                    )
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
                            "canonical_home_team_code": fixture[
                                "canonical_home_team_code"
                            ],
                            "canonical_away_team_code": fixture[
                                "canonical_away_team_code"
                            ],
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
                            availability_table.insert().values(
                                **self._insert_values(
                                    availability_table,
                                    values,
                                )
                            )
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
                        "canonical_home_team_code": fixture[
                            "canonical_home_team_code"
                        ],
                        "canonical_away_team_code": fixture[
                            "canonical_away_team_code"
                        ],
                        "expected_return": row.get("expected_return"),
                        "source": "api_football",
                        "collected_at": collected_at,
                        "raw_payload": self._json_value(
                            availability_table, "raw_payload", row.get("raw", {})
                        ),
                    }
                    connection.execute(
                        availability_table.insert().values(
                            **self._insert_values(
                                availability_table,
                                values,
                            )
                        )
                    )
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
                                "canonical_home_team_code": fixture[
                                    "canonical_home_team_code"
                                ],
                                "canonical_away_team_code": fixture[
                                    "canonical_away_team_code"
                                ],
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
                            connection.execute(
                                lineup_table.insert().values(
                                    **self._insert_values(lineup_table, values)
                                )
                            )
                            counts["lineup_players"] += 1
        return counts


def collect(
    provider: SportsProvider,
    teams: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    season: int,
    league_id: int,
    logger: logging.Logger,
) -> CollectionResult:
    squads = {}
    statistics: dict[int, list[dict[str, Any]]] = {}
    injuries: dict[int, list[dict[str, Any]]] = {}
    lineups: dict[int, list[dict[str, Any]]] = {}
    result = CollectionResult(squads, statistics, injuries, lineups)

    try:
        for index, team in enumerate(teams, start=1):
            provider_team_id = int(team["api_football_team_id"])
            logger.info(
                "[collect] Team %d/%d: %s provider_team_id=%s squad",
                index,
                len(teams),
                team.get("name") or team.get("code") or team["id"],
                provider_team_id,
            )
            squads[provider_team_id] = provider.get_squad(provider_team_id)
            logger.info(
                "[collect] Team provider_team_id=%s squad_players=%d",
                provider_team_id,
                len(squads[provider_team_id]),
            )

        for index, fixture in enumerate(fixtures, start=1):
            provider_fixture_id = int(fixture["api_football_fixture_id"])
            logger.info(
                "[collect] Fixture %d/%d: provider_fixture_id=%s injuries",
                index,
                len(fixtures),
                provider_fixture_id,
            )
            injuries[provider_fixture_id] = provider.get_injuries(
                fixture_id=provider_fixture_id
            )
            logger.info(
                "[collect] Fixture provider_fixture_id=%s injuries=%d lineups",
                provider_fixture_id,
                len(injuries[provider_fixture_id]),
            )
            lineups[provider_fixture_id] = provider.get_lineups(
                provider_fixture_id
            )
            logger.info(
                "[collect] Fixture provider_fixture_id=%s lineup_teams=%d",
                provider_fixture_id,
                len(lineups[provider_fixture_id]),
            )

        for index, team in enumerate(teams, start=1):
            provider_team_id = int(team["api_football_team_id"])
            logger.info(
                "[collect] Team %d/%d: provider_team_id=%s player statistics",
                index,
                len(teams),
                provider_team_id,
            )
            statistics[provider_team_id] = provider.get_player_statistics(
                season=season,
                league_id=league_id,
                team_id=provider_team_id,
            )
            logger.info(
                "[collect] Team provider_team_id=%s player_stat_rows=%d",
                provider_team_id,
                len(statistics[provider_team_id]),
            )
    except RequestLimitError as error:
        result.request_limit_reached = True
        logger.warning("[collect] %s; storing collected research data", error)
    except RateLimitError as error:
        result.rate_limited = True
        logger.warning("[collect] %s; storing collected research data", error)

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect research-only squad, injury, suspension, and lineup data."
    )
    parser.add_argument(
        "--max-fixtures",
        type=int,
        default=DEFAULT_MAX_FIXTURES,
        help=f"Maximum upcoming fixtures to process (default: {DEFAULT_MAX_FIXTURES}).",
    )
    parser.add_argument(
        "--max-teams",
        type=int,
        default=DEFAULT_MAX_TEAMS,
        help=f"Maximum fixture-linked teams to process (default: {DEFAULT_MAX_TEAMS}).",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help=f"Maximum API-Football HTTP requests (default: {DEFAULT_MAX_REQUESTS}).",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help=(
            "Timeout for each API-Football HTTP request in seconds "
            f"(default: {DEFAULT_REQUEST_TIMEOUT_SECONDS:g})."
        ),
    )
    parser.add_argument("--sample", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("update_squad_availability")
    logger.info(
        "[squad-availability] START max_fixtures=%d max_teams=%d "
        "max_requests=%d request_timeout=%.1fs sample=%s",
        args.max_fixtures,
        args.max_teams,
        args.max_requests,
        args.request_timeout,
        args.sample,
    )
    if (
        args.max_fixtures < 1
        or args.max_teams < 1
        or args.max_requests < 1
        or args.request_timeout <= 0
    ):
        logger.error(
            "Limits must be positive: --max-fixtures, --max-teams, "
            "--max-requests, and --request-timeout"
        )
        return 2

    logger.info("[setup] Loading environment")
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    env = dict(os.environ)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        logger.error("[squad-availability] FAILED: DATABASE_URL is required")
        return 2

    env["API_FOOTBALL_REQUEST_TIMEOUT_SECONDS"] = str(args.request_timeout)
    logger.info("[setup] Initializing database and provider")
    try:
        engine = create_database_engine(database_url)
        provider = create_sports_provider(env, logger, force_sample=args.sample)
    except Exception:
        logger.exception("[squad-availability] FAILED during initialization")
        return 1
    if hasattr(provider, "max_requests"):
        provider.max_requests = args.max_requests
        provider.request_count = 0

    try:
        repository = SquadAvailabilityRepository(engine)
        logger.info("[setup] Validating research schema")
        repository.assert_schema()
        season = int(env.get("API_FOOTBALL_SEASON", "2026"))
        league_id = int(env.get("API_FOOTBALL_LEAGUE_ID", "1"))
        logger.info(
            "[diagnosis] Fetching provider schedule league=%d season=%d",
            league_id,
            season,
        )
        try:
            provider_fixtures = provider.get_fixtures(
                league_id=league_id,
                season=season,
            )
        except (RateLimitError, RequestLimitError) as error:
            logger.warning(
                "[diagnosis] Could not fetch provider schedule: %s",
                error,
            )
            provider_fixtures = []
        database_teams, database_matches = repository.load_diagnostic_snapshot()
        diagnosis = diagnose_provider_fixtures(
            provider_fixtures,
            database_teams,
            database_matches,
            now=datetime.now(timezone.utc),
            logger=logger,
        )
        for mapping in diagnosis["canonical_mapping_audit"]:
            logger.info(
                "[diagnosis] Team mapping: provider_id=%s name=%s "
                "canonical=%s database_team_id=%s provider_backed=%s",
                mapping["provider_team_id"],
                mapping["provider_name"],
                mapping["canonical_team_id"],
                mapping["database_team_id"],
                mapping["provider_backed"],
            )

        logger.info("[targets] Loading upcoming provider-backed fixtures")
        teams, fixtures = select_research_targets(
            provider_fixtures,
            database_teams,
            database_matches,
            now=datetime.now(timezone.utc),
            max_fixtures=args.max_fixtures,
            max_teams=args.max_teams,
        )
        diagnosis["target_selection"] = {
            "selected_fixtures": len(fixtures),
            "selected_teams": len(teams),
            "max_fixtures": args.max_fixtures,
            "max_teams": args.max_teams,
            "fixtures": [
                {
                    "provider_fixture_id": fixture["provider_fixture_id"],
                    "fixture_id": (
                        str(fixture["fixture_id"])
                        if fixture.get("fixture_id") is not None
                        else None
                    ),
                    "canonical_home_team_code": fixture[
                        "canonical_home_team_code"
                    ],
                    "canonical_away_team_code": fixture[
                        "canonical_away_team_code"
                    ],
                    "target_source": fixture["target_source"],
                }
                for fixture in fixtures
            ],
        }
        diagnosis["selection_explanation"] = (
            diagnosis["diagnosis"]
            if not fixtures and not teams
            else (
                "Research targets were selected from the merged provider "
                "schedule and database match set."
            )
        )
        DIAGNOSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DIAGNOSIS_PATH.write_text(
            json.dumps(diagnosis, indent=2, default=str) + "\n"
        )
        logger.info(
            "[targets] Selected fixtures=%d teams=%d",
            len(fixtures),
            len(teams),
        )
        logger.info("[diagnosis] %s", diagnosis["selection_explanation"])
        logger.info(
            "[diagnosis] Wrote %s",
            DIAGNOSIS_PATH,
        )
        if not fixtures:
            requests = getattr(provider, "request_count", 0)
            logger.info(
                "[squad-availability] SUMMARY provider_fixtures_seen=%d "
                "selected_fixtures=0 selected_teams=0 requests_made=%d "
                "rows_written=0",
                len(provider_fixtures),
                requests,
            )
            return 0
        if not teams:
            requests = getattr(provider, "request_count", 0)
            logger.info(
                "[squad-availability] SUCCESS: no provider-backed teams for "
                "selected fixtures; requests=%d rows_written=0",
                requests,
            )
            return 0

        result = collect(
            provider,
            teams,
            fixtures,
            season,
            league_id,
            logger,
        )
        logger.info("[store] Persisting collected research rows")
        counts = repository.store(
            teams,
            fixtures,
            result.squads,
            result.statistics,
            result.injuries,
            result.lineups,
        )
        requests = getattr(provider, "request_count", 0)
        rows_written = sum(counts.values())
        logger.info(
            "[squad-availability] SUMMARY provider_fixtures_seen=%d "
            "selected_fixtures=%d selected_teams=%d requests_made=%d "
            "rows_written=%d request_limit_reached=%s rate_limited=%s",
            len(provider_fixtures),
            len(fixtures),
            len(teams),
            requests,
            rows_written,
            result.request_limit_reached,
            result.rate_limited,
        )
        return 0
    except Exception:
        logger.exception("[squad-availability] FAILED")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
