from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.backfill_historical_knockout_predictions import (
    build_historical_state,
    is_authentic_prediction,
    target_matches,
)
from scripts.generate_predictions import MODEL_VERSION, calculate_prediction


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


TEAMS = [
    {"id": "A", "name": "Alpha", "fifa_rank": 10},
    {"id": "B", "name": "Beta", "fifa_rank": 20},
]


def match(match_id: str, kickoff: str, **extra):
    return {
        "id": match_id,
        "kickoff": kickoff,
        "status": "completed",
        "home_team_id": "A",
        "away_team_id": "B",
        "home_score": 1,
        "away_score": 0,
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
        **extra,
    }


def stats(match_id: str, captured: str, goals=(1, 0)):
    common = {
        "match_id": match_id,
        "captured_at": captured,
        "created_at": captured,
        "shots": 10,
        "shots_on_target": 4,
        "shots_inside_box": 6,
        "shots_outside_box": 4,
        "blocked_shots": 2,
        "goalkeeper_saves": 3,
        "corners": 5,
        "possession": 50,
        "passes_attempted": 400,
        "passes_completed": 320,
        "pass_accuracy": 80,
    }
    return [
        {**common, "id": f"{match_id}-a", "team_id": "A", "goals": goals[0]},
        {**common, "id": f"{match_id}-b", "team_id": "B", "goals": goals[1]},
    ]


def state(matches, team_stats, cutoff="2026-07-01T12:00:00Z", target="target"):
    return build_historical_state(
        teams=TEAMS,
        matches=matches,
        team_stats=team_stats,
        player_stats=[],
        cutoff=dt(cutoff),
        target_match_id=target,
    )


def test_target_result_and_later_matches_are_excluded():
    matches = [
        match("prior", "2026-06-29T12:00:00Z"),
        match("target", "2026-07-01T12:00:01Z", home_score=9),
        match("later", "2026-07-02T12:00:00Z"),
    ]
    result = state(matches, stats("prior", "2026-06-29T15:00:00Z") + stats("target", "2026-07-01T15:00:00Z") + stats("later", "2026-07-02T15:00:00Z"))
    assert result.completed_match_count == 1
    assert result.team_stat_count == 2
    assert result.team_ratings["A"]["matches_played"] == 1


def test_rows_not_available_at_cutoff_are_excluded():
    matches = [match("prior", "2026-06-29T12:00:00Z")]
    result = state(matches, stats("prior", "2026-07-01T12:00:00Z"))
    assert result.team_stat_count == 0
    assert result.team_ratings["A"]["_rating_source"] == "rank_prior"


def test_current_rating_tables_are_not_inputs(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("current ratings must not be loaded")

    monkeypatch.setattr(
        "scripts.generate_predictions.PredictionRepository.load_current_team_ratings",
        fail,
    )
    result = state([match("prior", "2026-06-29T12:00:00Z")], stats("prior", "2026-06-29T15:00:00Z"))
    assert result.team_ratings["A"]["_rating_source"] == "historical_rebuild"


def test_walk_forward_includes_previous_fixture_only_after_it_is_available():
    matches = [match("r32", "2026-06-29T12:00:00Z"), match("r16", "2026-07-03T12:00:00Z")]
    rows = stats("r32", "2026-06-29T15:00:00Z")
    before = state(matches, rows, cutoff="2026-06-29T11:59:59Z", target="r32")
    after = state(matches, rows, cutoff="2026-07-03T11:59:59Z", target="r16")
    assert before.completed_match_count == 0
    assert after.completed_match_count == 1
    assert after.team_ratings["A"]["matches_played"] == 1


def test_authentic_prediction_prevents_target_but_backfill_does_not():
    fixture = match("ko", "2026-07-01T12:00:00Z", stage="round_of_32", match_number=73)
    authentic = {"match_id": "ko", "prediction_timestamp": "2026-07-01T11:00:00Z", "generation_mode": "standard"}
    backfill = {"match_id": "ko", "prediction_timestamp": "2026-07-10T00:00:00Z", "generation_mode": "historical_backfill", "historical_cutoff": "2026-07-01T11:59:59Z"}
    assert is_authentic_prediction(authentic, dt(fixture["kickoff"]))
    assert not is_authentic_prediction(backfill, dt(fixture["kickoff"]))
    assert target_matches([fixture], [authentic])[0]["_authentic"]
    assert not target_matches([fixture], [backfill])[0]["_authentic"]


def test_probability_is_normalized_deterministic_and_model_version_fixed():
    historical = state([match("prior", "2026-06-29T12:00:00Z")], stats("prior", "2026-06-29T15:00:00Z"))
    args = (historical.team_ratings["A"], historical.team_ratings["B"])
    first = calculate_prediction(*args, home_shot_volume_rating=historical.shot_volume_ratings["A"], away_shot_volume_rating=historical.shot_volume_ratings["B"])
    second = calculate_prediction(*args, home_shot_volume_rating=historical.shot_volume_ratings["A"], away_shot_volume_rating=historical.shot_volume_ratings["B"])
    triple = [first[name] for name in ("home_win_probability", "draw_probability", "away_win_probability")]
    assert sum(triple) == pytest.approx(1.0)
    assert first == second
    assert MODEL_VERSION == "elo-context-v4.2.1"


def test_penalty_and_aet_target_fields_do_not_change_inputs():
    prior = match("prior", "2026-06-29T12:00:00Z")
    rows = stats("prior", "2026-06-29T15:00:00Z")
    plain = match("target", "2026-07-01T12:00:01Z", status="ft")
    penalties = match("target", "2026-07-01T12:00:01Z", status="pen", home_score=8, away_score=7, penalty_home=6, penalty_away=5)
    assert state([prior, plain], rows) == state([prior, penalties], rows)
