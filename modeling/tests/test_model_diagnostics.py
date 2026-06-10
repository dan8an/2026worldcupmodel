import unittest
from datetime import date

from modeling.src.features.context import HistoricalResult
from scripts.diagnose_model import build_diagnostics
from scripts.evaluate_model import replay_backtest


def result(year, month, home, away, home_score, away_score, neutral=True):
    return HistoricalResult(
        played_on=date(year, month, 1),
        home_team_id=home,
        away_team_id=away,
        home_score=home_score,
        away_score=away_score,
        tournament="Friendly",
        neutral=neutral,
    )


class ModelDiagnosticTests(unittest.TestCase):
    def test_report_contains_requested_diagnostic_sections(self):
        results = [
            result(2017 + index, 1, "MEX", "USA", 2, 0)
            for index in range(5)
        ]
        results.extend(
            [
                result(2022, 1, "MEX", "USA", 1, 0),
                result(2022, 2, "USA", "MEX", 1, 1),
            ]
        )
        rows, _ = replay_backtest(results)
        report = build_diagnostics(rows)

        self.assertEqual(report["dataset"]["matches"], 2)
        self.assertEqual(len(report["top_5_error_patterns"]), 5)
        self.assertIn("draw_probability", report)
        self.assertIn("expected_goals", report)
        self.assertIn("recent_form", report)
        self.assertIn("recommended_model_changes", report)
        self.assertEqual(
            sum(item["matches"] for item in report["years"]),
            2,
        )

    def test_replay_exposes_form_neutral_counterfactual_and_xg(self):
        results = [
            result(2017 + index, 1, "MEX", "USA", 3, 0)
            for index in range(5)
        ]
        results.append(result(2022, 1, "MEX", "USA", 1, 0))
        rows, _ = replay_backtest(results)

        self.assertGreater(rows[0].home_xg, 0)
        self.assertAlmostEqual(sum(rows[0].no_form), 1.0)
        self.assertNotEqual(rows[0].model, rows[0].no_form)
