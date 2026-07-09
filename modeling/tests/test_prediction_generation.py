import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from modeling.src.data import build_fixtures, load_teams
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    DrawCalibrationFeatures,
    MODEL_VERSION,
    PredictionRepository,
    SHOT_VOLUME_WEIGHT,
    assert_complete_group_predictions,
    calculate_prediction,
    calibrate_draw_probability,
    canonical_prior_elo,
    load_canonical_future_matches,
)
from scripts.run_simulations import (
    build_knockout_prediction_provider,
    simulate_tournaments,
)

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
create table matches (
  id text primary key,
  kickoff text not null,
  tournament_stage text,
  home_team_id text,
  away_team_id text,
  api_football_fixture_id integer,
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
create table team_chance_quality_ratings (
  id integer primary key autoincrement,
  team_id text not null,
  rated_at text not null,
  model_version text not null,
  sample_matches integer not null,
  shot_volume_rating real,
  shot_quality_proxy real,
  box_shot_rate real,
  shots_on_target_rate real,
  chance_creation_rating real,
  defensive_shot_suppression real,
  keeper_pressure_allowed real,
  components text
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
  confidence_tier text,
  confidence_explanation text,
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
    def test_v4_applies_only_validated_shot_volume_tilt(self):
        home = {
            "elo_rating": 1560,
            "attack_rating": 72,
            "defense_rating": 65,
        }
        away = {
            "elo_rating": 1490,
            "attack_rating": 60,
            "defense_rating": 55,
        }
        v3 = calculate_prediction(home, away)
        v4 = calculate_prediction(
            home,
            away,
            home_shot_volume_rating=90,
            away_shot_volume_rating=40,
            home_team_name="Brazil",
            away_team_name="Morocco",
        )
        tilt = ((90 - 40) / 100) * SHOT_VOLUME_WEIGHT

        self.assertEqual(MODEL_VERSION, "elo-context-v4.2.1")
        self.assertGreater(v4["home_win_probability"], v3["home_win_probability"])
        self.assertLess(v4["away_win_probability"], v3["away_win_probability"])
        self.assertLessEqual(v4["draw_probability"], v3["draw_probability"])
        self.assertAlmostEqual(
            v4["home_win_probability"]
            + v4["draw_probability"]
            + v4["away_win_probability"],
            1.0,
            places=12,
        )
        self.assertAlmostEqual(tilt, 0.5 * SHOT_VOLUME_WEIGHT)
        self.assertTrue(
            any(
                factor["factor"] == "Shot volume"
                and factor["team"] == "Brazil"
                for factor in v4["top_factors"]
            )
        )
        self.assertGreaterEqual(v4["confidence_score"], 0)
        self.assertLessEqual(v4["confidence_score"], 100)

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
        self.assertGreaterEqual(first["confidence_score"], 0)
        self.assertLessEqual(first["confidence_score"], 100)
        self.assertIn(first["confidence_tier"], {"High", "Medium", "Low"})
        self.assertTrue(first["confidence_explanation"])
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

    def test_v4_preserves_disabled_form_and_player_inputs(self):
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

        for field in (
            "home_win_probability",
            "draw_probability",
            "away_win_probability",
            "score_probabilities",
        ):
            self.assertEqual(with_context[field], without_form_or_players[field])

    def test_v42_ignores_rest_inputs(self):
        home = {
            "elo_rating": 1510,
            "attack_rating": 55,
            "defense_rating": 60,
        }
        away = {
            "elo_rating": 1490,
            "attack_rating": 50,
            "defense_rating": 52,
        }

        stale_gap = calculate_prediction(
            home,
            away,
            home_rest_days=710,
            away_rest_days=2,
        )
        reversed_gap = calculate_prediction(
            home,
            away,
            home_rest_days=2,
            away_rest_days=710,
        )

        self.assertEqual(stale_gap, reversed_gap)
        self.assertFalse(
            any(
                "rest" in factor["factor"].lower()
                or "recovery" in factor["factor"].lower()
                for factor in stale_gap["top_factors"]
            )
        )

    def test_draw_calibration_elevates_even_low_goal_fixture(self):
        prediction = calculate_prediction(
            {"elo_rating": 1500, "attack_rating": 42, "defense_rating": 78},
            {"elo_rating": 1503, "attack_rating": 43, "defense_rating": 76},
        )

        self.assertGreater(prediction["draw_probability"], 0.31)
        self.assertLess(prediction["draw_probability"], 0.40)
        self.assertGreater(
            prediction["draw_probability"],
            prediction["legacy_v41_draw_probability"],
        )
        self.assertAlmostEqual(
            prediction["home_win_probability"]
            + prediction["draw_probability"]
            + prediction["away_win_probability"],
            1.0,
            places=12,
        )

    def test_draw_calibration_reduces_mismatch_fixture(self):
        prediction = calculate_prediction(
            {"elo_rating": 1690, "attack_rating": 82, "defense_rating": 78},
            {"elo_rating": 1390, "attack_rating": 43, "defense_rating": 45},
        )

        self.assertGreaterEqual(prediction["draw_probability"], 0.18)
        self.assertLess(prediction["draw_probability"], 0.20)
        self.assertLess(
            prediction["draw_probability"],
            prediction["legacy_v41_draw_probability"],
        )

    def test_draw_calibration_does_not_overboost_high_goal_even_fixture(self):
        prediction = calculate_prediction(
            {"elo_rating": 1500, "attack_rating": 82, "defense_rating": 42},
            {"elo_rating": 1500, "attack_rating": 80, "defense_rating": 44},
        )

        self.assertLess(prediction["draw_probability"], 0.30)
        self.assertLessEqual(
            prediction["draw_probability"],
            prediction["legacy_v41_draw_probability"],
        )

    def test_draw_calibration_has_wider_spread_than_legacy_multiplier(self):
        scenarios = [
            DrawCalibrationFeatures(0, 2.2, 42, 43, 78, 76),
            DrawCalibrationFeatures(25, 2.6, 54, 52, 58, 57),
            DrawCalibrationFeatures(180, 2.9, 78, 50, 72, 48),
            DrawCalibrationFeatures(0, 3.3, 82, 80, 42, 44),
            DrawCalibrationFeatures(-320, 3.6, 35, 85, 40, 80, -80),
        ]
        base = (0.36, 0.28, 0.36)
        calibrated_draws = [
            calibrate_draw_probability(base, scenario)[1]
            for scenario in scenarios
        ]
        legacy_draws = [
            (
                base[1] * 1.15
                / (base[0] + base[1] * 1.15 + base[2])
            )
            for _ in scenarios
        ]

        calibrated_spread = max(calibrated_draws) - min(calibrated_draws)
        legacy_spread = max(legacy_draws) - min(legacy_draws)

        self.assertGreater(calibrated_spread, 0.15)
        self.assertGreater(calibrated_spread, legacy_spread)
        self.assertGreater(max(calibrated_draws), 0.32)
        self.assertLess(min(calibrated_draws), 0.19)


class PredictionScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "predictions.sqlite3"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_script(self, generation_time: str = "2026-06-10T12:00:00+00:00"):
        return subprocess.run(
            [sys.executable, "scripts/generate_predictions.py"],
            cwd=ROOT,
            env={
                **os.environ,
                "DATABASE_URL": f"sqlite:///{self.database_path}",
                "PREDICTION_GENERATION_TIME": generation_time,
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
            connection.executemany(
                """
                insert into team_chance_quality_ratings (
                  team_id, rated_at, model_version, sample_matches,
                  shot_volume_rating, shot_quality_proxy,
                  defensive_shot_suppression, chance_creation_rating,
                  components
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "MEX",
                        "2026-06-10",
                        "xg-proxy-v4",
                        10,
                        90,
                        0,
                        0,
                        0,
                        "{}",
                    ),
                    (
                        "RSA",
                        "2026-06-10",
                        "xg-proxy-v4",
                        10,
                        40,
                        100,
                        100,
                        100,
                        "{}",
                    ),
                ],
            )

    def test_canonical_source_finds_72_group_matches_before_world_cup(self):
        matches = load_canonical_future_matches(
            datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(len(matches), 72)
        self.assertEqual(matches[0]["canonical_match_id"], "WC26-001")
        self.assertTrue(all(match["stage"] == "group" for match in matches))

    def test_canonical_source_keeps_all_72_matches_during_group_stage(self):
        matches = load_canonical_future_matches(
            datetime(2026, 6, 12, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(len(matches), 72)
        self.assertEqual(matches[0]["canonical_match_id"], "WC26-001")

    def test_canonical_source_does_not_generate_knockouts_after_group_stage(self):
        matches = load_canonical_future_matches(
            datetime(2026, 6, 28, 12, tzinfo=timezone.utc)
        )

        self.assertEqual(matches, [])

    def test_canonical_source_includes_real_known_upcoming_knockout(self):
        matches = load_canonical_future_matches(
            datetime(2026, 6, 28, 12, tzinfo=timezone.utc),
            database_matches=[
                {
                    "id": "provider-r32",
                    "match_number": 73,
                    "match_date": "2026-06-28T16:00:00+00:00",
                    "tournament_stage": "Round of 32",
                    "home_team_id": "mexico-db",
                    "away_team_id": "south-africa-db",
                }
            ],
            database_team_ids={
                "MEX": "mexico-db",
                "RSA": "south-africa-db",
            },
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["canonical_match_id"], "provider-r32")
        self.assertEqual(matches[0]["stage"], "round_of_32")
        self.assertEqual(matches[0]["home_team_id"], "MEX")
        self.assertEqual(matches[0]["away_team_id"], "RSA")

    def test_canonical_source_includes_real_known_upcoming_quarterfinal(self):
        matches = load_canonical_future_matches(
            datetime(2026, 7, 8, 12, tzinfo=timezone.utc),
            database_matches=[
                {
                    "id": "provider-qf",
                    "match_number": 97,
                    "match_date": "2026-07-09T20:00:00+00:00",
                    "tournament_stage": "Quarter-finals",
                    "home_team_id": "mexico-db",
                    "away_team_id": "south-africa-db",
                    "api_football_fixture_id": 90101,
                    "status": "scheduled",
                }
            ],
            database_team_ids={
                "MEX": "mexico-db",
                "RSA": "south-africa-db",
            },
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["canonical_match_id"], "provider-qf")
        self.assertEqual(matches[0]["database_match_id"], "provider-qf")
        self.assertEqual(matches[0]["provider_fixture_id"], 90101)
        self.assertEqual(matches[0]["stage"], "quarterfinal")

    def test_canonical_source_skips_unknown_knockout_placeholders(self):
        matches = load_canonical_future_matches(
            datetime(2026, 6, 28, 12, tzinfo=timezone.utc),
            database_matches=[
                {
                    "id": "placeholder-r32",
                    "match_number": 73,
                    "match_date": "2026-06-28T16:00:00+00:00",
                    "tournament_stage": "Round of 32",
                    "home_team": "Winner Group A",
                    "away_team": "Runner-up Group B",
                }
            ],
            database_team_ids={},
        )

        self.assertEqual(matches, [])

    def test_incomplete_generation_reports_exact_missing_fixtures(self):
        predictions = [
            {"canonical_match_id": f"WC26-{number:03d}"}
            for number in range(3, 73)
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            (
                r"missing 2 canonical group fixtures: "
                r"WC26-001 \(Mexico vs South Africa\), "
                r"WC26-002 \(South Korea vs Czechia\)"
            ),
        ):
            assert_complete_group_predictions(predictions)

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
                  top_factors, confidence_score, confidence_tier,
                  confidence_explanation
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
        self.assertTrue(all(0 <= row[7] <= 100 for row in prediction_rows))
        self.assertTrue(
            all(row[8] in {"High", "Medium", "Low"} for row in prediction_rows)
        )
        self.assertTrue(all(row[9] for row in prediction_rows))
        self.assertEqual(runs, [(MODEL_VERSION,), (MODEL_VERSION,)])
        first_factors = json.loads(
            next(row[6] for row in prediction_rows if row[0] == "WC26-001")
        )
        self.assertTrue(
            any(factor["factor"] == "Shot volume" for factor in first_factors)
        )
        first_row = next(row for row in prediction_rows if row[0] == "WC26-001")
        teams = {team.id: team for team in load_teams()}
        expected = calculate_prediction(
            {
                "elo_rating": 1540,
                "attack_rating": 70,
                "defense_rating": 68,
                "_team_rating_available": True,
                "_attack_defense_available": True,
            },
            {
                "elo_rating": canonical_prior_elo(teams["RSA"].rank),
                "attack_rating": 50,
                "defense_rating": 50,
                "_team_rating_available": False,
                "_attack_defense_available": False,
            },
            home_shot_volume_rating=90,
            away_shot_volume_rating=40,
            home_team_name="Mexico",
            away_team_name="South Africa",
        )
        for actual, field in zip(
            first_row[1:4],
            (
                "home_win_probability",
                "draw_probability",
                "away_win_probability",
            ),
        ):
            self.assertAlmostEqual(actual, expected[field], places=12)

    def test_script_supports_knockout_only_future_fixture_run(self):
        self.insert_sample_data()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                insert into matches (
                  id, kickoff, tournament_stage, home_team_id, away_team_id,
                  api_football_fixture_id, status
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "provider-qf-90101",
                    "2026-07-09T20:00:00+00:00",
                    "Quarter-finals",
                    "MEX",
                    "RSA",
                    90101,
                    "scheduled",
                ),
            )

        result = self.run_script("2026-07-08T12:00:00+00:00")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Knockout predictions generated=1", result.stderr)
        with sqlite3.connect(self.database_path) as connection:
            prediction_rows = connection.execute(
                """
                select canonical_match_id, match_id, home_win_probability,
                       draw_probability, away_win_probability
                from predictions
                """
            ).fetchall()

        self.assertEqual(len(prediction_rows), 1)
        self.assertEqual(prediction_rows[0][0], "provider-qf-90101")
        self.assertEqual(prediction_rows[0][1], "provider-qf-90101")
        self.assertAlmostEqual(sum(prediction_rows[0][2:5]), 1.0, places=12)

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

    def test_alias_mapping_is_unique_and_all_teams_have_rating_source(self):
        with sqlite3.connect(self.database_path) as connection:
            teams = json.loads((ROOT / "data/seed/teams.json").read_text())
            database_names = {
                "CZE": "Czech Republic",
                "BIH": "Bosnia & Herzegovina",
                "USA": "USA",
                "TUR": "Türkiye",
                "CUW": "Curaçao",
                "CPV": "Cape Verde Islands",
                "COD": "Congo DR",
            }
            database_teams = [
                (
                    f"db-{team['id']}",
                    database_names.get(team["id"], team["name"]),
                )
                for team in teams
                if team["id"] != "NZL"
            ]
            connection.executemany(
                "insert into teams (id, name) values (?, ?)",
                database_teams,
            )
            connection.executemany(
                """
                insert into team_ratings (
                  team_id, model_run_id, rated_at, updated_at, elo_rating,
                  attack_rating, defense_rating, form_rating, matches_played
                ) values (?, null, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        team_id,
                        "2026-06-10",
                        "2026-06-10",
                        1500 + index,
                        50,
                        50,
                        50,
                        10,
                    )
                    for index, (team_id, _) in enumerate(database_teams)
                ],
            )

        engine = create_database_engine(f"sqlite:///{self.database_path}")
        try:
            repository = PredictionRepository(engine)
            mapping = repository.load_database_team_ids()
            ratings = repository.load_current_team_ratings(mapping)
        finally:
            engine.dispose()

        new_zealand = next(team for team in load_teams() if team.id == "NZL")
        self.assertEqual(len(mapping), 48)
        self.assertEqual(len({value for value in mapping.values() if value}), 47)
        self.assertEqual(mapping["CZE"], "db-CZE")
        self.assertEqual(mapping["BIH"], "db-BIH")
        self.assertEqual(ratings["CZE"]["_rating_source"], "database_current")
        self.assertEqual(ratings["BIH"]["_rating_source"], "database_current")
        self.assertEqual(ratings["NZL"]["_rating_source"], "canonical_rank_prior")
        self.assertEqual(
            ratings["NZL"]["elo_rating"],
            canonical_prior_elo(new_zealand.rank),
        )
        self.assertTrue(all(rating["_rating_source"] for rating in ratings.values()))
        self.assertTrue(
            all(
                rating["elo_rating"] < 1700
                for rating in ratings.values()
                if rating["_rating_source"] == "canonical_rank_prior"
            )
        )

        predictions = {}
        for fixture in build_fixtures(load_teams()):
            if fixture.stage != "group":
                continue
            predictions[fixture.id] = calculate_prediction(
                ratings[fixture.home_team_id],
                ratings[fixture.away_team_id],
            )
        simulation = {
            row["team_id"]: row
            for row in simulate_tournaments(
                predictions,
                2_000,
                2026,
                build_knockout_prediction_provider(ratings),
            )
        }
        self.assertLess(simulation["CZE"]["champion_probability"], 0.05)
        self.assertLess(simulation["BIH"]["champion_probability"], 0.05)


if __name__ == "__main__":
    unittest.main()
