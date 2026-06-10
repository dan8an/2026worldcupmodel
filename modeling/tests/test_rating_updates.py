import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


SCHEMA = """
create table matches (
  id text primary key,
  match_date text
);
create table team_match_stats (
  id integer primary key autoincrement,
  match_id text not null,
  team_id text not null,
  goals integer,
  captured_at text
);
create table player_match_stats (
  id integer primary key autoincrement,
  match_id text not null,
  player_id text not null,
  team_id text not null,
  minutes_played integer,
  goals integer,
  assists integer,
  shots integer,
  shots_on_target integer,
  captured_at text
);
create table team_ratings (
  id integer primary key autoincrement,
  team_id text not null,
  model_run_id text,
  rated_at text not null,
  elo_rating real,
  attack_rating real,
  defense_rating real,
  form_rating real,
  matches_played integer,
  goals_for integer,
  goals_against integer,
  updated_at text not null,
  sample_matches integer,
  components text
);
create table player_ratings (
  id integer primary key autoincrement,
  player_id text not null,
  team_id text,
  model_run_id text,
  rated_at text not null,
  overall_rating real,
  goal_threat real,
  assist_threat real,
  shot_volume real,
  minutes_rating real,
  form_rating real,
  matches_played integer,
  minutes_played integer,
  updated_at text not null,
  attacking_rating real,
  creative_rating real,
  availability_rating real,
  projected_minutes real,
  components text
);
"""


class RatingUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "ratings.sqlite3"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA)

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_script(self):
        env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{self.database_path}",
        }
        return subprocess.run(
            [sys.executable, "scripts/update_ratings.py"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def insert_sample_data(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.executemany(
                "insert into matches (id, match_date) values (?, ?)",
                [("m1", "2026-06-01"), ("m2", "2026-06-05")],
            )
            connection.executemany(
                """
                insert into team_match_stats
                  (match_id, team_id, goals, captured_at)
                values (?, ?, ?, ?)
                """,
                [
                    ("m1", "team-a", 2, "2026-06-01"),
                    ("m1", "team-b", 1, "2026-06-01"),
                    ("m2", "team-b", 0, "2026-06-05"),
                    ("m2", "team-a", 0, "2026-06-05"),
                ],
            )
            connection.executemany(
                """
                insert into player_match_stats (
                  match_id, player_id, team_id, minutes_played, goals,
                  assists, shots, shots_on_target, captured_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("m1", "player-a", "team-a", 90, 1, 1, 4, 2, "2026-06-01"),
                    ("m2", "player-a", "team-a", 75, 0, 0, 2, 1, "2026-06-05"),
                    ("m1", "player-b", "team-b", 90, 1, 0, 3, 1, "2026-06-01"),
                ],
            )

    def test_script_creates_updates_and_does_not_duplicate_ratings(self):
        self.insert_sample_data()

        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        with sqlite3.connect(self.database_path) as connection:
            team_rows = connection.execute(
                """
                select team_id, matches_played, goals_for, goals_against, elo_rating
                from team_ratings order by team_id
                """
            ).fetchall()
            player_rows = connection.execute(
                """
                select
                  player_id, matches_played, minutes_played,
                  overall_rating, goal_threat
                from player_ratings order by player_id
                """
            ).fetchall()

        self.assertEqual(len(team_rows), 2)
        self.assertEqual(team_rows[0][1:4], (2, 2, 1))
        self.assertGreater(team_rows[0][4], 1500)
        self.assertEqual(len(player_rows), 2)
        self.assertEqual(player_rows[0][1:3], (2, 165))
        self.assertGreater(player_rows[0][3], 0)

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                update team_match_stats
                set goals = 1
                where team_id = 'team-a' and match_id = 'm2'
                """
            )
            connection.execute(
                """
                update player_match_stats
                set goals = 2, shots = 5
                where player_id = 'player-a' and match_id = 'm2'
                """
            )
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        with sqlite3.connect(self.database_path) as connection:
            counts = (
                connection.execute("select count(*) from team_ratings").fetchone()[0],
                connection.execute("select count(*) from player_ratings").fetchone()[0],
            )
            updated_goals = connection.execute(
                "select goal_threat from player_ratings where player_id = 'player-a'"
            ).fetchone()[0]
            updated_team_goals = connection.execute(
                "select goals_for from team_ratings where team_id = 'team-a'"
            ).fetchone()[0]

        self.assertEqual(counts, (2, 2))
        self.assertEqual(updated_team_goals, 3)
        self.assertGreater(updated_goals, player_rows[0][4])

    def test_no_data_exits_successfully(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no team or player match data yet", result.stderr)


if __name__ == "__main__":
    unittest.main()
