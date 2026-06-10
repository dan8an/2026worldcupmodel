import unittest
from datetime import date, timedelta

from modeling.src.features.context import HistoricalResult
from scripts.evaluate_model import replay_backtest
from scripts.validate_calibrated_v2 import (
    build_validation_report,
    chronological_split,
    tune_parameters,
)


def result(played_on, home, away, home_score, away_score):
    return HistoricalResult(
        played_on=played_on,
        home_team_id=home,
        away_team_id=away,
        home_score=home_score,
        away_score=away_score,
        tournament="Friendly",
        neutral=True,
    )


class CalibratedV2ValidationTests(unittest.TestCase):
    def setUp(self):
        results = [
            result(date(2017 + index, 1, 1), "AAA", "BBB", 2, 0)
            for index in range(5)
        ]
        start = date(2022, 1, 1)
        for index in range(20):
            results.append(
                result(
                    start + timedelta(days=index),
                    "AAA" if index % 2 == 0 else "BBB",
                    "BBB" if index % 2 == 0 else "AAA",
                    1 if index % 3 else 0,
                    0 if index % 3 else 1,
                )
            )
        self.rows, _ = replay_backtest(results)

    def test_split_is_chronological_and_within_requested_fraction(self):
        tuning, validation = chronological_split(self.rows, 0.22)

        self.assertLess(
            max(row.played_on for row in tuning),
            min(row.played_on for row in validation),
        )
        self.assertGreaterEqual(len(validation) / len(self.rows), 0.2)
        self.assertLessEqual(len(validation) / len(self.rows), 0.25)

    def test_tuning_returns_parameters_without_validation_data(self):
        tuning, _ = chronological_split(self.rows, 0.22)
        parameters, summary = tune_parameters(tuning)

        self.assertGreaterEqual(parameters.elo_weight, 0.85)
        self.assertEqual(summary["candidate_count"], 648)

    def test_report_contains_validation_diagnostics(self):
        report = build_validation_report(self.rows, 0.22)

        self.assertIn("calibration_buckets", report)
        self.assertIn("draw_accuracy", report)
        self.assertIn("favorite_underdog_breakdown", report)
        self.assertIn("validation_failure_analysis", report)
        self.assertFalse(report["promotion"]["production_changed"])
