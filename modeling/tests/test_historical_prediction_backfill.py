from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import (
    JSON, CheckConstraint, Column, Float, Integer, MetaData, String,
    Table, create_engine, func, select,
)

from scripts.backfill_historical_knockout_predictions import (
    build_historical_state,
    is_authentic_prediction,
    json_safe,
    main,
    HistoricalBackfillRepository,
    resolve_knockout_identity,
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


def official_provider_fields():
    return {
        "provider_name": "api_football",
        "provider_payload": {"league": {"id": 1, "season": 2026}},
    }


def test_identity_prefers_valid_canonical_match_id():
    identity = resolve_knockout_identity(
        {"id": "db-1", "canonical_match_id": "WC26-089", "match_number": 90},
        "round_of_16",
    )
    assert identity.canonical_match_id == "WC26-089"
    assert identity.official_match_number == 89
    assert identity.stable_key == ("match", "db-1")


def test_identity_uses_official_match_number_when_present():
    identity = resolve_knockout_identity(
        {"id": "db-2", "match_number": "73"}, "round_of_32"
    )
    assert identity.canonical_match_id == "WC26-073"
    assert identity.official_match_number == 73


def test_provider_only_official_identity_does_not_fabricate_canonical_id():
    identity = resolve_knockout_identity(
        {"api_football_fixture_id": 99089, **official_provider_fields()},
        "round_of_16",
    )
    assert identity.canonical_match_id is None
    assert identity.provider_fixture_id == 99089
    assert identity.stable_key == ("provider", "99089")


class FakeBackfillRepository:
    def __init__(self, fixture):
        self.fixture = fixture
        self.stored = []

    def assert_schema(self, apply=False):
        self.apply_checked = apply

    def rows(self, name):
        return {
            "matches": [self.fixture],
            "teams": TEAMS,
            "predictions": [],
        }[name]

    def load_stats(self, _name):
        return []

    def store(self, payload, generated_at):
        self.stored.append((payload, generated_at))
        return "run"


@pytest.mark.parametrize("apply", [False, True])
def test_dry_run_and_apply_complete_for_provider_only_identity(monkeypatch, apply):
    fixture = match(
        "db-provider", "2026-07-04T20:00:00Z", stage="round_of_16",
        match_number=None, canonical_match_id=None,
        api_football_fixture_id=99089, **official_provider_fields(),
    )
    repository = FakeBackfillRepository(fixture)
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.load_environment",
        lambda: {"DATABASE_URL": "sqlite://"},
    )
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.create_database_engine",
        lambda _url: object(),
    )
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.HistoricalBackfillRepository",
        lambda _engine: repository,
    )
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.map_database_team_ids",
        lambda _teams: {"USA": "A", "MEX": "B"},
    )
    assert main(["--apply"] if apply else []) == 0
    assert len(repository.stored) == int(apply)
    if apply:
        payload = repository.stored[0][0]
        assert payload["canonical_match_id"] is None
        assert payload["provider_fixture_id"] == 99089
        assert payload["match_id"] == "db-provider"


def test_all_identities_missing_skips_clearly_without_crashing(monkeypatch, caplog):
    fixture = match(
        None, "2026-07-04T20:00:00Z", stage="round_of_16",
        match_number=None, canonical_match_id=None,
        **official_provider_fields(),
    )
    repository = FakeBackfillRepository(fixture)
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.load_environment",
        lambda: {"DATABASE_URL": "sqlite://"},
    )
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.create_database_engine",
        lambda _url: object(),
    )
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.HistoricalBackfillRepository",
        lambda _engine: repository,
    )
    monkeypatch.setattr(
        "scripts.backfill_historical_knockout_predictions.map_database_team_ids",
        lambda _teams: {"USA": "A", "MEX": "B"},
    )
    with caplog.at_level("WARNING"):
        assert main([]) == 0
    assert not repository.stored
    assert "reason=no_stable_fixture_identity" in caplog.text
    assert "teams=USA_vs_MEX" in caplog.text
    assert "kickoff=2026-07-04T20:00:00+00:00" in caplog.text


class ExampleMode(Enum):
    BACKFILL = "historical_backfill"


def test_json_safe_recursively_converts_uuid_datetime_and_supported_types():
    identifier = uuid4()
    occurred_at = datetime(2026, 7, 4, 20, tzinfo=timezone.utc)
    value = {
        "metadata": {
            "fixture": identifier,
            "nested": [occurred_at, date(2026, 7, 4), Decimal("1.25")],
            "mode": ExampleMode.BACKFILL,
            "path": Path("reports/backfill.json"),
            "set": {identifier},
        }
    }
    safe = json_safe(value)
    assert safe["metadata"]["fixture"] == str(identifier)
    assert safe["metadata"]["nested"] == [
        "2026-07-04T20:00:00+00:00", "2026-07-04", 1.25,
    ]
    assert safe["metadata"]["mode"] == "historical_backfill"
    assert safe["metadata"]["path"] == "reports/backfill.json"
    assert safe["metadata"]["set"] == [str(identifier)]


def repository_schema():
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table(
        "model_runs", metadata,
        Column("id", String, primary_key=True),
        Column("run_date", String),
        Column("model_version", String),
        Column("notes", String),
        Column("data_cutoff", String),
        Column("status", String),
        Column("random_seed", Integer),
        Column("generated_at", String),
        Column("metadata", JSON),
    )
    Table(
        "predictions", metadata,
        Column("id", String, primary_key=True),
        Column("model_run_id", String, nullable=False),
        Column("match_id", String),
        Column("provider_fixture_id", Integer),
        Column("canonical_match_id", String),
        Column("model_version", String),
        Column("generation_mode", String),
        Column("historical_cutoff", String),
        Column("backfilled_at", String),
        Column("prediction_timestamp", String),
        Column("created_at", String),
        Column("updated_at", String),
        Column("data_cutoff", String),
        Column("home_win_probability", Float),
        Column("draw_probability", Float),
        Column("away_win_probability", Float),
        Column("provenance", JSON),
        CheckConstraint("home_win_probability >= 0", name="valid_home_probability"),
    )
    metadata.create_all(engine)
    return engine


def stored_prediction(match_id: str, home_probability=0.5):
    provenance_uuid = uuid4()
    return {
        "database_match_id": match_id,
        "match_id": match_id,
        "provider_fixture_id": 99089,
        "canonical_match_id": None,
        "historical_cutoff": "2026-07-04T19:59:59+00:00",
        "home_win_probability": home_probability,
        "draw_probability": 0.25,
        "away_win_probability": 0.25,
        "provenance": {
            "fixture_uuid": provenance_uuid,
            "generated_for": datetime(2026, 7, 4, 20, tzinfo=timezone.utc),
        },
        "_expected_provenance_uuid": provenance_uuid,
    }


def table_count(engine, name):
    table = Table(name, MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return connection.execute(select(func.count()).select_from(table)).scalar_one()


def test_store_sanitizes_nested_run_metadata_and_prediction_provenance():
    engine = repository_schema()
    repository = HistoricalBackfillRepository(engine)
    match_id = str(uuid4())
    payload = stored_prediction(match_id)
    expected_provenance_uuid = payload.pop("_expected_provenance_uuid")
    nested_run_uuid = uuid4()
    payload["run_metadata"] = {
        "nested": {"fixture_uuid": nested_run_uuid},
        "prepared_at": datetime(2026, 7, 10, 11, tzinfo=timezone.utc),
    }
    run_id = repository.store(
        payload,
        datetime(2026, 7, 10, 12, tzinfo=timezone.utc),
    )
    assert run_id != "skipped_existing_backfill"
    assert table_count(engine, "model_runs") == 1
    assert table_count(engine, "predictions") == 1

    runs = repository.table("model_runs")
    predictions = repository.table("predictions")
    with engine.connect() as connection:
        metadata = connection.execute(select(runs.c.metadata)).scalar_one()
        provenance = connection.execute(select(predictions.c.provenance)).scalar_one()
    assert metadata["database_match_id"] == match_id
    assert metadata["provenance"] == {
        "nested": {"fixture_uuid": str(nested_run_uuid)},
        "prepared_at": "2026-07-10T11:00:00+00:00",
    }
    assert provenance == {
        "fixture_uuid": str(expected_provenance_uuid),
        "generated_for": "2026-07-04T20:00:00+00:00",
    }


def test_store_rolls_back_prediction_failure_then_reruns_idempotently():
    engine = repository_schema()
    repository = HistoricalBackfillRepository(engine)
    match_id = str(uuid4())
    generated_at = datetime(2026, 7, 10, 12, tzinfo=timezone.utc)

    with pytest.raises(Exception):
        failed = stored_prediction(match_id, home_probability=-1)
        failed.pop("_expected_provenance_uuid")
        repository.store(failed, generated_at)
    assert table_count(engine, "model_runs") == 0
    assert table_count(engine, "predictions") == 0

    successful = stored_prediction(match_id)
    successful.pop("_expected_provenance_uuid")
    run_id = repository.store(successful, generated_at)
    assert run_id != "skipped_existing_backfill"
    assert table_count(engine, "model_runs") == 1
    assert table_count(engine, "predictions") == 1

    duplicate = stored_prediction(match_id)
    duplicate.pop("_expected_provenance_uuid")
    assert repository.store(duplicate, generated_at) == "skipped_existing_backfill"
    assert table_count(engine, "model_runs") == 1
    assert table_count(engine, "predictions") == 1
