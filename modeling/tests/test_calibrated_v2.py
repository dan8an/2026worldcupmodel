import unittest
from datetime import date

from modeling.src.features.context import HistoricalResult
from scripts.evaluate_calibrated_v2 import (
    MODEL_VERSION,
    build_report,
    calibrated_v2_probabilities,
)
from scripts.evaluate_model import replay_backtest


def result(year, month, home, away, home_score, away_score):
    return HistoricalResult(
        played_on=date(year, month, 1),
        home_team_id=home,
        away_team_id=away,
        home_score=home_score,
        away_score=away_score,
        tournament="Friendly",
        neutral=True,
    )


class CalibratedV2Tests(unittest.TestCase):
    def setUp(self):
        results = [
            result(2017 + index, 1, "AAA", "BBB", 2, 0)
            for index in range(5)
        ]
        results.extend(
            [
                result(2022, 1, "AAA", "BBB", 1, 0),
                result(2022, 2, "BBB", "AAA", 0, 1),
            ]
        )
        self.rows, _ = replay_backtest(results)

    def test_probabilities_are_normalized_and_draw_is_increased(self):
        row = self.rows[0]
        probabilities = calibrated_v2_probabilities(row)
        raw_blend_draw = 0.95 * row.elo[1] + 0.05 * row.no_form[1]

        self.assertAlmostEqual(sum(probabilities), 1.0, places=12)
        self.assertGreater(probabilities[1], raw_blend_draw)

    def test_report_compares_all_three_models(self):
        report = build_report(self.rows)

        self.assertEqual(report["model_version"], MODEL_VERSION)
        self.assertEqual(report["dataset"]["matches"], 2)
        self.assertIn("v2", report["metrics"])
        self.assertIn("current_model", report["metrics"])
        self.assertIn("elo_baseline", report["metrics"])
        self.assertEqual(len(report["calibration_table"]), 10)
        self.assertFalse(report["promotion"]["production_changed"])
