import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.collect_market_odds import (
    map_canonical_match_id,
    validate_odds_fixture_identity,
)
from scripts.data_ingestion.providers import ApiFootballProvider
from scripts.evaluate_market_comparison import build_comparison, build_report
from scripts.market_odds import (
    decimal_odds_to_implied_probability,
    disagreement_bucket,
    normalize_1x2_probabilities,
    odds_to_market_probabilities,
    remove_bookmaker_margin,
)

ROOT = Path(__file__).resolve().parents[2]


class OddsConversionTests(unittest.TestCase):
    def test_decimal_odds_convert_to_implied_probability(self):
        self.assertAlmostEqual(decimal_odds_to_implied_probability(2.5), 0.4)
        with self.assertRaises(ValueError):
            decimal_odds_to_implied_probability(1.0)

    def test_devig_probabilities_sum_to_one(self):
        raw = (1 / 2.0, 1 / 3.5, 1 / 4.0)
        devigged = remove_bookmaker_margin(raw)

        self.assertAlmostEqual(sum(devigged), 1.0)
        self.assertTrue(all(0 <= value <= 1 for value in devigged))
        self.assertEqual(devigged, normalize_1x2_probabilities(raw))

    def test_odds_conversion_preserves_raw_overround(self):
        converted = odds_to_market_probabilities(2.0, 3.5, 4.0)

        self.assertGreater(converted["overround"], 1.0)
        self.assertAlmostEqual(sum(converted["devigged"]), 1.0)
        self.assertAlmostEqual(sum(converted["raw"]), converted["overround"])

    def test_disagreement_buckets_have_expected_boundaries(self):
        self.assertEqual(disagreement_bucket(0.0199), "0-2%")
        self.assertEqual(disagreement_bucket(0.02), "2-5%")
        self.assertEqual(disagreement_bucket(0.05), "5-10%")
        self.assertEqual(disagreement_bucket(0.10), "10%+")


class MarketProviderParsingTests(unittest.TestCase):
    def test_api_football_parses_complete_match_winner_odds(self):
        provider = ApiFootballProvider("test-key")
        provider._request = lambda path, **params: [
            {
                "fixture": {"id": 100},
                "update": "2026-06-11T10:00:00+00:00",
                "bookmakers": [
                    {
                        "id": 1,
                        "name": "Example Sportsbook",
                        "bets": [
                            {
                                "name": "Match Winner",
                                "values": [
                                    {"value": "Home", "odd": "2.10"},
                                    {"value": "Draw", "odd": "3.20"},
                                    {"value": "Away", "odd": "3.80"},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]

        rows = provider.get_odds(100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider_fixture_id"], 100)
        self.assertEqual(rows[0]["home_decimal_odds"], 2.1)
        self.assertEqual(rows[0]["draw_decimal_odds"], 3.2)
        self.assertEqual(rows[0]["away_decimal_odds"], 3.8)

    def test_provider_fixture_maps_to_canonical_match_without_database_row(self):
        fixture = {
            "canonical_home_team_code": "MEX",
            "canonical_away_team_code": "RSA",
            "date": "2026-06-11T19:00:00+00:00",
        }

        self.assertEqual(map_canonical_match_id(fixture), "WC26-001")

    def test_odds_fixture_id_mismatch_is_rejected(self):
        fixture = {"provider_fixture_id": 100}
        odds_rows = [{"provider_fixture_id": 101}]

        with self.assertRaisesRegex(ValueError, "Odds fixture mismatch"):
            validate_odds_fixture_identity(fixture, odds_rows)


class MarketComparisonReportTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "id": "snapshot-1",
            "match_id": None,
            "canonical_match_id": "WC26-001",
            "provider_fixture_id": 100,
            "provider_home_team_id": 16,
            "provider_away_team_id": 1531,
            "provider_home_team_name": "Mexico",
            "provider_away_team_name": "South Africa",
            "canonical_home_team_code": "MEX",
            "canonical_away_team_code": "RSA",
            "bookmaker": "Example Sportsbook",
            "source": "api_football",
            "collected_at": "2026-06-11T10:00:00+00:00",
            "home_decimal_odds": 2.0,
            "draw_decimal_odds": 3.5,
            "away_decimal_odds": 4.0,
        }
        self.prediction = {
            "canonical_match_id": "WC26-001",
            "canonical_home_team_code": "MEX",
            "canonical_away_team_code": "RSA",
            "model_run_id": "run-1",
            "model_version": "elo-context-v4",
            "prediction_timestamp": "2026-06-11T09:00:00+00:00",
            "home_win_probability": 0.55,
            "draw_probability": 0.25,
            "away_win_probability": 0.20,
        }

    def test_report_output_is_current_only_without_historical_outcome(self):
        comparison = build_comparison(self.snapshot, self.prediction)
        report = build_report([comparison], snapshot_count=1)
        serialized = json.loads(json.dumps(report))

        self.assertEqual(serialized["status"], "current_comparison_only")
        self.assertEqual(
            serialized["coverage"]["snapshots_with_model_comparison"], 1
        )
        self.assertEqual(serialized["coverage"]["comparison_matches"], 1)
        self.assertEqual(
            serialized["coverage"]["historical_validation_matches"], 0
        )
        self.assertIsNone(serialized["model_metrics"])
        self.assertIsNone(serialized["market_metrics"])
        self.assertAlmostEqual(
            sum(comparison["market_probability"].values()), 1.0
        )

    def test_historical_report_contains_brier_log_loss_and_calibration(self):
        comparison = build_comparison(
            self.snapshot,
            self.prediction,
            outcome=0,
            kickoff=datetime(2026, 6, 11, 19, tzinfo=timezone.utc),
        )
        report = build_report([comparison], snapshot_count=1)

        self.assertEqual(report["status"], "historical_validation_complete")
        for metrics in (report["model_metrics"], report["market_metrics"]):
            self.assertIn("brier_score", metrics)
            self.assertIn("log_loss", metrics)
            self.assertIn("calibration_bins", metrics)
        self.assertIn("brier_score_delta", report["model_vs_market"])
        self.assertEqual(sum(report["disagreement_buckets"].values()), 1)

    def test_swapped_market_home_away_identity_is_rejected(self):
        swapped = {
            **self.snapshot,
            "canonical_home_team_code": "RSA",
            "canonical_away_team_code": "MEX",
        }

        self.assertIsNone(build_comparison(swapped, self.prediction))

    def test_swapped_model_home_away_identity_is_rejected(self):
        swapped = {
            **self.prediction,
            "canonical_home_team_code": "RSA",
            "canonical_away_team_code": "MEX",
        }

        self.assertIsNone(build_comparison(self.snapshot, swapped))

    def test_empty_report_is_honest_insufficient_coverage(self):
        report = build_report([], snapshot_count=0)

        self.assertEqual(report["status"], "insufficient_coverage")
        self.assertFalse(
            report["coverage"]["historical_validation_available"]
        )
        self.assertIsNone(report["model_vs_market"])


class MarketSchemaAndIsolationTests(unittest.TestCase):
    def test_migration_contains_research_tables_and_probability_fields(self):
        migration = (
            ROOT
            / "supabase"
            / "migrations"
            / "202606110007_market_comparison_v5.sql"
        ).read_text()
        for table in (
            "market_odds_snapshots",
            "market_implied_probabilities",
            "market_comparison_reports",
        ):
            self.assertIn(f"create table if not exists public.{table}", migration)
        for field in (
            "canonical_match_id",
            "home_decimal_odds",
            "draw_decimal_odds",
            "away_decimal_odds",
            "raw_home_probability",
            "devig_home_probability",
            "model_home_probability",
            "home_probability_difference",
            "average_absolute_disagreement",
            "provider_home_team_id",
            "provider_away_team_id",
            "canonical_home_team_code",
            "canonical_away_team_code",
        ):
            self.assertIn(field, migration)
        identity_migration = (
            ROOT
            / "supabase"
            / "migrations"
            / "202606110008_market_comparison_identity.sql"
        ).read_text()
        self.assertIn(
            "add column if not exists provider_home_team_id",
            identity_migration,
        )
        self.assertIn(
            "add column if not exists canonical_home_team_code",
            identity_migration,
        )

    def test_production_prediction_generation_has_no_market_dependency(self):
        source = (ROOT / "scripts" / "generate_predictions.py").read_text()

        self.assertIn('MODEL_VERSION = "elo-context-v4.1"', source)
        self.assertNotIn("market_odds_snapshots", source)
        self.assertNotIn("market_implied_probabilities", source)
        self.assertNotIn("evaluate_market_comparison", source)

    def test_market_comparison_is_not_exposed_by_api_yet(self):
        schema_source = (ROOT / "apps" / "api" / "app" / "schemas.py").read_text()
        service_source = (ROOT / "apps" / "api" / "app" / "service.py").read_text()

        self.assertNotIn("market_comparison", schema_source)
        self.assertNotIn("market_implied_probabilities", service_source)


if __name__ == "__main__":
    unittest.main()
