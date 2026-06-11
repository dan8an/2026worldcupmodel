import unittest

from scripts.confidence_v1 import DataCompleteness, calculate_confidence
from scripts.evaluate_model import replay_backtest
from scripts.validate_confidence_v1 import build_report


class ConfidenceV1Tests(unittest.TestCase):
    def test_score_is_bounded_and_does_not_mutate_probabilities(self):
        elo = (0.48, 0.27, 0.25)
        final = (0.53, 0.28, 0.19)
        original = tuple(final)

        confidence = calculate_confidence(
            elo,
            final,
            DataCompleteness(True, True, False, True),
            [
                {
                    "count": 40,
                    "mean_probability": 0.55,
                    "observed_rate": 0.57,
                }
                for _ in range(10)
            ],
        )

        self.assertGreaterEqual(confidence["confidence_score"], 0)
        self.assertLessEqual(confidence["confidence_score"], 100)
        self.assertIn(confidence["confidence_tier"], {"High", "Medium", "Low"})
        self.assertTrue(confidence["confidence_explanation"])
        self.assertEqual(final, original)

    def test_historical_tiers_separate_on_chronological_holdout(self):
        rows, _ = replay_backtest()
        report = build_report(rows)
        metrics = report["tier_metrics"]

        self.assertTrue(report["quality_ordering"]["high_beats_medium_beats_low"])
        self.assertTrue(all(metrics[tier]["matches"] > 0 for tier in metrics))
        self.assertLess(metrics["High"]["brier_score"], metrics["Medium"]["brier_score"])
        self.assertLess(metrics["Medium"]["brier_score"], metrics["Low"]["brier_score"])
        self.assertLess(metrics["High"]["log_loss"], metrics["Medium"]["log_loss"])
        self.assertLess(metrics["Medium"]["log_loss"], metrics["Low"]["log_loss"])


if __name__ == "__main__":
    unittest.main()
