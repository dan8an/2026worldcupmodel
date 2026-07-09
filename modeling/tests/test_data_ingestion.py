import unittest
from argparse import Namespace
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import Mock, patch

from sqlalchemy.pool import NullPool

from scripts.data_ingestion.providers import (
    ApiFootballProvider,
    RateLimitError,
    SampleSportsProvider,
    create_sports_provider,
)
from scripts.data_ingestion.repository import DataIngestionRepository
from scripts.database import (
    configure_database_timeouts,
    create_database_engine,
    sqlalchemy_database_url,
)
from scripts.update_data import main, merge_provider_fixtures, parse_args


class DataIngestionTests(unittest.TestCase):
    def test_missing_key_uses_sample_provider(self):
        provider = create_sports_provider({"SPORTS_PROVIDER": "api_football"})
        self.assertIsInstance(provider, SampleSportsProvider)

    def test_sample_provider_implements_full_contract(self):
        provider = SampleSportsProvider()
        matches = provider.get_completed_matches("2026-06-09")
        fixture_id = matches[0]["provider_fixture_id"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(len(provider.get_fixture_statistics(fixture_id)), 2)
        self.assertEqual(len(provider.get_fixture_players(fixture_id)), 2)
        self.assertEqual(len(provider.get_lineups(fixture_id)), 2)

    def test_force_sample_ignores_api_key_and_requested_date(self):
        provider = create_sports_provider(
            {
                "SPORTS_PROVIDER": "api_football",
                "API_FOOTBALL_KEY": "real-key-must-not-be-used",
            },
            force_sample=True,
        )

        matches = provider.get_completed_matches("1999-01-01")

        self.assertIsInstance(provider, SampleSportsProvider)
        self.assertEqual(len(matches), 1)

    def test_production_requires_api_key_unless_sample_is_explicit(self):
        with self.assertRaisesRegex(ValueError, "required in production"):
            create_sports_provider(
                {
                    "RENDER": "true",
                    "SPORTS_PROVIDER": "api_football",
                }
            )

        provider = create_sports_provider(
            {
                "RENDER": "true",
                "SPORTS_PROVIDER": "api_football",
                "INGESTION_USE_SAMPLE": "true",
            }
        )
        self.assertIsInstance(provider, SampleSportsProvider)

    def test_api_provider_normalizes_completed_matches(self):
        provider = ApiFootballProvider("test-key")
        captured = {}

        def response(path, **params):
            captured.update(params)
            return [
                {
                    "fixture": {
                        "id": 123,
                        "date": "2026-06-09T19:00:00+00:00",
                        "status": {"short": "FT"},
                    },
                    "league": {
                        "id": 1,
                        "name": "World Cup",
                        "season": 2026,
                        "round": "Round 1",
                    },
                    "teams": {
                        "home": {"id": 26, "name": "Argentina"},
                        "away": {"id": 2, "name": "France"},
                    },
                    "goals": {"home": 2, "away": 1},
                }
            ]

        provider._request = response

        matches = provider.get_completed_matches("2026-06-09")

        self.assertEqual(matches[0]["provider_fixture_id"], 123)
        self.assertEqual(matches[0]["home_team"]["name"], "Argentina")
        self.assertEqual(captured["league"], 1)
        self.assertEqual(captured["season"], 2026)

    def test_api_provider_normalizes_all_final_statuses(self):
        provider = ApiFootballProvider("test-key")
        provider._request = lambda path, **params: [
            {
                "fixture": {
                    "id": index,
                    "date": "2026-06-13T01:00:00+00:00",
                    "status": {"short": status},
                },
                "league": {"id": 1},
                "teams": {
                    "home": {"id": 1, "name": "USA"},
                    "away": {"id": 2, "name": "Paraguay"},
                },
                "goals": {"home": 1, "away": 0},
            }
            for index, status in enumerate(("FT", "AET", "PEN", "NS"), start=1)
        ]

        matches = provider.get_completed_matches_range(
            "2026-06-12",
            "2026-06-13",
            1,
            2026,
        )

        self.assertEqual(
            [match["status"] for match in matches],
            ["FT", "AET", "PEN"],
        )

    def test_api_provider_loads_completed_matches_for_range(self):
        provider = ApiFootballProvider("test-key")
        captured = {}

        def response(path, **params):
            captured["path"] = path
            captured.update(params)
            return [
                {
                    "fixture": {
                        "id": 123,
                        "date": "2022-11-20T16:00:00+00:00",
                        "status": {"short": "FT"},
                    },
                    "league": {"id": 1, "name": "World Cup", "season": 2022},
                    "teams": {
                        "home": {"id": 1, "name": "Qatar"},
                        "away": {"id": 2, "name": "Ecuador"},
                    },
                    "goals": {"home": 0, "away": 2},
                },
                {
                    "fixture": {
                        "id": 124,
                        "date": "2022-11-21T13:00:00+00:00",
                        "status": {"short": "NS"},
                    },
                    "league": {"id": 1},
                },
            ]

        provider._request = response

        matches = provider.get_completed_matches_range(
            "2022-11-20", "2022-12-18", 1, 2022
        )

        self.assertEqual([match["provider_fixture_id"] for match in matches], [123])
        self.assertEqual(
            captured,
            {
                "path": "/fixtures",
                "league": 1,
                "season": 2022,
                "from": "2022-11-20",
                "to": "2022-12-18",
            },
        )

    def test_api_provider_fixture_listing_preserves_final_scores_and_penalties(self):
        provider = ApiFootballProvider("test-key")
        provider._request = lambda path, **params: [
            {
                "fixture": {
                    "id": 90101,
                    "date": "2026-07-09T20:00:00+00:00",
                    "status": {"short": "PEN"},
                },
                "league": {
                    "id": 1,
                    "name": "World Cup",
                    "season": 2026,
                    "round": "Quarter-finals",
                },
                "teams": {
                    "home": {"id": 1, "name": "Mexico"},
                    "away": {"id": 2, "name": "South Africa"},
                },
                "goals": {"home": 1, "away": 1},
                "score": {"penalty": {"home": 4, "away": 3}},
            }
        ]

        [fixture] = provider.get_fixtures(league_id=1, season=2026)

        self.assertEqual(fixture["provider_fixture_id"], 90101)
        self.assertEqual(fixture["status"], "PEN")
        self.assertEqual(fixture["home_score"], 1)
        self.assertEqual(fixture["away_score"], 1)
        self.assertEqual(fixture["home_penalty_score"], 4)
        self.assertEqual(fixture["away_penalty_score"], 3)
        self.assertEqual(fixture["raw"]["score"]["penalty"]["home"], 4)

    def test_api_provider_normalizes_competitions_and_seasons(self):
        provider = ApiFootballProvider("test-key")
        provider._request = lambda path: [
            {
                "league": {"id": 4, "name": "Euro Championship"},
                "country": {"name": "World"},
                "seasons": [{"year": 2024}, {"year": 2020}, {"year": 2024}],
            }
        ]

        competitions = provider.get_competitions()

        self.assertEqual(
            competitions,
            [
                {
                    "league_id": 4,
                    "name": "Euro Championship",
                    "country": "World",
                    "seasons": [2020, 2024],
                }
            ],
        )

    def test_provider_factory_reads_filter_and_delay_settings(self):
        provider = create_sports_provider(
            {
                "SPORTS_PROVIDER": "api_football",
                "API_FOOTBALL_KEY": "test-key",
                "API_FOOTBALL_LEAGUE_ID": "4",
                "API_FOOTBALL_SEASON": "2024",
                "API_FOOTBALL_REQUEST_DELAY_SECONDS": "2.5",
            }
        )
        self.assertEqual(provider.league_id, 4)
        self.assertEqual(provider.season, 2024)
        self.assertEqual(provider.request_delay_seconds, 2.5)

    def test_http_429_raises_specific_rate_limit_error(self):
        headers = Message()
        headers["Retry-After"] = "60"

        def rate_limited(*args, **kwargs):
            raise HTTPError(
                "https://example.test",
                429,
                "Too Many Requests",
                headers,
                BytesIO(),
            )

        provider = ApiFootballProvider(
            "test-key",
            opener=rate_limited,
            sleep=lambda seconds: None,
        )
        with self.assertRaisesRegex(RateLimitError, "retry after 60 seconds"):
            provider.get_fixture_statistics(123)

    def test_rate_limiter_sleeps_between_requests(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"response":[]}'

        times = iter([10.0, 10.25, 11.0])
        sleeps = []
        provider = ApiFootballProvider(
            "test-key",
            request_delay_seconds=1.0,
            opener=lambda *args, **kwargs: Response(),
            sleep=sleeps.append,
            monotonic=lambda: next(times),
        )
        provider.get_fixture_statistics(1)
        provider.get_fixture_players(1)
        self.assertEqual(sleeps, [0.75])

    def test_max_fixtures_defaults_to_five(self):
        import sys

        original = sys.argv
        try:
            sys.argv = ["update_data.py"]
            self.assertEqual(parse_args().max_fixtures, 5)
            self.assertFalse(parse_args().sample)
            self.assertLess(parse_args().date, parse_args().date_to)
        finally:
            sys.argv = original

    def test_sample_cli_flag_is_parsed(self):
        import sys

        original = sys.argv
        try:
            sys.argv = ["update_data.py", "--sample"]
            self.assertTrue(parse_args().sample)
        finally:
            sys.argv = original

    def test_database_url_uses_existing_psycopg_driver(self):
        self.assertEqual(
            sqlalchemy_database_url("postgresql://host/database"),
            "postgresql+psycopg://host/database",
        )

    def test_postgres_engine_disables_prepared_statement_cache(self):
        with (
            patch("scripts.database.create_engine") as create_engine,
            patch("scripts.database.event.listen") as listen,
        ):
            create_database_engine("postgresql://host/database")

        create_engine.assert_called_once_with(
            "postgresql+psycopg://host/database",
            pool_pre_ping=True,
            connect_args={"prepare_threshold": None},
        )
        self.assertEqual(listen.call_args.args[1], "connect")

    def test_postgres_engine_accepts_connect_timeout(self):
        with (
            patch("scripts.database.create_engine") as create_engine,
            patch("scripts.database.event.listen"),
        ):
            create_database_engine(
                "postgresql://host/database",
                connect_timeout_seconds=15,
            )

        create_engine.assert_called_once_with(
            "postgresql+psycopg://host/database",
            pool_pre_ping=True,
            connect_args={
                "prepare_threshold": None,
                "connect_timeout": 15,
            },
        )

    def test_postgres_transaction_timeouts_are_registered(self):
        engine = Mock()
        engine.dialect.name = "postgresql"
        with patch("scripts.database.event.listen") as listen:
            configure_database_timeouts(
                engine,
                statement_timeout_seconds=120,
                lock_timeout_seconds=10,
            )

        callback = listen.call_args.args[2]
        connection = Mock()
        callback(connection)
        self.assertEqual(
            [call.args[0] for call in connection.exec_driver_sql.call_args_list],
            [
                "SET LOCAL statement_timeout = 120000",
                "SET LOCAL lock_timeout = 10000",
            ],
        )

    def test_supabase_pooler_uses_null_pool(self):
        with (
            patch("scripts.database.create_engine") as create_engine,
            patch("scripts.database.event.listen"),
        ):
            create_database_engine(
                "postgresql://user:password@project.pooler.supabase.com:6543/postgres"
            )

        create_engine.assert_called_once_with(
            "postgresql+psycopg://user:password@project.pooler.supabase.com:6543/postgres",
            pool_pre_ping=True,
            connect_args={"prepare_threshold": None},
            poolclass=NullPool,
        )

    def test_sqlite_engine_does_not_receive_psycopg_connect_args(self):
        with patch("scripts.database.create_engine") as create_engine:
            create_database_engine("sqlite:///test.sqlite3")

        create_engine.assert_called_once_with(
            "sqlite:///test.sqlite3",
            pool_pre_ping=True,
        )

    def test_main_exits_zero_when_no_completed_matches_exist(self):
        class Provider:
            name = "api_football"
            league_id = 1
            season = 2026

            def get_completed_matches_range(self, date_from, date_to, league_id, season):
                return []

        class Engine:
            def dispose(self):
                pass

        class Repository:
            def __init__(self, engine, logger):
                pass

            def assert_schema(self):
                pass

        with (
            patch(
                "scripts.update_data.load_environment",
                return_value={"DATABASE_URL": "postgresql://example/database"},
            ),
            patch("scripts.update_data.create_sports_provider", return_value=Provider()),
            patch("scripts.update_data.create_database_engine", return_value=Engine()),
            patch("scripts.update_data.DataIngestionRepository", Repository),
            patch("sys.argv", ["update_data.py"]),
        ):
            self.assertEqual(main(), 0)

    def test_main_exits_zero_when_fixture_discovery_is_rate_limited(self):
        class Provider:
            name = "api_football"
            league_id = 1
            season = 2026

            def get_completed_matches_range(self, date_from, date_to, league_id, season):
                raise RateLimitError("60")

        class Engine:
            def dispose(self):
                pass

        class Repository:
            def __init__(self, engine, logger):
                pass

            def assert_schema(self):
                pass

        with (
            patch(
                "scripts.update_data.load_environment",
                return_value={"DATABASE_URL": "postgresql://example/database"},
            ),
            patch("scripts.update_data.create_sports_provider", return_value=Provider()),
            patch("scripts.update_data.create_database_engine", return_value=Engine()),
            patch("scripts.update_data.DataIngestionRepository", Repository),
            patch("sys.argv", ["update_data.py"]),
        ):
            self.assertEqual(main(), 0)

    def test_default_cron_window_includes_late_night_fixture_on_today_utc(self):
        fixture = {
            "provider_fixture_id": 1489370,
            "date": "2026-06-13T01:00:00+00:00",
            "status": "FT",
            "home_team": {"provider_id": 2384, "name": "USA"},
            "away_team": {"provider_id": 2380, "name": "Paraguay"},
            "home_score": 4,
            "away_score": 1,
        }

        class Provider:
            name = "api_football"
            league_id = 1
            season = 2026

            def __init__(self):
                self.window = None

            def get_completed_matches_range(
                self,
                date_from,
                date_to,
                league_id,
                season,
            ):
                self.window = (date_from, date_to, league_id, season)
                return [fixture]

        class Engine:
            def dispose(self):
                pass

        class Repository:
            stored = []

            def __init__(self, engine, logger):
                pass

            def assert_schema(self):
                pass

            def upsert_provider_matches(self, matches):
                self.stored.extend(matches)

            def find_completed_matches_missing_stats(self, fixture_ids):
                return []

        provider = Provider()
        with (
            patch(
                "scripts.update_data.parse_args",
                return_value=Namespace(
                    date="2026-06-12",
                    date_to="2026-06-13",
                    max_fixtures=5,
                    sample=False,
                ),
            ),
            patch(
                "scripts.update_data.load_environment",
                return_value={"DATABASE_URL": "postgresql://example/database"},
            ),
            patch("scripts.update_data.create_sports_provider", return_value=provider),
            patch("scripts.update_data.create_database_engine", return_value=Engine()),
            patch("scripts.update_data.DataIngestionRepository", Repository),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(
            provider.window,
            ("2026-06-12", "2026-06-13", 1, 2026),
        )
        self.assertEqual(Repository.stored, [fixture])

    def test_update_data_does_not_wait_on_nonexistent_placeholder_fixtures(self):
        real_fixture = {
            "provider_fixture_id": 90073,
            "date": "2026-06-28T16:00:00+00:00",
            "competition": "FIFA World Cup",
            "round": "Round of 32",
            "home_team": {"provider_id": 1, "name": "Mexico"},
            "away_team": {"provider_id": 2, "name": "South Africa"},
            "home_score": 2,
            "away_score": 0,
        }

        class Provider:
            name = "api_football"
            league_id = 1
            season = 2026

            def get_completed_matches_range(self, *_args):
                return [real_fixture]

            def get_fixture_statistics(self, fixture_id):
                raise AssertionError(f"unexpected stats fetch for {fixture_id}")

            def get_fixture_players(self, fixture_id):
                raise AssertionError(f"unexpected player fetch for {fixture_id}")

            def get_lineups(self, fixture_id):
                raise AssertionError(f"unexpected lineup fetch for {fixture_id}")

        class Engine:
            def dispose(self):
                pass

        class Repository:
            stored = []
            missing_stats_requests = []

            def __init__(self, engine, logger):
                pass

            def assert_schema(self):
                pass

            def upsert_provider_matches(self, matches):
                self.stored.extend(matches)

            def find_completed_matches_missing_stats(self, fixture_ids):
                self.missing_stats_requests.append(list(fixture_ids))
                return []

        with (
            patch(
                "scripts.update_data.parse_args",
                return_value=Namespace(
                    date="2026-06-28",
                    date_to="2026-06-28",
                    max_fixtures=5,
                    sample=False,
                ),
            ),
            patch(
                "scripts.update_data.load_environment",
                return_value={"DATABASE_URL": "postgresql://example/database"},
            ),
            patch("scripts.update_data.create_sports_provider", return_value=Provider()),
            patch("scripts.update_data.create_database_engine", return_value=Engine()),
            patch("scripts.update_data.DataIngestionRepository", Repository),
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(Repository.stored, [real_fixture])
        self.assertEqual(Repository.missing_stats_requests, [[90073]])

    def test_completed_knockout_fixture_wins_merge_over_scheduled_listing(self):
        scheduled = {
            "provider_fixture_id": 90101,
            "date": "2026-07-09T20:00:00+00:00",
            "status": "NS",
            "round": "Quarter-finals",
            "home_team": {"provider_id": 1, "name": "Mexico"},
            "away_team": {"provider_id": 2, "name": "South Africa"},
        }
        completed = {
            **scheduled,
            "status": "FT",
            "home_score": 2,
            "away_score": 0,
        }

        [merged] = merge_provider_fixtures([completed], [scheduled])

        self.assertEqual(merged["status"], "FT")
        self.assertEqual(merged["home_score"], 2)
        self.assertEqual(merged["away_score"], 0)

    def test_completed_upsert_updates_existing_provider_fixture(self):
        connection = Mock()
        connection.execute.return_value.scalar_one_or_none.return_value = False
        DataIngestionRepository._upsert_match(
            connection,
            {
                "provider_fixture_id": 1489370,
                "date": "2026-06-13T01:00:00+00:00",
                "status": "FT",
                "round": "Group Stage - 1",
                "home_team": {"name": "USA"},
                "away_team": {"name": "Paraguay"},
                "home_score": 4,
                "away_score": 1,
                "raw": {},
            },
            "usa-id",
            "paraguay-id",
        )

        statement, parameters = connection.execute.call_args.args
        sql = str(statement)
        self.assertIn("on conflict (api_football_fixture_id)", sql)
        self.assertIn("do update set", sql)
        self.assertIn("completed = excluded.completed", sql)
        self.assertIs(parameters["completed"], True)
        self.assertEqual(parameters["status"], "FT")
        self.assertEqual(parameters["fixture_id"], 1489370)
        self.assertEqual(parameters["home_score"], 4)
        self.assertEqual(parameters["away_score"], 1)

    def test_scheduled_upsert_preserves_incomplete_provider_fixture(self):
        connection = Mock()
        connection.execute.return_value.scalar_one_or_none.return_value = None
        DataIngestionRepository._upsert_match(
            connection,
            {
                "provider_fixture_id": 90101,
                "date": "2026-07-09T20:00:00+00:00",
                "status": "NS",
                "round": "Quarter-finals",
                "home_team": {"name": "Mexico"},
                "away_team": {"name": "South Africa"},
                "raw": {},
            },
            "mexico-id",
            "south-africa-id",
        )

        _statement, parameters = connection.execute.call_args.args
        self.assertIs(parameters["completed"], False)
        self.assertEqual(parameters["status"], "NS")
        self.assertIsNone(parameters["home_score"])
        self.assertIsNone(parameters["away_score"])

    def test_penalties_status_marks_provider_fixture_completed(self):
        connection = Mock()
        connection.execute.return_value.scalar_one_or_none.return_value = False
        result = DataIngestionRepository._upsert_match(
            connection,
            {
                "provider_fixture_id": 90101,
                "date": "2026-07-09T20:00:00+00:00",
                "status": "PEN",
                "round": "Quarter-finals",
                "home_team": {"name": "Mexico"},
                "away_team": {"name": "South Africa"},
                "home_score": 1,
                "away_score": 1,
                "home_penalty_score": 4,
                "away_penalty_score": 3,
                "raw": {"score": {"penalty": {"home": 4, "away": 3}}},
            },
            "mexico-id",
            "south-africa-id",
        )

        _statement, parameters = connection.execute.call_args.args
        self.assertIs(parameters["completed"], True)
        self.assertEqual(parameters["status"], "PEN")
        self.assertEqual(parameters["home_score"], 1)
        self.assertEqual(parameters["away_score"], 1)
        self.assertIs(result["updated_from_scheduled"], True)


if __name__ == "__main__":
    unittest.main()
