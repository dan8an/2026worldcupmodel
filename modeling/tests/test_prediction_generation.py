import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from scripts.generate_predictions import (
    MODEL_VERSION,
    calculate_prediction,
    load_canonical_future_matches,
)

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
create table matches (
  id text primary key,
  kickoff text not null,
  home_team_id text,
  away_team_id text,
  status text
);
create table teams (
  id text primary key,
  name text not null
);
create table team_ratings (
  id integer primary key autoincrement,
  team_id text not null,
  model_run_id text,
  rated_at text,
  updated_at text,
  elo_rating real,
  attack_rating real,
  defense_rating real,
  form_rating real,
  matches_played integer
);
create table player_ratings (
  id integer primary key autoincrement,
  player_id text not null,
  team_id text,
  model_run_id text,
  rated_at text,
  overall_rating real
);
create table model_runs (
  id text primary key,
  run_date text not null,
  model_version text not null,
  notes text,
  data_cutoff text,
  status text,
  random_seed integer,
  generated_at text,
  metadata text
);
create table predictions (
  id text primary key,
  match_id text,
  canonical_match_id text,
  model_run_id text,
  home_xg real,
  away_xg real,
  prediction_timestamp text,
  model_version text,
  confidence_score real,
  elo_base_home_probability real,
  elo_base_draw_probability real,
  elo_base_away_probability real,
  attack_defense_adjustment real,
  draw_calibration_adjustment real,
  context_adjustment_total real,
  final_home_probability real,
  final_draw_probability real,
  final_away_probability real,
  top_factors text,
  home_win_probability real,
  draw_probability real,
  away_win_probability real,
  most_likely_scoreline text,
  expected_total_goals real,
  over_2_5_probability real,
  both_teams_to_score_probability real,
  score_probabilities text,
  created_at text
);
"""


class PredictionCalculationTests(unittest.TestCase):
    def test_probabilities_sum_to_one_and_are_reproducible(self):
        home = {
            "elo_rating": 1560,
            "attack_rating": 72,
            "defense_rating": 65,
            "form_rating": 70,
            "matches_played": 12,
        }
        away = {
            "elo_rating": 1490,
            "attack_rating": 60,
            "defense_rating": 55,
            "form_rating": 45,
            "matches_played": 10,
        }

        first = calculate_prediction(home, away, 64, 51)
        second = calculate_prediction(home, away, 64, 51)

        self.assertEqual(first, second)
        self.assertAlmostEqual(
            first["home_win_probability"]
            + first["draw_probability"]
            + first["away_win_probability"],
            1.0,
            places=12,
        )
        self.assertEqual(len(first["score_probabilities"]), 49)
        self.assertTrue(first["top_factors"])
        self.assertTrue(
            all(
                set(factor) == {"factor", "team", "impact"}
                for factor in first["top_factors"]
            )
        )
        self.assertEqual(
            (
                first["final_home_probability"],
                first["final_draw_probability"],
                first["final_away_probability"],
            ),
            (
                first["home_win_probability"],
                first["draw_probability"],
                first["away_win_probability"],
            ),
        )
        self.assertAlmostEqual(
            sum(score["probability"] for score in first["score_probabilities"]),
            1.0,
            places=9,
        )
        self.assertAlmostEqual(
            sum(
                score["probability"]
                for score in first["score_probabilities"]
                if score["home_goals"] > score["away_goals"]
            ),
            first["home_win_probability"],
            places=9,
        )
        self.assertAlmostEqual(
            sum(
                score["probability"]
                for score in first["score_probabilities"]
                if score["home_goals"] == score["away_goals"]
            ),
            first["draw_probability"],
            places=9,
        )

    def test_recent_form_and_player_inputs_are_disabled_in_v3(self):
        home = {
            "elo_rating": 1560,
            "attack_rating": 72,
            "defense_rating": 65,
            "form_rating": 100,
            "matches_played": 12,
        }
        away = {
            "elo_rating": 1490,
            "attack_rating": 60,
            "defense_rating": 55,
            "form_rating": 0,
            "matches_played": 10,
        }

        with_context = calculate_prediction(home, away, 100, 0)
        without_form_or_players = calculate_prediction(
            {**home, "form_rating": 0},
            {**away, "form_rating": 100},
            None,
            None,
        )

        self.assertEqual(with_context, without_form_or_players)


class PredictionScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "predictions.sqlite3"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_script(self):
        return subprocess.run(
            [sys.executable, "scripts/generate_predictions.py"],
            cwd=ROOT,
            env={
                **os.environ,
                "DATABASE_URL": f"sqlite:///{self.database_path}",
                "PREDICTION_GENERATION_TIME": "2026-06-10T12:00:00+00:00",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def insert_sample_data(self):
        with sqlite3.connect(self.database_path) as connection:
            teams = json.loads((ROOT / "data/seed/teams.json").read_text())
            connection.executemany(
                "insert into teams (id, name) values (?, ?)",
                [(team["id"], team["name"]) for team in teams],
            )
            connection.execute(
                """
                insert into team_ratings (
                  team_id, model_run_id, rated_at, updated_at, elo_rating,
                  attack_rating, defense_rating, form_rating, matches_played
                ) values (?, null, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("MEX", "2026-06-10", "2026-06-10", 1540, 70, 68, 65, 12),
            )

    def test_canonical_source_finds_72_group_matches_before_world_cup(self):
        matches = load_canonical_future_matches(
            datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(len(matches), 72)
        self.assertEqual(matches[0]["canonical_match_id"], "WC26-001")
        self.assertTrue(all(match["stage"] == "group" for match in matches))

    def test_script_updates_without_duplicate_prediction_rows(self):
        self.insert_sample_data()

        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)

        with sqlite3.connect(self.database_path) as connection:
            prediction_rows = connection.execute(
                """
                select
                  canonical_match_id, home_win_probability, draw_probability,
                  away_win_probability, score_probabilities, model_version,
                  top_factors
                from predictions
                """
            ).fetchall()
            runs = connection.execute(
                "select model_version from model_runs"
            ).fetchall()

        self.assertEqual(len(prediction_rows), 72)
        self.assertEqual({row[0] for row in prediction_rows}, {
            f"WC26-{number:03d}" for number in range(1, 73)
        })
        self.assertAlmostEqual(sum(prediction_rows[0][1:4]), 1.0, places=12)
        self.assertEqual(len(json.loads(prediction_rows[0][4])), 49)
        self.assertTrue(all(row[5] == MODEL_VERSION for row in prediction_rows))
        self.assertTrue(all(json.loads(row[6]) for row in prediction_rows))
        self.assertEqual(runs, [(MODEL_VERSION,), (MODEL_VERSION,)])

    def test_no_future_matches_exits_successfully_without_a_run(self):
        env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{self.database_path}",
            "PREDICTION_GENERATION_TIME": "2026-07-20T12:00:00+00:00",
        }
        result = subprocess.run(
            [sys.executable, "scripts/generate_predictions.py"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no future matches found", result.stderr)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("select count(*) from model_runs").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
