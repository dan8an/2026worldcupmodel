import json
from datetime import date
from pathlib import Path

import pytest

from modeling.src.evaluation.artifacts import portable_path, write_json_atomic
from modeling.src.expected_goals_v44 import (
    PoissonGoalModel,
    TeamGoalHistory,
    amplify_goal_difference,
    capped_margin,
    competition_category,
    opponent_adjusted_snapshot,
    opponent_adjusted_xg,
    update_histories_batched,
    weighted_shrunk,
)
from modeling.src.features.context import HistoricalResult


def result(day, home="A", away="B", home_score=2, away_score=0):
    return HistoricalResult(date.fromisoformat(day), home, away, home_score, away_score, "Friendly", False)


def test_point_in_time_snapshot_excludes_same_day_and_future_matches():
    history = TeamGoalHistory(attack=[(date(2024, 1, 1), 0.5), (date(2024, 2, 1), 1.0)])
    value = weighted_shrunk(history.attack, date(2024, 2, 1), 180)
    assert value == pytest.approx(0.5 / 9)


def test_same_day_updates_are_batched_against_prior_state():
    histories = {}
    day = [result("2024-01-01", "A", "B"), result("2024-01-01", "A", "C", 0, 1)]
    before = opponent_adjusted_snapshot(histories, "A", date(2024, 1, 1))
    update_histories_batched(histories, day)
    assert before["long_attack"] == 0
    assert len(histories["A"].attack) == 2
    assert opponent_adjusted_snapshot(histories, "A", date(2024, 1, 1))["long_attack"] == 0


def test_small_samples_shrink_and_recent_matches_receive_more_weight():
    one = weighted_shrunk([(date(2024, 1, 1), 0.8)], date(2024, 6, 1), 180)
    many = weighted_shrunk([(date(2024, 1, day), 0.8) for day in range(1, 10)], date(2024, 6, 1), 180)
    assert 0 < one < many < 0.8
    recent = weighted_shrunk([(date(2023, 1, 1), -0.5), (date(2024, 5, 1), 0.5)], date(2024, 6, 1), 180, 0)
    assert recent > 0


def test_expected_goals_are_monotonic_bounded_and_separated():
    neutral = {key: 0.0 for key in ("long_attack", "long_defense_weakness", "short_attack", "short_defense_weakness")}
    strong = {**neutral, "long_attack": 0.6}
    base = opponent_adjusted_xg(1.35, 1.35, neutral, neutral)
    improved = opponent_adjusted_xg(1.35, 1.35, strong, neutral)
    assert improved[0] > base[0]
    separated = amplify_goal_difference(*improved, 1.15)
    assert separated[0] / separated[1] > improved[0] / improved[1]
    assert all(0.2 <= value <= 4.5 for value in separated)


def test_context_and_margin_are_bounded():
    assert competition_category("FIFA World Cup qualification") == "qualification"
    assert competition_category("UEFA Nations League") == "nations_league"
    assert competition_category("Friendly") == "friendly"
    assert abs(capped_margin(7, 0)) <= 2


def test_poisson_coefficient_round_trip_and_determinism():
    model = PoissonGoalModel((0.2, 0.3), ("intercept", "strength"), 1.0)
    loaded = PoissonGoalModel.from_dict(model.to_dict())
    assert loaded == model
    assert loaded.predict((1.0, 0.5)) == model.predict((1.0, 0.5))


def test_portable_paths_accept_relative_and_absolute(tmp_path: Path):
    relative = Path("artifact.json")
    assert portable_path(relative, Path.cwd()).endswith("artifact.json")
    inside = tmp_path / "inside.json"
    assert portable_path(inside, tmp_path) == "inside.json"


def test_atomic_json_persistence_leaves_no_temporary_file(tmp_path: Path):
    path = tmp_path / "raw.json"
    write_json_atomic(path, {"path": path, "tuple": (1, 2), "set": {3}})
    assert json.loads(path.read_text())["tuple"] == [1, 2]
    assert not path.with_name("raw.json.tmp").exists()
