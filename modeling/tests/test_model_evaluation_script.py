import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from modeling.src.features.context import HistoricalResult
from scripts.evaluate_model import run_backtest

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
create table evaluation_results (
  id text primary key,
  model_version text not null,
  evaluated_at text not null,
  evaluation_start text not null,
  evaluation_end text not null,
  match_count integer not null,
  brier_score real not null,
  log_loss real not null,
  accuracy real not null,
  elo_brier_score real not null,
  elo_log_loss real not null,
  elo_accuracy real not null,
  market_match_count integer not null,
  market_brier_score real,
  market_log_loss real,
  market_accuracy real,
  calibration text not null,
  confidence_tiers text not null,
  report text not null,
  protocol text not null,
  created_at text not null
);
"""


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


class CurrentModelBacktestTests(unittest.TestCase):
    def test_walk_forward_report_has_required_metrics_and_baselines(self):
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
        report = run_backtest(
            results,
            market_probabilities={
                (date(2022, 1, 1), "AAA", "BBB"): (0.6, 0.25, 0.15)
            },
        )

        self.assertEqual(report["model"]["matches"], 2)
        self.assertIn("brier_score", report["model"])
        self.assertIn("log_loss", report["model"])
        self.assertEqual(len(report["model"]["calibration_bins"]), 10)
        self.assertEqual(report["market_baseline"]["matches"], 1)
        self.assertEqual(
            sum(tier["matches"] for tier in report["confidence_tiers"]),
            2,
        )

    def test_same_day_results_do_not_leak(self):
        warmup = [
            result(2017 + index, 1, "AAA", "BBB", 1, 0)
            for index in range(5)
        ]
        first = run_backtest(
            warmup
            + [
                result(2022, 1, "AAA", "BBB", 9, 0),
                result(2022, 1, "BBB", "AAA", 0, 1),
            ]
        )
        second = run_backtest(
            warmup
            + [
                result(2022, 1, "AAA", "BBB", 1, 0),
                result(2022, 1, "BBB", "AAA", 0, 1),
            ]
        )
        self.assertEqual(first["model"]["brier_score"], second["model"]["brier_score"])


class EvaluationScriptStorageTests(unittest.TestCase):
    def test_script_stores_an_evaluation_result(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "evaluation.sqlite3"
            report_path = Path(directory) / "report.json"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/evaluate_model.py",
                    "--output",
                    str(report_path),
                ],
                cwd=ROOT,
                env={**os.environ, "DATABASE_URL": f"sqlite:///{database}"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "select match_count, report from evaluation_results"
                ).fetchone()
            self.assertGreater(row[0], 0)
            self.assertEqual(json.loads(row[1])["model_version"], "poisson-ratings-v1")
            self.assertTrue(report_path.exists())
