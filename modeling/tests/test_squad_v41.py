import json
import unittest
from datetime import date, timedelta
from pathlib import Path

from scripts.build_squad_strength import calculate_squad_strength
from scripts.data_ingestion.providers import ApiFootballProvider
from scripts.validate_squad_v41 import SquadValidationRow, build_report

ROOT = Path(__file__).resolve().parents[2]


class SquadProviderParsingTests(unittest.TestCase):
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
        self.assertEqual(result["coverage_level"], 1.0)


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

        report = build_report(rows)

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

    def test_empty_validation_is_valid_insufficient_data_report(self):
        report = build_report([])
        serialized = json.loads(json.dumps(report))

        self.assertEqual(serialized["status"], "insufficient_data")
        self.assertFalse(serialized["promotion"]["recommend_promotion"])


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
        ):
            self.assertIn(field, migration)
        self.assertIn(
            "status in ('available', 'injured', 'suspended', 'unknown')",
            migration,
        )

    def test_production_prediction_and_simulation_do_not_use_squad_v41(self):
        prediction_source = (ROOT / "scripts" / "generate_predictions.py").read_text()
        simulation_source = (ROOT / "scripts" / "run_simulations.py").read_text()

        self.assertIn('MODEL_VERSION = "elo-context-v4"', prediction_source)
        for source in (prediction_source, simulation_source):
            self.assertNotIn("squad_strength_ratings", source)
            self.assertNotIn("player_availability_reports", source)
            self.assertNotIn("validate_squad_v41", source)


if __name__ == "__main__":
    unittest.main()
