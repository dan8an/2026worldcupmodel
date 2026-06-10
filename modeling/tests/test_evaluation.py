import unittest
from datetime import date

from modeling.src.evaluation.backtest import run_backtest
from modeling.src.evaluation.metrics import evaluate
from modeling.src.features.context import HistoricalResult


class MetricTests(unittest.TestCase):
    def test_perfect_predictions_have_zero_scores(self):
        metrics = evaluate(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            [0, 1, 2],
        )
        self.assertAlmostEqual(metrics["log_loss"], 0.0)
        self.assertAlmostEqual(metrics["brier_score"], 0.0)
        self.assertAlmostEqual(metrics["ranked_probability_score"], 0.0)
        self.assertEqual(metrics["accuracy"], 1.0)

    def test_equal_probabilities_have_expected_log_loss(self):
        metrics = evaluate([(1 / 3, 1 / 3, 1 / 3)] * 3, [0, 1, 2])
        self.assertAlmostEqual(metrics["log_loss"], 1.098612, places=6)
        self.assertAlmostEqual(metrics["expected_calibration_error"], 0.0)


class BacktestTests(unittest.TestCase):
    def test_backtest_is_reproducible_and_uses_prior_matches(self):
        results = []
        for year in range(2017, 2022):
            results.append(
                HistoricalResult(
                    played_on=date(year, 1, 1),
                    home_team_id="AAA",
                    away_team_id="BBB",
                    home_score=2,
                    away_score=0,
                    tournament="Friendly",
                    neutral=True,
                )
            )
        results.extend(
            [
                HistoricalResult(
                    played_on=date(2022, 1, 1),
                    home_team_id="AAA",
                    away_team_id="BBB",
                    home_score=1,
                    away_score=0,
                    tournament="Friendly",
                    neutral=True,
                ),
                HistoricalResult(
                    played_on=date(2022, 2, 1),
                    home_team_id="BBB",
                    away_team_id="AAA",
                    home_score=0,
                    away_score=1,
                    tournament="Friendly",
                    neutral=True,
                ),
            ]
        )
        first = run_backtest(results=results)
        second = run_backtest(results=results)
        self.assertEqual(first, second)
        self.assertEqual(first["aggregate"]["elo"]["matches"], 2)
        self.assertEqual(
            first["protocol"]["same_day_updates"],
            "batched_after_predictions",
        )
