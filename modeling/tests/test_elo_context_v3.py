import unittest
from datetime import date, timedelta

from modeling.src.features.context import HistoricalResult
from scripts.evaluate_model import replay_backtest
from scripts.validate_elo_context_v3 import (
    EloContextParameters,
    build_report,
    context_signals,
    elo_context_probabilities,
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


class EloContextV3Tests(unittest.TestCase):
    def setUp(self):
        results = [
            result(date(2017 + index, 1, 1), "AAA", "BBB", 2, 0)
            for index in range(5)
        ]
        for index in range(20):
            results.append(
                result(
                    date(2022, 1, 1) + timedelta(days=index),
                    "AAA" if index % 2 == 0 else "BBB",
                    "BBB" if index % 2 == 0 else "AAA",
                    1 if index % 3 else 0,
                    0 if index % 3 else 1,
                )
            )
        self.rows, _ = replay_backtest(results)

    def test_zero_context_is_exactly_elo(self):
        row = self.rows[0]
        probabilities = elo_context_probabilities(
            row, EloContextParameters()
        )

        for actual, expected in zip(probabilities, row.elo):
            self.assertAlmostEqual(actual, expected, places=12)

    def test_context_signals_include_available_and_missing_features(self):
        signals = context_signals(self.rows[-1])

        self.assertIn("attack", signals)
        self.assertIn("rest", signals)
        self.assertEqual(signals["player"], 0.0)
        self.assertEqual(signals["travel"], 0.0)
        self.assertEqual(signals["availability"], 0.0)

    def test_report_has_required_ablations(self):
        report = build_report(self.rows)

        self.assertEqual(
            set(report["validation_ablations"]),
            {
                "elo_only",
                "elo_attack_defense",
                "elo_player_ratings",
                "elo_draw_calibration",
                "elo_all_context",
            },
        )
        self.assertIn("favorite_underdog_breakdown", report)
        self.assertFalse(report["promotion"]["production_changed"])
