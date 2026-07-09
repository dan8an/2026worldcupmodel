from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.api.app import main as main_module
from apps.api.app.main import app
from apps.api.app.service import (
    DatabaseMatchResultSource,
    DatabasePredictionSource,
    DatabaseSimulationSource,
    PredictionService,
)
from modeling.src.data import load_teams

client = TestClient(app)


class LatestV4PredictionSource:
    def load_latest(self):
        return {
            "model_run_id": "v4-run",
            "model_version": "elo-context-v4.1",
            "generated_at": "2026-06-11T06:00:00+00:00",
            "data_cutoff": "2026-06-11T05:59:00+00:00",
            "source": "database_latest",
            "predictions": {
                "WC26-001": {
                    "canonical_match_id": "WC26-001",
                    "model_version": "elo-context-v4.1",
                    "home_win_probability": 0.61,
                    "draw_probability": 0.24,
                    "away_win_probability": 0.15,
                    "final_home_probability": 0.61,
                    "final_draw_probability": 0.24,
                    "final_away_probability": 0.15,
                    "elo_base_home_probability": 0.55,
                    "elo_base_draw_probability": 0.27,
                    "elo_base_away_probability": 0.18,
                    "attack_defense_adjustment": 0.02,
                    "draw_calibration_adjustment": 0.01,
                    "context_adjustment_total": 0.03,
                    "confidence_score": 67.4,
                    "confidence_tier": "Medium",
                    "confidence_explanation": (
                        "Medium confidence because the leading outcome is separated."
                    ),
                    "top_factors": [
                        {
                            "factor": "Shot volume",
                            "team": "Mexico",
                            "impact": "+1.4%",
                        }
                    ],
                }
            },
        }


def use_v4_database_service(monkeypatch):
    service = PredictionService(
        prediction_source=LatestV4PredictionSource(),
        prediction_cache_seconds=0,
    )
    monkeypatch.setattr(main_module, "service", service)
    return service


class LatestV4SimulationSource:
    def load_latest(self):
        model_inputs = {
            team.id: {
                "elo_rating": 2100.0 if team.id == "ARG" else 1500.0,
                "elo_rank": 1 if team.id == "ARG" else 20,
                "attack_rating": 82.5,
                "defense_rating": 79.25,
                "shot_volume_rating": 96.0 if team.id == "ARG" else 50.0,
                "rating_source": "database_current",
                "rating_matches": 12,
                "shot_volume_sample_matches": 10,
            }
            for team in load_teams()
        }
        return {
            "run": {
                "id": "v4-simulation",
                "model_version": "elo-context-v4.1",
                "num_simulations": 50000,
                "random_seed": 2026,
                "created_at": "2026-06-11T06:05:00+00:00",
            },
            "results": [
                {
                    "simulation_run_id": "v4-simulation",
                    "team_id": team.id,
                    "group_stage_exit_probability": 0.25,
                    "round_of_32_probability": 0.75,
                    "round_of_16_probability": 0.5,
                    "quarterfinal_probability": 0.3,
                    "semifinal_probability": 0.2,
                    "final_probability": 0.1,
                    "champion_probability": 0.314 if team.id == "ARG" else 0.01,
                }
                for team in load_teams()
            ],
            "model_inputs": model_inputs,
        }


def use_v4_simulation_service(monkeypatch):
    service = PredictionService(
        prediction_source=LatestV4PredictionSource(),
        simulation_source=LatestV4SimulationSource(),
        prediction_cache_seconds=0,
    )
    monkeypatch.setattr(main_module, "service", service)
    return service


def test_production_cors_preflight_for_v1_endpoint():
    response = client.options(
        "/v1/simulations/custom",
        headers={
            "Origin": "https://footballoracle.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )

    assert response.status_code == 200
    assert (
        response.headers["access-control-allow-origin"]
        == "https://footballoracle.vercel.app"
    )
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_production_cors_preflight_for_api_compatibility_endpoints():
    for path in (
        "/api/teams",
        "/api/matches?stage=group",
        "/api/simulations/latest",
    ):
        response = client.options(
            path,
            headers={
                "Origin": "https://footballoracle.vercel.app",
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.status_code in (200, 204)
        assert (
            response.headers["access-control-allow-origin"]
            == "https://footballoracle.vercel.app"
        )


def test_vercel_preview_cors_preflight():
    response = client.options(
        "/api/teams",
        headers={
            "Origin": "https://footballoracle-git-preview.vercel.app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code in (200, 204)
    assert (
        response.headers["access-control-allow-origin"]
        == "https://footballoracle-git-preview.vercel.app"
    )


def test_v1_teams_get_allows_production_origin():
    response = client.get(
        "/v1/teams",
        headers={"Origin": "https://footballoracle.vercel.app"},
    )

    assert response.status_code == 200
    assert response.json()
    assert (
        response.headers["access-control-allow-origin"]
        == "https://footballoracle.vercel.app"
    )


def test_api_compatibility_get_routes():
    teams_response = client.get("/api/teams")
    matches_response = client.get("/api/matches?stage=group")
    simulation_response = client.get("/api/simulations/latest")

    assert teams_response.status_code == 200
    assert teams_response.json()
    assert matches_response.status_code == 200
    assert matches_response.json()
    assert simulation_response.status_code == 200
    assert len(simulation_response.json()["teams"]) == 48


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tournament_shape():
    response = client.get("/v1/tournament")
    assert response.status_code == 200
    assert response.json()["team_count"] == 48
    assert response.json()["match_count"] == 104


def test_teams_return_non_empty_names():
    response = client.get("/v1/teams")
    teams = response.json()

    assert response.status_code == 200
    assert teams
    assert all(team["id"] and team["name"] for team in teams)


def test_latest_simulation_returns_team_names():
    response = client.get("/v1/simulations/latest")
    teams = response.json()["teams"]

    assert response.status_code == 200
    assert len(teams) == 48
    assert all(team["team_id"] and team["team_name"] for team in teams)


def test_match_prediction_probabilities():
    response = client.get("/v1/matches/WC26-001")
    payload = response.json()
    probabilities = payload["prediction"]["probabilities"]
    assert abs(sum(probabilities.values()) - 1) < 0.00001


def test_group_matches_are_chronological():
    response = client.get("/v1/matches?stage=group")
    assert response.status_code == 200
    matches = response.json()
    ordering = [(match["kickoff"], match["number"]) for match in matches]
    assert ordering == sorted(ordering)
    assert [match["group"] for match in matches[:5]] == ["A", "A", "B", "D", "B"]


def test_api_exposes_full_104_match_tournament_catalog():
    response = client.get("/v1/matches")
    matches = response.json()

    assert response.status_code == 200
    assert len(matches) == 104
    assert sum(match["stage"] == "group" for match in matches) == 72
    assert any(match["stage"] == "round_of_32" for match in matches)


def test_model_performance_is_published():
    response = client.get("/v1/model/performance")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "evaluated"
    assert payload["aggregate"]["elo"]["matches"] > 0
    assert payload["promotion_gate"]["status"] in ("pass", "fail")


def test_team_profile_includes_players_form_and_grounded_analysis():
    response = client.get("/v1/teams/USA")
    assert response.status_code == 200
    payload = response.json()
    assert payload["flag"] == "🇺🇸"
    assert len(payload["key_players"]) == 4
    assert len(payload["recent_results"]) == 8
    assert len(payload["group_path"]) == 3
    assert payload["analysis"]["overview"]
    assert payload["analysis"]["method"].startswith("Structured")
    assert payload["player_data_source"]["team_count"] == 48
    result_dates = [result["played_on"] for result in payload["recent_results"]]
    assert result_dates == sorted(result_dates, reverse=True)
    for match in payload["group_path"]:
        assert abs(
            match["team_win_probability"]
            + match["draw_probability"]
            + match["opponent_win_probability"]
            - 1
        ) < 0.00001


def test_match_teams_include_flags():
    response = client.get("/v1/matches/WC26-001")
    payload = response.json()
    assert payload["home_team"]["flag"] == "🇲🇽"
    assert payload["away_team"]["flag"] == "🇿🇦"
    assert payload["status"] == "scheduled"
    assert payload["home_score"] is None
    assert payload["away_score"] is None


def _completed_group_results(service):
    return {
        match.id: {
            "status": "completed",
            "home_score": 2,
            "away_score": 0,
        }
        for match in service.fixtures
        if match.stage == "group"
    }


def test_completed_group_stage_resolves_round_of_32_teams():
    service = PredictionService(match_result_source=None)
    results = _completed_group_results(service)

    payload = service.match_payload("WC26-073", match_results=results)

    assert payload["home_team"]["name"]
    assert payload["home_team"]["flag"]
    assert payload["away_team"]["id"].isalpha()
    assert payload["home_slot"] is None
    assert payload["away_slot"] is None


def test_partial_group_stage_resolves_known_team_and_keeps_friendly_unknown_slot():
    service = PredictionService(match_result_source=None)
    results = {
        match.id: {"status": "completed", "home_score": 1, "away_score": 0}
        for match in service.fixtures
        if match.stage == "group" and match.group == "A"
    }

    payload = service.match_payload("WC26-073", match_results=results)

    assert payload["home_team"]["name"]
    assert payload["home_slot"] is None
    assert payload["away_team"] is None
    assert payload["away_slot"] == "Round of 32 away qualifier 1"
    assert not payload["away_slot"].isdigit()


def test_later_knockout_round_inherits_completed_match_winner():
    service = PredictionService(match_result_source=None)
    results = _completed_group_results(service)
    home, away = service.resolve_match_participants(results)["WC26-073"]
    results["WC26-073"] = {
        "status": "completed",
        "home_score": 3,
        "away_score": 1,
        "home_team_id": home,
        "away_team_id": away,
    }

    payload = service.match_payload("WC26-089", match_results=results)

    assert payload["home_team"]["id"] == home
    assert payload["home_slot"] is None
    assert payload["away_team"] is None
    assert payload["away_slot"] == "Winner Round of 32 Match 2"


def test_team_match_filter_includes_resolved_knockout_fixtures(monkeypatch):
    service = PredictionService(match_result_source=None, prediction_cache_seconds=0)
    results = _completed_group_results(service)
    resolved_home, _ = service.resolve_match_participants(results)["WC26-073"]
    monkeypatch.setattr(main_module, "service", service)
    monkeypatch.setattr(service, "current_match_results", lambda: results)

    response = client.get(f"/v1/matches?team_id={resolved_home}")
    payload = response.json()

    assert response.status_code == 200
    assert any(match["id"] == "WC26-073" for match in payload)
    knockout = next(match for match in payload if match["id"] == "WC26-073")
    assert resolved_home in (
        knockout["home_team"]["id"],
        knockout["away_team"]["id"],
    )
    assert knockout["home_slot"] is None or knockout["away_slot"] is None


def test_completed_match_results_are_merged_into_canonical_fixtures(monkeypatch):
    class MatchResultSource:
        def load(self):
            return [
                {
                    "match_date": "2026-06-11T17:00:00+00:00",
                    "home_team_name": "Mexico",
                    "away_team_name": "South Africa",
                    "status": "finished",
                    "home_score": 2,
                    "away_score": 1,
                }
            ]

    service = PredictionService(
        prediction_source=LatestV4PredictionSource(),
        match_result_source=MatchResultSource(),
        prediction_cache_seconds=0,
    )
    monkeypatch.setattr(main_module, "service", service)

    payload = client.get("/v1/matches/WC26-001").json()

    assert payload["status"] == "finished"
    assert payload["home_score"] == 2
    assert payload["away_score"] == 1
    assert payload["prediction"]["model_version"] == "elo-context-v4.1"


def test_late_night_result_matches_canonical_fixture_across_utc_date(monkeypatch):
    class MatchResultSource:
        def load(self):
            return [
                {
                    "match_date": "2026-06-13T01:00:00+00:00",
                    "home_team_name": "USA",
                    "away_team_name": "Paraguay",
                    "completed": True,
                    "home_score": 4,
                    "away_score": 1,
                }
            ]

    service = PredictionService(
        prediction_source=LatestV4PredictionSource(),
        match_result_source=MatchResultSource(),
        prediction_cache_seconds=0,
    )
    monkeypatch.setattr(main_module, "service", service)

    matches = client.get("/v1/matches?stage=group").json()
    payload = next(match for match in matches if match["id"] == "WC26-019")

    assert payload["status"] == "completed"
    assert payload["home_score"] == 4
    assert payload["away_score"] == 1


def test_database_match_result_source_loads_team_names():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("create table teams (id text primary key, name text)")
        )
        connection.execute(
            text(
                """
                create table matches (
                  id text primary key,
                  match_date text,
                  home_team_id text,
                  away_team_id text,
                  home_score integer,
                  away_score integer,
                  completed boolean
                )
                """
            )
        )
        connection.execute(
            text(
                """
                insert into teams values
                  ('mex', 'Mexico'),
                  ('rsa', 'South Africa')
                """
            )
        )
        connection.execute(
            text(
                """
                insert into matches values
                  ('match-1', '2026-06-11T17:00:00+00:00',
                   'mex', 'rsa', 2, 1, true)
                """
            )
        )

    [row] = DatabaseMatchResultSource(engine).load()

    assert row["home_team_name"] == "Mexico"
    assert row["away_team_name"] == "South Africa"
    assert row["home_score"] == 2


def test_latest_database_prediction_run_wins_over_static(monkeypatch):
    service = use_v4_database_service(monkeypatch)
    static = service.prediction_payload(
        "WC26-001",
        {
            "model_run_id": None,
            "model_version": "context-0.2.0",
            "generated_at": service.generated_at,
            "data_cutoff": service.data_cutoff,
            "predictions": service.static_predictions,
        },
    )

    response = client.get("/v1/predictions/latest")
    payload = response.json()

    assert response.status_code == 200
    assert payload["model_version"] == "elo-context-v4.1"
    assert payload["source"] == "database_latest"
    assert len(payload["predictions"]) == 1
    prediction = payload["predictions"][0]
    assert prediction["match_id"] == "WC26-001"
    assert prediction["probabilities"] == {
        "home_win": 0.61,
        "draw": 0.24,
        "away_win": 0.15,
    }
    assert prediction["probabilities"] != static["probabilities"]
    assert prediction["model_version"] == "elo-context-v4.1"
    assert prediction["source"] == "database_latest"


def test_latest_database_simulation_wins_over_static(monkeypatch):
    use_v4_simulation_service(monkeypatch)

    for path in ("/v1/simulations/latest", "/api/simulations/latest"):
        response = client.get(path)
        payload = response.json()

        assert response.status_code == 200
        assert payload["model_version"] == "elo-context-v4.1"
        assert payload["generated_at"] == "2026-06-11T06:05:00+00:00"
        assert payload["created_at"] == "2026-06-11T06:05:00+00:00"
        assert payload["source"] == "database_latest"
        argentina = next(
            team for team in payload["teams"] if team["team_id"] == "ARG"
        )
        assert argentina["team_name"] == "Argentina"
        assert argentina["champion"] == 0.314
        assert argentina["model_inputs"] == {
            "elo_rating": 2100.0,
            "elo_rank": 1,
            "attack_rating": 82.5,
            "defense_rating": 79.25,
            "shot_volume_rating": 96.0,
            "rating_source": "database_current",
            "rating_matches": 12,
            "shot_volume_sample_matches": 10,
        }


def test_database_simulation_failure_uses_labeled_static_fallback():
    class FailingSimulationSource:
        def load_latest(self):
            raise RuntimeError("database unavailable")

    service = PredictionService(
        prediction_source=LatestV4PredictionSource(),
        simulation_source=FailingSimulationSource(),
        prediction_cache_seconds=0,
    )

    payload = service.latest_simulation()

    assert payload["source"] == "fallback_static"
    assert payload["model_version"] == "context-0.2.0"
    assert payload["created_at"] == payload["generated_at"]
    assert len(payload["teams"]) == 48


def test_api_matches_use_v4_probabilities_and_canonical_fixture(monkeypatch):
    use_v4_database_service(monkeypatch)

    response = client.get("/api/matches?stage=group")
    matches = response.json()
    match = next(item for item in matches if item["id"] == "WC26-001")

    assert response.status_code == 200
    assert match["id"] == "WC26-001"
    assert match["number"] == 1
    assert match["group"] == "A"
    assert match["home_team"]["id"] == "MEX"
    assert match["away_team"]["id"] == "RSA"
    assert match["prediction"]["model_version"] == "elo-context-v4.1"
    assert match["prediction"]["source"] == "database_latest"
    assert match["prediction"]["final_home_probability"] == 0.61
    assert match["prediction"]["top_factors"][0]["factor"] == "Shot volume"

    detail_response = client.get("/api/matches/WC26-001")
    assert detail_response.status_code == 200
    assert detail_response.json()["id"] == "WC26-001"


def test_database_failure_falls_back_to_static_with_warning(caplog):
    class FailingPredictionSource:
        def load_latest(self):
            raise RuntimeError("database unavailable")

    service = PredictionService(
        prediction_source=FailingPredictionSource(),
        prediction_cache_seconds=0,
    )

    with caplog.at_level("WARNING"):
        payload = service.latest_predictions_payload(force=True)

    assert payload["model_version"] == "context-0.2.0"
    assert payload["source"] == "fallback_static"
    assert len(payload["predictions"]) == 72
    assert all(
        prediction["source"] == "fallback_static"
        for prediction in payload["predictions"]
    )
    assert "serving static prediction fallback" in caplog.text


def test_database_source_selects_newest_prediction_run():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                create table predictions (
                  canonical_match_id text,
                  model_run_id text,
                  model_version text,
                  prediction_timestamp text,
                  data_cutoff text,
                  home_win_probability real,
                  draw_probability real,
                  away_win_probability real
                )
                """
            )
        )
        connection.execute(
            text(
                """
                create table model_runs (
                  id text primary key,
                  model_version text,
                  status text,
                  generated_at text,
                  data_cutoff text
                )
                """
            )
        )
        connection.execute(
            text(
                """
                insert into model_runs values
                  ('old-run', 'context-0.2.0', 'completed',
                   '2026-06-10T00:00:00+00:00',
                   '2026-06-10T00:00:00+00:00'),
                  ('v4-run', 'elo-context-v4.1', 'completed',
                   '2026-06-11T00:00:00+00:00',
                   '2026-06-11T00:00:00+00:00'),
                  ('failed-run', 'elo-context-v5', 'failed',
                   '2026-06-12T00:00:00+00:00',
                   '2026-06-12T00:00:00+00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                insert into predictions values
                  ('WC26-001', 'old-run', 'context-0.2.0',
                   '2026-06-10T00:00:00+00:00',
                   '2026-06-10T00:00:00+00:00', 0.40, 0.30, 0.30),
                  ('WC26-001', 'v4-run', 'elo-context-v4.1',
                   '2026-06-11T00:00:00+00:00',
                   '2026-06-11T00:00:00+00:00', 0.61, 0.24, 0.15)
                """
            )
        )

    latest = DatabasePredictionSource(engine).load_latest()

    assert latest["model_run_id"] == "v4-run"
    assert latest["model_version"] == "elo-context-v4.1"
    assert latest["predictions"]["WC26-001"]["home_win_probability"] == 0.61


def test_database_source_selects_newest_simulation_run_and_team_results():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                create table simulation_runs (
                  id text primary key,
                  model_version text,
                  num_simulations integer,
                  random_seed integer,
                  created_at text
                )
                """
            )
        )
        connection.execute(
            text(
                """
                create table team_simulation_results (
                  simulation_run_id text,
                  team_id text,
                  champion_probability real
                )
                """
            )
        )
        connection.execute(
            text(
                """
                insert into simulation_runs values
                  ('old-simulation', 'context-0.2.0', 1000, 2026,
                   '2026-06-10T00:46:29+00:00'),
                  ('v4-simulation', 'elo-context-v4.1', 50000, 2026,
                   '2026-06-11T06:05:00+00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                insert into team_simulation_results values
                  ('old-simulation', 'ARG', 0.11),
                  ('v4-simulation', 'ARG', 0.314)
                """
            )
        )

    latest = DatabaseSimulationSource(engine).load_latest()

    assert latest["run"]["id"] == "v4-simulation"
    assert latest["run"]["model_version"] == "elo-context-v4.1"
    assert latest["results"] == [
        {
            "simulation_run_id": "v4-simulation",
            "team_id": "ARG",
            "champion_probability": 0.314,
        }
    ]
