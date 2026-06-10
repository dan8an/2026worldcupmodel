import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from modeling.src.data import build_fixtures, load_teams
from scripts.generate_predictions import MODEL_VERSION, calculate_prediction
from scripts.run_simulations import rank_group, simulate_tournaments

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
create table model_runs (
  id text primary key,
  model_version text
);
create table predictions (
  id text primary key,
  canonical_match_id text,
  model_run_id text,
  model_version text,
  prediction_timestamp text,
  home_xg real,
  away_xg real,
  home_win_probability real,
  draw_probability real,
  away_win_probability real,
  score_probabilities text
);
create table simulation_runs (
  id text primary key,
  model_run_id text,
  model_version text,
  num_simulations integer,
  random_seed integer,
  created_at text
);
create table team_simulation_results (
  simulation_run_id text not null,
  team_id text not null,
  group_stage_exit_probability real not null,
  round_of_32_probability real not null,
  round_of_16_probability real not null,
  quarterfinal_probability real not null,
  semifinal_probability real not null,
  final_probability real not null,
  champion_probability real not null,
  created_at text,
  primary key (simulation_run_id, team_id)
);
"""


def canonical_predictions():
    teams = {team.id: team for team in load_teams()}
    predictions = {}
    for fixture in build_fixtures(list(teams.values())):
        if fixture.stage != "group":
            continue
        home = teams[fixture.home_team_id]
        away = teams[fixture.away_team_id]
        prediction = calculate_prediction(
            {
                "elo_rating": home.elo,
                "attack_rating": 50,
                "defense_rating": 50,
                "form_rating": 50,
                "matches_played": 0,
            },
            {
                "elo_rating": away.elo,
                "attack_rating": 50,
                "defense_rating": 50,
                "form_rating": 50,
                "matches_played": 0,
            },
        )
        predictions[fixture.id] = prediction
    return predictions


class SimulationCalculationTests(unittest.TestCase):
    def test_group_tiebreaker_is_deterministic(self):
        table = rank_group(
            ["AAA", "BBB", "CCC", "DDD"],
            [
                ("AAA", "BBB", 0, 0),
                ("CCC", "DDD", 0, 0),
                ("AAA", "CCC", 0, 0),
                ("BBB", "DDD", 0, 0),
                ("AAA", "DDD", 0, 0),
                ("BBB", "CCC", 0, 0),
            ],
        )
        self.assertEqual([row["team_id"] for row in table], ["AAA", "BBB", "CCC", "DDD"])

    def test_simulation_is_reproducible_and_stage_totals_are_correct(self):
        predictions = canonical_predictions()

        first = simulate_tournaments(predictions, 20, 7)
        second = simulate_tournaments(predictions, 20, 7)

        self.assertEqual(first, second)
        expected_totals = {
            "group_stage_exit_probability": 16,
            "round_of_32_probability": 32,
            "round_of_16_probability": 16,
            "quarterfinal_probability": 8,
            "semifinal_probability": 4,
            "final_probability": 2,
            "champion_probability": 1,
        }
        for field, expected in expected_totals.items():
            self.assertAlmostEqual(sum(row[field] for row in first), expected)


class SimulationScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "simulations.sqlite3"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_script(self):
        return subprocess.run(
            [
                sys.executable,
                "scripts/run_simulations.py",
                "--simulations",
                "10",
                "--seed",
                "11",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "DATABASE_URL": f"sqlite:///{self.database_path}",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def insert_predictions(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "insert into model_runs (id, model_version) values (?, ?)",
                ("run-1", MODEL_VERSION),
            )
            rows = []
            for fixture_id, prediction in canonical_predictions().items():
                rows.append(
                    (
                        fixture_id,
                        fixture_id,
                        "run-1",
                        MODEL_VERSION,
                        "2026-06-10T12:00:00+00:00",
                        prediction["home_xg"],
                        prediction["away_xg"],
                        prediction["home_win_probability"],
                        prediction["draw_probability"],
                        prediction["away_win_probability"],
                        json.dumps(prediction["score_probabilities"]),
                    )
                )
            connection.executemany(
                """
                insert into predictions (
                  id, canonical_match_id, model_run_id, model_version,
                  prediction_timestamp, home_xg, away_xg,
                  home_win_probability, draw_probability,
                  away_win_probability, score_probabilities
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def test_script_stores_one_result_per_team(self):
        self.insert_predictions()

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[simulation] SUCCESS", result.stderr)
        with sqlite3.connect(self.database_path) as connection:
            run = connection.execute(
                "select model_version, num_simulations from simulation_runs"
            ).fetchone()
            result_count = connection.execute(
                "select count(*) from team_simulation_results"
            ).fetchone()[0]
        self.assertEqual(run, (MODEL_VERSION, 10))
        self.assertEqual(result_count, 48)

    def test_no_predictions_exits_successfully(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no prediction run available", result.stderr)


if __name__ == "__main__":
    unittest.main()
