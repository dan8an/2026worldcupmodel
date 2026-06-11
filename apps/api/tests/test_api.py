from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.api.app import main as main_module
from apps.api.app.main import app
from apps.api.app.service import DatabasePredictionSource, PredictionService

client = TestClient(app)


class LatestV4PredictionSource:
    def load_latest(self):
        return {
            "model_run_id": "v4-run",
            "model_version": "elo-context-v4",
            "generated_at": "2026-06-11T06:00:00+00:00",
            "data_cutoff": "2026-06-11T05:59:00+00:00",
            "predictions": {
                "WC26-001": {
                    "canonical_match_id": "WC26-001",
                    "model_version": "elo-context-v4",
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
    assert payload["model_version"] == "elo-context-v4"
    assert len(payload["predictions"]) == 1
    prediction = payload["predictions"][0]
    assert prediction["match_id"] == "WC26-001"
    assert prediction["probabilities"] == {
        "home_win": 0.61,
        "draw": 0.24,
        "away_win": 0.15,
    }
    assert prediction["probabilities"] != static["probabilities"]
    assert prediction["model_version"] == "elo-context-v4"


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
    assert match["prediction"]["model_version"] == "elo-context-v4"
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
    assert len(payload["predictions"]) == 72
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
                insert into predictions values
                  ('WC26-001', 'old-run', 'context-0.2.0',
                   '2026-06-10T00:00:00+00:00',
                   '2026-06-10T00:00:00+00:00', 0.40, 0.30, 0.30),
                  ('WC26-001', 'v4-run', 'elo-context-v4',
                   '2026-06-11T00:00:00+00:00',
                   '2026-06-11T00:00:00+00:00', 0.61, 0.24, 0.15)
                """
            )
        )

    latest = DatabasePredictionSource(engine).load_latest()

    assert latest["model_run_id"] == "v4-run"
    assert latest["model_version"] == "elo-context-v4"
    assert latest["predictions"]["WC26-001"]["home_win_probability"] == 0.61
