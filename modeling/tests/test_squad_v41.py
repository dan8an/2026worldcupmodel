import json
import logging
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from modeling.src.data import load_teams
from scripts.build_squad_strength import (
    build_coverage_report,
    calculate_squad_strength,
)
from scripts.data_ingestion.providers import ApiFootballProvider, RequestLimitError
from scripts.update_squad_availability import (
    DEFAULT_MAX_FIXTURES,
    DEFAULT_MAX_REQUESTS,
    DEFAULT_MAX_TEAMS,
    SquadAvailabilityRepository,
    build_canonical_team_mappings,
    collect,
    diagnose_provider_fixtures,
    main as update_availability_main,
    parse_args as parse_availability_args,
    select_research_targets,
)
from scripts.validate_squad_v41 import SquadValidationRow, build_report

ROOT = Path(__file__).resolve().parents[2]


class SquadProviderParsingTests(unittest.TestCase):
    def test_api_requests_enforce_timeout_and_request_limit(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"response":[]}'

        calls = []

        def opener(*args, **kwargs):
            calls.append(kwargs)
            return Response()

        provider = ApiFootballProvider(
            "test-key",
            request_delay_seconds=0,
            request_timeout_seconds=4.5,
            max_requests=1,
            opener=opener,
        )

        provider.get_squad(10)
        with self.assertRaisesRegex(RequestLimitError, "request limit reached"):
            provider.get_squad(11)

        self.assertEqual(provider.request_count, 1)
        self.assertEqual(calls[0]["timeout"], 4.5)

    def test_injuries_parse_suspensions_and_injuries(self):
        provider = ApiFootballProvider("test-key")
        provider._request = lambda path, **params: [
            {
                "fixture": {"id": 100},
                "team": {"id": 10, "name": "Example"},
                "player": {
                    "id": 1,
                    "name": "Suspended Player",
                    "type": "Missing Fixture",
                    "reason": "Red Card Suspension",
                },
            },
            {
                "fixture": {"id": 100},
                "team": {"id": 10, "name": "Example"},
                "player": {
                    "id": 2,
                    "name": "Injured Player",
                    "type": "Knee Injury",
                    "reason": "Knee",
                },
            },
        ]

        rows = provider.get_injuries(fixture_id=100)

        self.assertEqual([row["status"] for row in rows], ["suspended", "injured"])
        self.assertEqual(rows[0]["fixture_id"], 100)
        self.assertIsNone(rows[1]["expected_return"])

    def test_squad_and_player_statistics_preserve_rating_and_minutes(self):
        provider = ApiFootballProvider("test-key")

        def response(path, **params):
            if path == "/players/squads":
                return [
                    {
                        "team": {"id": 10, "name": "Example"},
                        "players": [
                            {
                                "id": 1,
                                "name": "Player",
                                "position": "Attacker",
                                "age": 25,
                                "number": 9,
                            }
                        ],
                    }
                ]
            return [
                {
                    "player": {"id": 1, "name": "Player"},
                    "statistics": [
                        {
                            "team": {"id": 10, "name": "Example"},
                            "games": {
                                "appearences": 8,
                                "lineups": 7,
                                "minutes": 630,
                                "position": "Attacker",
                                "rating": "7.25",
                            },
                            "goals": {"total": 4, "assists": 2},
                            "shots": {"total": 20, "on": 10},
                        }
                    ],
                }
            ]

        provider._request = response
        squad = provider.get_squad(10)
        stats = provider.get_player_statistics(season=2026, team_id=10)

        self.assertEqual(squad[0]["position"], "Attacker")
        self.assertEqual(stats[0]["minutes"], 630)
        self.assertEqual(stats[0]["rating"], "7.25")

    def test_fixture_schedule_preserves_upcoming_status_and_team_names(self):
        provider = ApiFootballProvider("test-key")
        provider._request = lambda path, **params: [
            {
                "fixture": {
                    "id": 9001,
                    "date": "2026-06-12T19:00:00+00:00",
                    "status": {"short": "NS"},
                },
                "league": {
                    "id": 1,
                    "name": "World Cup",
                    "season": 2026,
                    "round": "Group Stage",
                },
                "teams": {
                    "home": {"id": 26, "name": "Argentina"},
                    "away": {"id": 2, "name": "France"},
                },
            }
        ]

        fixtures = provider.get_fixtures(league_id=1, season=2026)

        self.assertEqual(fixtures[0]["provider_fixture_id"], 9001)
        self.assertEqual(fixtures[0]["status"], "NS")
        self.assertEqual(fixtures[0]["home_team"]["name"], "Argentina")


class SquadStrengthTests(unittest.TestCase):
    def test_unavailable_high_rating_player_reduces_available_strength(self):
        players = [
            {
                "player_id": index,
                "player_name": f"Player {index}",
                "overall_rating": 80 - index,
                "minutes_played": 900,
                "status": "injured" if index == 0 else "available",
                "in_lineup": 1 <= index <= 11,
            }
            for index in range(18)
        ]

        result = calculate_squad_strength(players)

        self.assertEqual(result["player_count"], 18)
        self.assertEqual(result["available_player_count"], 17)
        self.assertGreater(result["unavailable_player_penalty"], 0)
        self.assertLess(
            result["available_squad_strength"],
            sum(80 - index for index in range(11)) / 11,
        )
        self.assertEqual(result["rating_source"], "player_ratings")
        self.assertGreater(result["coverage_level"], 0.0)

    def test_unrated_squad_uses_depth_only_without_inventing_quality(self):
        positions = (
            ["Goalkeeper"] * 3
            + ["Defender"] * 8
            + ["Midfielder"] * 9
            + ["Attacker"] * 6
        )
        players = [
            {
                "provider_player_id": index,
                "player_name": f"Player {index}",
                "position": position,
                "status": "injured" if index == 25 else "available",
            }
            for index, position in enumerate(positions)
        ]

        result = calculate_squad_strength(players)

        self.assertEqual(result["rating_source"], "squad_depth_only")
        self.assertEqual(result["squad_size"], 26)
        self.assertEqual(result["available_players"], 25)
        self.assertEqual(result["unavailable_players"], 1)
        self.assertEqual(result["known_position_counts"], 26)
        self.assertEqual(result["goalkeeper_count"], 3)
        self.assertEqual(result["defender_count"], 8)
        self.assertEqual(result["midfielder_count"], 9)
        self.assertEqual(result["attacker_count"], 6)
        self.assertEqual(result["squad_depth_score"], 100.0)
        self.assertAlmostEqual(result["availability_score"], 96.1538)
        self.assertEqual(result["data_completeness_score"], 100.0)
        self.assertIsNone(result["projected_lineup_strength"])
        self.assertTrue(
            all(
                player["strength"] is None
                for player in result["components"]["players"]
            )
        )

    def test_coverage_report_summarizes_team_feature_availability(self):
        ratings = [
            {
                "team_id": "mex-id",
                "team_code": "MEX",
                "provider_fixture_id": 100,
                "rating_source": "squad_depth_only",
                "squad_size": 26,
                "available_players": 26,
                "unavailable_players": 0,
                "known_position_counts": 26,
                "goalkeeper_count": 3,
                "defender_count": 6,
                "midfielder_count": 12,
                "attacker_count": 5,
                "squad_depth_score": 100.0,
                "availability_score": 100.0,
                "data_completeness_score": 100.0,
                "squad_strength": 100.0,
            },
            {
                "team_id": "rsa-id",
                "team_code": "RSA",
                "provider_fixture_id": 100,
                "rating_source": "squad_depth_only",
                "squad_size": 26,
            },
        ]
        players = [
            {
                "team_code": "MEX",
                "availability_source": "api_football_inferred_available",
                "status": "available",
                "in_lineup": False,
            },
            {
                "team_code": "RSA",
                "availability_source": "api_football",
                "status": "injured",
                "in_lineup": True,
            },
        ]

        report = build_coverage_report(ratings, players)
        serialized = json.loads(json.dumps(report))

        self.assertEqual(serialized["status"], "research_only")
        self.assertEqual(serialized["teams_with_squad_ratings"], 2)
        self.assertEqual(serialized["teams_with_availability_data"], 2)
        self.assertEqual(serialized["teams_with_injury_data"], 2)
        self.assertEqual(serialized["teams_with_unavailable_players"], 1)
        self.assertEqual(serialized["teams_with_lineup_data"], 1)
        self.assertEqual(
            serialized["rating_source_counts"], {"squad_depth_only": 2}
        )
        self.assertFalse(serialized["chronological_validation_usable"])


class SquadValidationTests(unittest.TestCase):
    def test_validation_output_contains_metrics_coverage_and_gate(self):
        start = date(2024, 1, 1)
        rows = []
        for index in range(80):
            signal = 0.8 if index % 2 == 0 else -0.8
            rows.append(
                SquadValidationRow(
                    played_on=start + timedelta(days=index),
                    outcome=0 if signal > 0 else 2,
                    v4=(0.4, 0.2, 0.4),
                    squad_strength_signal=signal,
                    unavailable_penalty_signal=signal,
                    projected_lineup_signal=signal,
                    match_id=index,
                    injury_data=True,
                    squad_data=True,
                    lineup_data=True,
                )
            )

        report = build_report(
            rows,
            {
                "teams_with_squad_strength": 48,
                "teams_with_injury_data": 40,
                "teams_with_lineup_data": 32,
            },
        )

        self.assertEqual(report["status"], "chronological_holdout_complete")
        self.assertFalse(report["production_predictions_changed"])
        self.assertEqual(
            report["coverage"]["matches_with_injury_data"]["matches"], 80
        )
        for name, result in report["ablations"].items():
            self.assertEqual(result["status"], "evaluated", name)
            self.assertIn("brier_score", result["validation_metrics"])
            self.assertIn("log_loss", result["validation_metrics"])
            self.assertIn("calibration", result)
        self.assertTrue(report["promotion"]["recommend_promotion"])
        self.assertTrue(
            report["squad_feature_usability"]["usable_for_validation"]
        )
        self.assertEqual(
            report["squad_feature_usability"]["teams_with_squad_strength"],
            48,
        )

    def test_empty_validation_is_valid_insufficient_data_report(self):
        report = build_report([])
        serialized = json.loads(json.dumps(report))

        self.assertEqual(serialized["status"], "insufficient_data")
        self.assertFalse(serialized["promotion"]["recommend_promotion"])
        self.assertFalse(
            serialized["squad_feature_usability"]["usable_for_validation"]
        )


class SquadAvailabilityCommandTests(unittest.TestCase):
    def test_cli_defaults_are_small_and_bounded(self):
        with patch("sys.argv", ["update_squad_availability.py"]):
            args = parse_availability_args()

        self.assertEqual(args.max_fixtures, DEFAULT_MAX_FIXTURES)
        self.assertEqual(args.max_teams, DEFAULT_MAX_TEAMS)
        self.assertEqual(args.max_requests, DEFAULT_MAX_REQUESTS)
        self.assertLessEqual(args.max_fixtures, 2)
        self.assertLessEqual(args.max_teams, 4)
        self.assertLessEqual(args.max_requests, 12)

    def test_collect_stops_cleanly_at_request_budget(self):
        class Provider:
            request_count = 0

            def _call(self):
                if self.request_count >= 2:
                    raise RequestLimitError(2)
                self.request_count += 1

            def get_squad(self, team_id):
                self._call()
                return [{"team_id": team_id}]

            def get_injuries(self, **kwargs):
                self._call()
                return []

            def get_lineups(self, fixture_id):
                self._call()
                return []

            def get_player_statistics(self, **kwargs):
                self._call()
                return []

        teams = [
            {"id": "a", "name": "A", "api_football_team_id": 1},
            {"id": "b", "name": "B", "api_football_team_id": 2},
        ]
        fixtures = [{"id": "f", "api_football_fixture_id": 100}]

        result = collect(
            Provider(),
            teams,
            fixtures,
            season=2026,
            league_id=1,
            logger=logging.getLogger("test_squad_collection"),
        )

        self.assertTrue(result.request_limit_reached)
        self.assertEqual(set(result.squads), {1, 2})
        self.assertEqual(result.injuries, {})

    def test_raw_provider_fixture_is_selected_without_database_match(self):
        fixtures = [
            {
                "provider_fixture_id": 100,
                "date": "2026-06-12T19:00:00+00:00",
                "status": "NS",
                "home_team": {"provider_id": 16, "name": "Mexico"},
                "away_team": {"provider_id": 1531, "name": "South Africa"},
            }
        ]

        teams, selected = select_research_targets(
            fixtures,
            [],
            [],
            now=datetime(2026, 6, 11, tzinfo=timezone.utc),
            max_fixtures=1,
            max_teams=2,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["target_source"], "provider_schedule")
        self.assertIsNone(selected[0]["fixture_id"])
        self.assertEqual(selected[0]["provider_fixture_id"], 100)
        self.assertEqual(selected[0]["canonical_home_team_code"], "MEX")
        self.assertEqual(selected[0]["canonical_away_team_code"], "RSA")
        self.assertEqual({team["code"] for team in teams}, {"MEX", "RSA"})

    def test_new_zealand_uses_canonical_mapping_without_database_team(self):
        fixtures = [
            {
                "provider_fixture_id": 101,
                "date": "2026-06-12T19:00:00+00:00",
                "status": "NS",
                "home_team": {"provider_id": 4673, "name": "New Zealand"},
                "away_team": {"provider_id": 32, "name": "Egypt"},
            }
        ]
        database_teams = [
            {
                "id": "db-egypt",
                "name": "Egypt",
                "api_football_team_id": 32,
            }
        ]

        teams, selected = select_research_targets(
            fixtures,
            database_teams,
            [],
            now=datetime(2026, 6, 11, tzinfo=timezone.utc),
            max_fixtures=1,
            max_teams=2,
        )

        new_zealand = next(team for team in teams if team["code"] == "NZL")
        self.assertIsNone(new_zealand["id"])
        self.assertEqual(new_zealand["api_football_team_id"], 4673)
        self.assertEqual(selected[0]["canonical_home_team_code"], "NZL")

    def test_database_fixture_can_be_selected_without_provider_schedule_row(self):
        database_teams = [
            {
                "id": "db-mex",
                "name": "Mexico",
                "api_football_team_id": 16,
            },
            {
                "id": "db-rsa",
                "name": "South Africa",
                "api_football_team_id": 1531,
            },
        ]
        database_matches = [
            {
                "id": "db-fixture",
                "api_football_fixture_id": 100,
                "match_date": "2026-06-12T19:00:00+00:00",
                "home_team_id": "db-mex",
                "away_team_id": "db-rsa",
                "completed": False,
            }
        ]

        teams, selected = select_research_targets(
            [],
            database_teams,
            database_matches,
            now=datetime(2026, 6, 11, tzinfo=timezone.utc),
            max_fixtures=1,
            max_teams=2,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["target_source"], "database_match")
        self.assertEqual(selected[0]["fixture_id"], "db-fixture")
        self.assertEqual({team["code"] for team in teams}, {"MEX", "RSA"})

    def test_all_48_provider_teams_resolve_to_canonical_codes(self):
        teams = load_teams()
        fixtures = []
        for index in range(0, len(teams), 2):
            fixtures.append(
                {
                    "provider_fixture_id": 1000 + index,
                    "date": "2026-06-12T19:00:00+00:00",
                    "status": "NS",
                    "home_team": {
                        "provider_id": index + 1,
                        "name": teams[index].name,
                    },
                    "away_team": {
                        "provider_id": index + 2,
                        "name": teams[index + 1].name,
                    },
                }
            )

        mappings = build_canonical_team_mappings(fixtures, [])

        self.assertEqual(len(mappings), 48)
        self.assertEqual(
            {mapping["code"] for mapping in mappings.values()},
            {team.id for team in teams},
        )

    def test_provider_only_fixture_persists_without_matches_row(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    create table players (
                      id integer primary key autoincrement,
                      team_id text,
                      provider_key text unique,
                      display_name text,
                      primary_position text,
                      updated_at text
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    create table player_availability_reports (
                      id integer primary key autoincrement,
                      team_id text,
                      team_code text,
                      player_id integer,
                      provider_player_id integer,
                      player_name text,
                      position text,
                      status text,
                      reason text,
                      fixture_id text,
                      provider_fixture_id integer,
                      canonical_home_team_code text,
                      canonical_away_team_code text,
                      expected_return text,
                      source text,
                      collected_at text,
                      raw_payload json
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    create table projected_lineups (
                      id integer primary key autoincrement,
                      team_id text,
                      team_code text,
                      fixture_id text,
                      provider_fixture_id integer,
                      canonical_home_team_code text,
                      canonical_away_team_code text,
                      player_id integer,
                      provider_player_id integer,
                      player_name text,
                      position text,
                      lineup_status text,
                      formation text,
                      projected_minutes real,
                      player_strength real,
                      source text,
                      collected_at text,
                      raw_payload json
                    )
                    """
                )
            )

        repository = SquadAvailabilityRepository(engine)
        fixture = {
            "id": None,
            "api_football_fixture_id": 101,
            "canonical_home_team_code": "NZL",
            "canonical_away_team_code": "EGY",
        }
        new_zealand = {
            "id": None,
            "code": "NZL",
            "name": "New Zealand",
            "api_football_team_id": 4673,
        }
        counts = repository.store(
            [new_zealand],
            [fixture],
            {
                4673: [
                    {
                        "player": {"provider_id": 1, "name": "NZ Player"},
                        "position": "Midfielder",
                        "raw": {},
                    }
                ]
            },
            {},
            {101: []},
            {},
        )
        with engine.connect() as connection:
            row = connection.execute(
                text("select * from player_availability_reports")
            ).mappings().one()

        self.assertEqual(counts["availability_reports"], 1)
        self.assertIsNone(row["fixture_id"])
        self.assertIsNone(row["team_id"])
        self.assertEqual(row["team_code"], "NZL")
        self.assertEqual(row["provider_fixture_id"], 101)
        self.assertEqual(row["canonical_home_team_code"], "NZL")
        self.assertEqual(row["canonical_away_team_code"], "EGY")

    def test_diagnosis_categorizes_fixture_exclusions_and_mappings(self):
        now = datetime(2026, 6, 11, tzinfo=timezone.utc)
        argentina = {
            "id": "db-arg",
            "name": "Argentina",
            "api_football_team_id": 26,
        }
        france = {
            "id": "db-fra",
            "name": "France",
            "api_football_team_id": 999,
        }
        fixtures = [
            {
                "provider_fixture_id": 1,
                "date": "2026-06-12T19:00:00+00:00",
                "status": "NS",
                "home_team": {"provider_id": 26, "name": "Argentina"},
                "away_team": {"provider_id": 2, "name": "France"},
            },
            {
                "provider_fixture_id": 1,
                "date": "2026-06-12T19:00:00+00:00",
                "status": "NS",
                "home_team": {"provider_id": 26, "name": "Argentina"},
                "away_team": {"provider_id": 2, "name": "France"},
            },
            {
                "provider_fixture_id": 2,
                "date": "2026-06-10T19:00:00+00:00",
                "status": "FT",
                "home_team": {"provider_id": 26, "name": "Argentina"},
                "away_team": {"provider_id": 500, "name": "Unknown XI"},
            },
        ]

        report = diagnose_provider_fixtures(
            fixtures,
            [argentina, france],
            [],
            now=now,
            logger=logging.getLogger("test_squad_diagnosis"),
        )

        self.assertEqual(report["provider_fixture_count"], 3)
        self.assertEqual(report["exclusion_counts"]["duplicate_filtering"], 1)
        self.assertEqual(report["exclusion_counts"]["date_filtering"], 1)
        self.assertEqual(report["exclusion_counts"]["fixture_status"], 1)
        self.assertGreater(
            report["exclusion_counts"]["missing_team_mappings"], 0
        )
        self.assertGreater(
            report["exclusion_counts"]["provider_backed_team_requirements"], 0
        )
        self.assertEqual(report["exclusion_counts"]["database_fixture_missing"], 3)
        france_mapping = next(
            row
            for row in report["canonical_mapping_audit"]
            if row["provider_name"] == "France"
        )
        self.assertEqual(france_mapping["canonical_team_id"], "FRA")
        self.assertFalse(france_mapping["provider_backed"])

    def test_main_exits_cleanly_when_no_relevant_fixtures_exist(self):
        class Engine:
            def dispose(self):
                pass

        class Repository:
            def __init__(self, engine):
                pass

            def assert_schema(self):
                pass

            def load_targets(
                self,
                provider_fixtures,
                max_fixtures,
                max_teams,
            ):
                return [], []

            def load_diagnostic_snapshot(self):
                return [], []

        class Provider:
            request_count = 0

            def get_fixtures(self, **kwargs):
                self.request_count += 1
                return []

        with (
            patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}, clear=True),
            patch("scripts.update_squad_availability.load_dotenv"),
            patch(
                "scripts.update_squad_availability.create_database_engine",
                return_value=Engine(),
            ),
            patch(
                "scripts.update_squad_availability.create_sports_provider",
                return_value=Provider(),
            ),
            patch(
                "scripts.update_squad_availability.SquadAvailabilityRepository",
                Repository,
            ),
            patch("sys.argv", ["update_squad_availability.py"]),
            patch(
                "scripts.update_squad_availability.DIAGNOSIS_PATH",
                Path("/tmp/test_squad_availability_diagnosis.json"),
            ),
            self.assertLogs("update_squad_availability", level="INFO") as logs,
        ):
            status = update_availability_main()

        self.assertEqual(status, 0)
        output = "\n".join(logs.output)
        self.assertIn("START", output)
        self.assertIn("provider_fixtures_seen=0", output)
        self.assertIn("selected_fixtures=0", output)
        self.assertIn("rows_written=0", output)

    def test_main_exits_cleanly_when_request_limit_is_reached(self):
        future_kickoff = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()

        class Engine:
            def dispose(self):
                pass

        class Provider:
            request_count = 0
            max_requests = 2

            def _call(self):
                if self.request_count >= self.max_requests:
                    raise RequestLimitError(self.max_requests)
                self.request_count += 1

            def get_fixtures(self, **kwargs):
                self._call()
                return [
                    {
                        "provider_fixture_id": 100,
                        "date": future_kickoff,
                        "status": "NS",
                        "home_team": {
                            "provider_id": 16,
                            "name": "Mexico",
                        },
                        "away_team": {
                            "provider_id": 1531,
                            "name": "South Africa",
                        },
                    }
                ]

            def get_squad(self, team_id):
                self._call()
                return []

        class Repository:
            def __init__(self, engine):
                pass

            def assert_schema(self):
                pass

            def load_diagnostic_snapshot(self):
                return [], []

            def store(self, *args):
                return {
                    "players": 0,
                    "availability_reports": 0,
                    "lineup_players": 0,
                }

        provider = Provider()
        with (
            patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}, clear=True),
            patch("scripts.update_squad_availability.load_dotenv"),
            patch(
                "scripts.update_squad_availability.create_database_engine",
                return_value=Engine(),
            ),
            patch(
                "scripts.update_squad_availability.create_sports_provider",
                return_value=provider,
            ),
            patch(
                "scripts.update_squad_availability.SquadAvailabilityRepository",
                Repository,
            ),
            patch(
                "scripts.update_squad_availability.DIAGNOSIS_PATH",
                Path("/tmp/test_squad_limited_diagnosis.json"),
            ),
            patch(
                "sys.argv",
                [
                    "update_squad_availability.py",
                    "--max-requests",
                    "2",
                ],
            ),
            self.assertLogs("update_squad_availability", level="INFO") as logs,
        ):
            status = update_availability_main()

        self.assertEqual(status, 0)
        output = "\n".join(logs.output)
        self.assertIn("provider_fixtures_seen=1", output)
        self.assertIn("selected_fixtures=1", output)
        self.assertIn("selected_teams=2", output)
        self.assertIn("requests_made=2", output)
        self.assertIn("request_limit_reached=True", output)


class SquadSchemaAndIsolationTests(unittest.TestCase):
    def test_migration_contains_research_tables_and_required_fields(self):
        migration = (
            ROOT
            / "supabase"
            / "migrations"
            / "202606110004_squad_v41_research.sql"
        ).read_text()
        for table in (
            "player_availability_reports",
            "projected_lineups",
            "squad_strength_ratings",
        ):
            self.assertIn(f"create table if not exists public.{table}", migration)
        for field in (
            "team_id",
            "team_code",
            "provider_player_id",
            "player_name",
            "position",
            "expected_return",
            "source",
            "collected_at",
            "canonical_home_team_code",
            "canonical_away_team_code",
        ):
            self.assertIn(field, migration)
        self.assertIn(
            "status in ('available', 'injured', 'suspended', 'unknown')",
            migration,
        )
        provider_fixture_migration = (
            ROOT
            / "supabase"
            / "migrations"
            / "202606110005_squad_v41_provider_fixtures.sql"
        ).read_text()
        self.assertIn(
            "alter table public.player_availability_reports",
            provider_fixture_migration,
        )
        self.assertIn(
            "add column if not exists canonical_home_team_code",
            provider_fixture_migration,
        )
        strength_feature_migration = (
            ROOT
            / "supabase"
            / "migrations"
            / "202606110006_squad_v41_strength_features.sql"
        ).read_text()
        for field in (
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
        ):
            self.assertIn(f"add column if not exists {field}", strength_feature_migration)

    def test_production_prediction_and_simulation_do_not_use_squad_v41(self):
        prediction_source = (ROOT / "scripts" / "generate_predictions.py").read_text()
        simulation_source = (ROOT / "scripts" / "run_simulations.py").read_text()

        self.assertIn('MODEL_VERSION = "elo-context-v4.2.1"', prediction_source)
        for source in (prediction_source, simulation_source):
            self.assertNotIn("squad_strength_ratings", source)
            self.assertNotIn("player_availability_reports", source)
            self.assertNotIn("validate_squad_v41", source)

    def test_squad_availability_is_not_in_production_cron_instructions(self):
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn(
            "python scripts/update_squad_availability.py",
            readme,
        )


if __name__ == "__main__":
    unittest.main()
