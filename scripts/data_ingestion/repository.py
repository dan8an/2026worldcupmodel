from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("%", ""))
    except ValueError:
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else round(number)


def _stat(stats: dict[str, Any], *names: str) -> Any:
    return next((stats[name] for name in names if stats.get(name) is not None), None)


COMPLETED_STATUSES = {"FT", "AET", "PEN", "completed", "finished"}


class DataIngestionRepository:
    def __init__(self, engine: Engine, logger: logging.Logger | None = None) -> None:
        self.engine = engine
        self.logger = logger or logging.getLogger(__name__)

    def assert_schema(self) -> None:
        with self.engine.connect() as connection:
            schema = connection.execute(
                text(
                    """
                    select
                      to_regclass('public.players') is not null as players,
                      to_regclass('public.team_match_stats') is not null as team_stats,
                      to_regclass('public.player_match_stats') is not null as player_stats,
                      exists (
                        select 1 from information_schema.columns
                        where table_schema = 'public'
                          and table_name = 'matches'
                          and column_name = 'api_football_fixture_id'
                      ) as provider_fixture_id,
                      (
                        select count(*) = 5
                        from information_schema.columns
                        where table_schema = 'public'
                          and table_name = 'team_match_stats'
                          and column_name in (
                            'shots_inside_box',
                            'shots_outside_box',
                            'blocked_shots',
                            'goalkeeper_saves',
                            'pass_accuracy'
                          )
                      ) as xg_proxy_fields
                    """
                )
            ).mappings().one()
        if not all(schema.values()):
            raise RuntimeError(
                "Daily pipeline schema is missing. Apply "
                "supabase/migrations/202606100001_daily_prediction_pipeline.sql and "
                "supabase/migrations/202606110003_xg_proxy_v4.sql first."
            )

    def upsert_provider_matches(
        self,
        matches: list[dict[str, Any]],
        connection: Connection | None = None,
    ) -> int:
        context = nullcontext(connection) if connection is not None else self.engine.begin()
        with context as active_connection:
            for match in matches:
                home_team_id = self._upsert_team(active_connection, match["home_team"])
                away_team_id = self._upsert_team(active_connection, match["away_team"])
                self._upsert_match(
                    active_connection,
                    match,
                    home_team_id,
                    away_team_id,
                )
        return len(matches)

    def find_completed_matches_missing_stats(
        self,
        provider_fixture_ids: list[int],
        connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        if not provider_fixture_ids:
            return []
        context = nullcontext(connection) if connection is not None else self.engine.connect()
        with context as active_connection:
            return list(
                active_connection.execute(
                    text(
                        """
                        select
                          m.id,
                          m.api_football_fixture_id,
                          m.home_team_id,
                          m.away_team_id,
                          m.home_team,
                          m.away_team,
                          m.home_score,
                          m.away_score,
                          m.match_date,
                          m.tournament_stage,
                          count(distinct tms.id) as team_stat_rows,
                          count(distinct pms.id) as player_stat_rows
                        from public.matches m
                        left join public.team_match_stats tms on tms.match_id = m.id
                        left join public.player_match_stats pms on pms.match_id = m.id
                        where m.completed = true
                          and m.api_football_fixture_id = any(:fixture_ids)
                        group by m.id
                        having count(distinct tms.id) < 2
                            or count(distinct pms.id) = 0
                        order by m.match_date, m.id
                        """
                    ),
                    {"fixture_ids": provider_fixture_ids},
                ).mappings()
            )

    def find_provider_fixtures_with_complete_team_stats(
        self,
        provider_fixture_ids: list[int],
        connection: Connection | None = None,
    ) -> set[int]:
        if not provider_fixture_ids:
            return set()
        context = nullcontext(connection) if connection is not None else self.engine.connect()
        with context as active_connection:
            return set(
                active_connection.execute(
                    text(
                        """
                        select m.api_football_fixture_id
                        from public.matches m
                        join public.team_match_stats tms on tms.match_id = m.id
                        where m.api_football_fixture_id = any(:fixture_ids)
                        group by m.id, m.api_football_fixture_id
                        having count(distinct tms.team_id) >= 2
                        """
                    ),
                    {"fixture_ids": provider_fixture_ids},
                ).scalars()
            )

    def ingest_fixture(
        self,
        match: dict[str, Any],
        fixture: dict[str, Any],
        statistics: list[dict[str, Any]],
        players: list[dict[str, Any]],
        lineups: list[dict[str, Any]],
        connection: Connection | None = None,
    ) -> dict[str, int]:
        context = nullcontext(connection) if connection is not None else self.engine.begin()
        with context as active_connection:
            team_ids = {
                fixture["home_team"]["provider_id"]: match["home_team_id"],
                fixture["away_team"]["provider_id"]: match["away_team_id"],
            }
            for item in statistics:
                team_id = team_ids.get(item["team"]["provider_id"])
                if team_id is None:
                    continue
                is_home = item["team"]["provider_id"] == fixture["home_team"]["provider_id"]
                self._upsert_team_stats(
                    active_connection,
                    match,
                    fixture,
                    item,
                    team_id,
                    match["away_team_id"] if is_home else match["home_team_id"],
                    is_home,
                )

            starters = {
                player["provider_id"]
                for lineup in lineups
                for player in lineup["starters"]
            }
            lineup_players = {
                player["provider_id"]: {**player, "team": lineup["team"]}
                for lineup in lineups
                for player in lineup["starters"] + lineup["substitutes"]
            }
            appearances = {row["player"]["provider_id"]: row for row in players}
            for provider_id, lineup_player in lineup_players.items():
                appearances.setdefault(
                    provider_id,
                    {
                        "team": lineup_player["team"],
                        "player": {
                            "provider_id": provider_id,
                            "name": lineup_player["name"],
                        },
                        "appearance": {
                            "minutes": None,
                            "position": lineup_player.get("position"),
                        },
                        "raw": {"lineup_only": True},
                    },
                )

            for player in appearances.values():
                team_id = team_ids.get(player["team"]["provider_id"])
                if team_id is None:
                    continue
                is_home = player["team"]["provider_id"] == fixture["home_team"]["provider_id"]
                player_id = self._upsert_player(active_connection, player, team_id)
                self._upsert_player_stats(
                    active_connection,
                    match,
                    fixture,
                    player,
                    player_id,
                    team_id,
                    match["away_team_id"] if is_home else match["home_team_id"],
                    player["player"]["provider_id"] in starters,
                )
        return {"team_stats": len(statistics), "player_stats": len(appearances)}

    def ingest_historical_team_fixture(
        self,
        fixture: dict[str, Any],
        statistics: list[dict[str, Any]],
        connection: Connection | None = None,
    ) -> dict[str, int]:
        """Upsert one completed fixture and only its team-level statistics."""
        context = nullcontext(connection) if connection is not None else self.engine.begin()
        with context as active_connection:
            existing_match_id = active_connection.execute(
                text(
                    """
                    select id from public.matches
                    where api_football_fixture_id = :fixture_id
                    """
                ),
                {"fixture_id": fixture["provider_fixture_id"]},
            ).scalar_one_or_none()

            self.upsert_provider_matches([fixture], connection=active_connection)
            match = active_connection.execute(
                text(
                    """
                    select id, home_team_id, away_team_id
                    from public.matches
                    where api_football_fixture_id = :fixture_id
                    """
                ),
                {"fixture_id": fixture["provider_fixture_id"]},
            ).mappings().one()
            team_ids = {
                fixture["home_team"]["provider_id"]: match["home_team_id"],
                fixture["away_team"]["provider_id"]: match["away_team_id"],
            }
            existing_team_ids = set(
                active_connection.execute(
                    text(
                        """
                        select team_id from public.team_match_stats
                        where match_id = :match_id
                        """
                    ),
                    {"match_id": match["id"]},
                ).scalars()
            )

            stored_team_ids: set[Any] = set()
            for item in statistics:
                provider_team_id = item["team"]["provider_id"]
                team_id = team_ids.get(provider_team_id)
                if team_id is None:
                    continue
                is_home = provider_team_id == fixture["home_team"]["provider_id"]
                self._upsert_team_stats(
                    active_connection,
                    match,
                    fixture,
                    item,
                    team_id,
                    match["away_team_id"] if is_home else match["home_team_id"],
                    is_home,
                )
                stored_team_ids.add(team_id)

        inserted_team_ids = stored_team_ids - existing_team_ids
        return {
            "fixtures_inserted": int(existing_match_id is None),
            "fixtures_updated": int(existing_match_id is not None),
            "team_stats_inserted": len(inserted_team_ids),
            "team_stats_updated": len(stored_team_ids - inserted_team_ids),
        }

    @staticmethod
    def _upsert_team(connection: Connection, team: dict[str, Any]) -> Any:
        existing = connection.execute(
            text(
                """
                select id from public.teams
                where api_football_team_id = :provider_id
                   or lower(name) = lower(:name)
                order by (api_football_team_id = :provider_id) desc
                limit 1
                """
            ),
            team,
        ).scalar_one_or_none()
        if existing:
            connection.execute(
                text(
                    """
                    update public.teams
                    set api_football_team_id = coalesce(api_football_team_id, :provider_id)
                    where id = :id
                    """
                ),
                {**team, "id": existing},
            )
            return existing
        return connection.execute(
            text(
                """
                insert into public.teams (name, api_football_team_id)
                values (:name, :provider_id)
                returning id
                """
            ),
            team,
        ).scalar_one()

    @staticmethod
    def _upsert_match(
        connection: Connection,
        match: dict[str, Any],
        home_team_id: Any,
        away_team_id: Any,
    ) -> None:
        status = str(match.get("status") or "").strip()
        completed = (
            status in COMPLETED_STATUSES
            or (
                match.get("home_score") is not None
                and match.get("away_score") is not None
            )
        )
        connection.execute(
            text(
                """
                insert into public.matches (
                  home_team, away_team, match_date, tournament_stage,
                  home_score, away_score, completed, home_team_id, away_team_id,
                  api_football_fixture_id, provider_name, provider_payload, updated_at
                )
                values (
                  :home_name, :away_name, :match_date, :stage,
                  :home_score, :away_score, :completed, :home_team_id, :away_team_id,
                  :fixture_id, 'api_football', cast(:raw as jsonb), now()
                )
                on conflict (api_football_fixture_id)
                  where api_football_fixture_id is not null
                do update set
                  home_team = excluded.home_team,
                  away_team = excluded.away_team,
                  match_date = excluded.match_date,
                  tournament_stage = excluded.tournament_stage,
                  home_score = excluded.home_score,
                  away_score = excluded.away_score,
                  completed = excluded.completed,
                  home_team_id = excluded.home_team_id,
                  away_team_id = excluded.away_team_id,
                  provider_payload = excluded.provider_payload,
                  updated_at = now()
                """
            ),
            {
                "home_name": match["home_team"]["name"],
                "away_name": match["away_team"]["name"],
                "match_date": match["date"],
                "stage": match.get("round") or match.get("competition"),
                "home_score": match.get("home_score"),
                "away_score": match.get("away_score"),
                "completed": completed,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "fixture_id": match["provider_fixture_id"],
                "raw": json.dumps(match.get("raw", {})),
            },
        )

    @staticmethod
    def _upsert_player(
        connection: Connection,
        player: dict[str, Any],
        team_id: Any,
    ) -> Any:
        provider_key = f"api_football:{player['player']['provider_id']}"
        return connection.execute(
            text(
                """
                insert into public.players (
                  team_id, provider_key, display_name, primary_position, updated_at
                )
                values (:team_id, :provider_key, :name, :position, now())
                on conflict (provider_key) where provider_key is not null
                do update set
                  team_id = excluded.team_id,
                  display_name = excluded.display_name,
                  primary_position = coalesce(
                    excluded.primary_position,
                    public.players.primary_position
                  ),
                  active = true,
                  updated_at = now()
                returning id
                """
            ),
            {
                "team_id": team_id,
                "provider_key": provider_key,
                "name": player["player"]["name"],
                "position": player.get("appearance", {}).get("position"),
            },
        ).scalar_one()

    @staticmethod
    def _upsert_team_stats(
        connection: Connection,
        match: dict[str, Any],
        fixture: dict[str, Any],
        item: dict[str, Any],
        team_id: Any,
        opponent_team_id: Any,
        is_home: bool,
    ) -> None:
        stats = item["statistics"]
        connection.execute(
            text(
                """
                insert into public.team_match_stats (
                  match_id, team_id, opponent_team_id, is_home, goals,
                  expected_goals, possession, shots, shots_on_target,
                  shots_inside_box, shots_outside_box, blocked_shots,
                  goalkeeper_saves, corners, fouls, yellow_cards, red_cards,
                  passes_attempted, passes_completed, pass_accuracy,
                  source_name, source_match_key, captured_at, raw_payload
                )
                values (
                  :match_id, :team_id, :opponent_team_id, :is_home, :goals,
                  :expected_goals, :possession, :shots, :shots_on_target,
                  :shots_inside_box, :shots_outside_box, :blocked_shots,
                  :goalkeeper_saves, :corners, :fouls, :yellow_cards, :red_cards,
                  :passes_attempted, :passes_completed, :pass_accuracy,
                  'api_football', :source_match_key, now(),
                  cast(:raw as jsonb)
                )
                on conflict (match_id, team_id)
                do update set
                  opponent_team_id = excluded.opponent_team_id,
                  is_home = excluded.is_home,
                  goals = excluded.goals,
                  expected_goals = excluded.expected_goals,
                  possession = excluded.possession,
                  shots = excluded.shots,
                  shots_on_target = excluded.shots_on_target,
                  shots_inside_box = excluded.shots_inside_box,
                  shots_outside_box = excluded.shots_outside_box,
                  blocked_shots = excluded.blocked_shots,
                  goalkeeper_saves = excluded.goalkeeper_saves,
                  corners = excluded.corners,
                  fouls = excluded.fouls,
                  yellow_cards = excluded.yellow_cards,
                  red_cards = excluded.red_cards,
                  passes_attempted = excluded.passes_attempted,
                  passes_completed = excluded.passes_completed,
                  pass_accuracy = excluded.pass_accuracy,
                  captured_at = excluded.captured_at,
                  raw_payload = excluded.raw_payload
                """
            ),
            {
                "match_id": match["id"],
                "team_id": team_id,
                "opponent_team_id": opponent_team_id,
                "is_home": is_home,
                "goals": fixture["home_score"] if is_home else fixture["away_score"],
                "expected_goals": _number(_stat(stats, "Expected Goals")),
                "possession": _number(_stat(stats, "Ball Possession")),
                "shots": _integer(_stat(stats, "Total Shots")),
                "shots_on_target": _integer(_stat(stats, "Shots on Goal")),
                "shots_inside_box": _integer(_stat(stats, "Shots insidebox")),
                "shots_outside_box": _integer(_stat(stats, "Shots outsidebox")),
                "blocked_shots": _integer(_stat(stats, "Blocked Shots")),
                "goalkeeper_saves": _integer(_stat(stats, "Goalkeeper Saves")),
                "corners": _integer(_stat(stats, "Corner Kicks")),
                "fouls": _integer(_stat(stats, "Fouls")),
                "yellow_cards": _integer(_stat(stats, "Yellow Cards")),
                "red_cards": _integer(_stat(stats, "Red Cards")),
                "passes_attempted": _integer(_stat(stats, "Total passes")),
                "passes_completed": _integer(_stat(stats, "Passes accurate")),
                "pass_accuracy": _number(_stat(stats, "Passes %")),
                "source_match_key": str(fixture["provider_fixture_id"]),
                "raw": json.dumps(item.get("raw", item)),
            },
        )

    @staticmethod
    def _upsert_player_stats(
        connection: Connection,
        match: dict[str, Any],
        fixture: dict[str, Any],
        player: dict[str, Any],
        player_id: Any,
        team_id: Any,
        opponent_team_id: Any,
        started: bool,
    ) -> None:
        appearance = player.get("appearance", {})
        connection.execute(
            text(
                """
                insert into public.player_match_stats (
                  match_id, player_id, team_id, opponent_team_id, started,
                  minutes_played, goals, assists, shots, shots_on_target,
                  key_passes, tackles, interceptions, saves, yellow_cards,
                  red_cards, source_name, source_player_key, captured_at, raw_payload
                )
                values (
                  :match_id, :player_id, :team_id, :opponent_team_id, :started,
                  :minutes, :goals, :assists, :shots, :shots_on_target,
                  :key_passes, :tackles, :interceptions, :saves, :yellow_cards,
                  :red_cards, 'api_football', :source_player_key, now(),
                  cast(:raw as jsonb)
                )
                on conflict (match_id, player_id, team_id)
                do update set
                  opponent_team_id = excluded.opponent_team_id,
                  started = excluded.started,
                  minutes_played = excluded.minutes_played,
                  goals = excluded.goals,
                  assists = excluded.assists,
                  shots = excluded.shots,
                  shots_on_target = excluded.shots_on_target,
                  key_passes = excluded.key_passes,
                  tackles = excluded.tackles,
                  interceptions = excluded.interceptions,
                  saves = excluded.saves,
                  yellow_cards = excluded.yellow_cards,
                  red_cards = excluded.red_cards,
                  captured_at = excluded.captured_at,
                  raw_payload = excluded.raw_payload
                """
            ),
            {
                "match_id": match["id"],
                "player_id": player_id,
                "team_id": team_id,
                "opponent_team_id": opponent_team_id,
                "started": started,
                "minutes": _integer(appearance.get("minutes")),
                "goals": _integer(appearance.get("goals")),
                "assists": _integer(appearance.get("assists")),
                "shots": _integer(appearance.get("shots")),
                "shots_on_target": _integer(appearance.get("shots_on_target")),
                "key_passes": _integer(appearance.get("key_passes")),
                "tackles": _integer(appearance.get("tackles")),
                "interceptions": _integer(appearance.get("interceptions")),
                "saves": _integer(appearance.get("saves")),
                "yellow_cards": _integer(appearance.get("yellow_cards")),
                "red_cards": _integer(appearance.get("red_cards")),
                "source_player_key": (
                    f"api_football:{player['player']['provider_id']}"
                ),
                "raw": json.dumps(
                    {
                        "fixture_id": fixture["provider_fixture_id"],
                        "player": player.get("raw", player),
                    }
                ),
            },
        )
