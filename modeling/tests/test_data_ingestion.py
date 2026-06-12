import unittest
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
from scripts.database import (
    configure_database_timeouts,
    create_database_engine,
    sqlalchemy_database_url,
)
from scripts.update_data import main, parse_args


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

            def get_completed_matches(self, date):
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

            def get_completed_matches(self, date):
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


if __name__ == "__main__":
    unittest.main()
