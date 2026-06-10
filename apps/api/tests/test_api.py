from fastapi.testclient import TestClient

from apps.api.app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tournament_shape():
    response = client.get("/v1/tournament")
    assert response.status_code == 200
    assert response.json()["team_count"] == 48
    assert response.json()["match_count"] == 104


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
