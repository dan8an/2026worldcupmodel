import argparse
import logging
import unittest

from scripts.backfill_historical_stats import parse_args, run_backfill, validate_args
from scripts.data_ingestion import RateLimitError


def fixture(fixture_id):
    return {
        "provider_fixture_id": fixture_id,
        "date": f"2022-11-{fixture_id:02d}T16:00:00+00:00",
        "home_team": {"provider_id": fixture_id * 2, "name": f"Home {fixture_id}"},
        "away_team": {
            "provider_id": fixture_id * 2 + 1,
            "name": f"Away {fixture_id}",
        },
    }


class HistoricalStatsBackfillTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("historical-backfill-test")
        self.args = argparse.Namespace(
            league_id=1,
            season=2022,
            date_from="2022-11-20",
            date_to="2022-12-18",
            max_fixtures=2,
        )

    def test_cli_defaults_to_safe_small_run(self):
        args = parse_args([])
        self.assertEqual(args.league_id, 1)
        self.assertEqual(args.max_fixtures, 5)
        self.assertEqual(args.date_from, args.date_to)

    def test_validation_rejects_reversed_dates(self):
        self.args.date_from = "2022-12-19"
        with self.assertRaisesRegex(ValueError, "cannot be after"):
            validate_args(self.args)

    def test_backfill_limits_fixtures_and_reports_exact_insert_counts(self):
        class Provider:
            def get_completed_matches_range(self, *args):
                return [fixture(3), fixture(1), fixture(2)]

            def get_fixture_statistics(self, fixture_id):
                return [{"team": {"provider_id": fixture_id * 2}}, {"team": {}}]

        class Repository:
            def __init__(self):
                self.ids = []

            def find_provider_fixtures_with_complete_team_stats(self, fixture_ids):
                return {3}

            def ingest_historical_team_fixture(self, item, statistics):
                self.ids.append(item["provider_fixture_id"])
                return {
                    "fixtures_inserted": 1,
                    "fixtures_updated": 0,
                    "team_stats_inserted": len(statistics),
                    "team_stats_updated": 0,
                }

        repository = Repository()
        summary = run_backfill(self.args, Provider(), repository, self.logger)

        self.assertEqual(repository.ids, [1, 2])
        self.assertEqual(summary.fixtures_discovered, 3)
        self.assertEqual(summary.fixtures_skipped_complete, 1)
        self.assertEqual(summary.fixtures_selected, 2)
        self.assertEqual(summary.fixtures_inserted, 2)
        self.assertEqual(summary.team_stats_inserted, 4)

    def test_rate_limit_preserves_partial_success(self):
        class Provider:
            def get_completed_matches_range(self, *args):
                return [fixture(1), fixture(2)]

            def get_fixture_statistics(self, fixture_id):
                if fixture_id == 2:
                    raise RateLimitError("60")
                return [{}, {}]

        class Repository:
            def find_provider_fixtures_with_complete_team_stats(self, fixture_ids):
                return set()

            def ingest_historical_team_fixture(self, item, statistics):
                return {
                    "fixtures_inserted": 1,
                    "fixtures_updated": 0,
                    "team_stats_inserted": 2,
                    "team_stats_updated": 0,
                }

        summary = run_backfill(self.args, Provider(), Repository(), self.logger)

        self.assertTrue(summary.rate_limited)
        self.assertEqual(summary.fixtures_processed, 1)
        self.assertEqual(summary.fixtures_inserted, 1)
        self.assertEqual(summary.team_stats_inserted, 2)


if __name__ == "__main__":
    unittest.main()
