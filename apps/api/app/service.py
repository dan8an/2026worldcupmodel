import json
from dataclasses import asdict
from datetime import datetime, timezone

from modeling.src.data import ROOT, build_fixtures, load_teams, load_venues, validate_tournament
from modeling.src.features.context import ContextRepository
from modeling.src.flags import flag_for_team
from modeling.src.poisson import predict_match
from modeling.src.simulation import (
    PUBLISHED_SIMULATION_ITERATIONS,
    prediction_dict,
    simulate_tournament,
)
from modeling.src.team_profiles import (
    form_summary,
    key_players,
    load_squad_metadata,
    load_squad_players,
    player_payload,
    recent_results,
    team_analysis,
)

MODEL_VERSION = "context-0.2.0"


class PredictionService:
    def __init__(self) -> None:
        self.generated_at = datetime.now(timezone.utc).isoformat()
        self.data_cutoff = self.generated_at
        self.teams = load_teams()
        self.teams_by_id = {team.id: team for team in self.teams}
        self.contexts = ContextRepository()
        self.fixtures = build_fixtures(self.teams)
        validate_tournament(self.teams, self.fixtures)
        self.venues = load_venues()
        self.squad_players = load_squad_players()
        self.squad_metadata = load_squad_metadata()
        self.predictions = {
            match.id: predict_match(
                self.teams_by_id[match.home_team_id or ""],
                self.teams_by_id[match.away_team_id or ""],
                match.id,
                context=self.contexts.for_match(
                    match.home_team_id or "",
                    match.away_team_id or "",
                    datetime.fromisoformat(self.data_cutoff),
                ),
            )
            for match in self.fixtures
            if match.stage == "group"
        }
        self._simulation: dict | None = None
        self._simulation_generated_at = self.generated_at
        self._simulation_data_cutoff = self.data_cutoff

    def team_payload(self, team_id: str) -> dict:
        team = self.teams_by_id[team_id]
        return {
            **asdict(team),
            "elo": round(team.elo, 1),
            "flag": flag_for_team(team_id),
        }

    def prediction_payload(self, match_id: str) -> dict | None:
        prediction = self.predictions.get(match_id)
        if not prediction:
            return None
        return {
            **prediction_dict(prediction),
            "model_version": MODEL_VERSION,
            "generated_at": self.generated_at,
            "data_cutoff": self.data_cutoff,
        }

    def match_payload(self, match_id: str) -> dict:
        match = next(match for match in self.fixtures if match.id == match_id)
        return {
            "id": match.id,
            "number": match.number,
            "stage": match.stage,
            "kickoff": match.kickoff.isoformat(),
            "venue_id": match.venue_id,
            "group": match.group,
            "home_team": (
                self.team_payload(match.home_team_id) if match.home_team_id else None
            ),
            "away_team": (
                self.team_payload(match.away_team_id) if match.away_team_id else None
            ),
            "home_slot": match.home_slot,
            "away_slot": match.away_slot,
            "prediction": self.prediction_payload(match.id),
        }

    def latest_simulation(self) -> dict:
        if self._simulation is None:
            snapshot_path = ROOT / "data" / "generated" / "latest.json"
            if snapshot_path.exists():
                snapshot = json.loads(snapshot_path.read_text())
                simulation = snapshot.get("simulation", {})
                if (
                    snapshot.get("model_version") == MODEL_VERSION
                    and simulation.get("iterations") == PUBLISHED_SIMULATION_ITERATIONS
                ):
                    self._simulation = simulation
                    self._simulation_generated_at = snapshot["generated_at"]
                    self._simulation_data_cutoff = snapshot["data_cutoff"]
            if self._simulation is None:
                self._simulation = simulate_tournament(
                    iterations=PUBLISHED_SIMULATION_ITERATIONS,
                    seed=2026,
                    context_repository=self.contexts,
                    cutoff=datetime.fromisoformat(self.data_cutoff),
                )
        return {
            **self._simulation,
            "model_version": MODEL_VERSION,
            "generated_at": self._simulation_generated_at,
            "data_cutoff": self._simulation_data_cutoff,
        }

    def team_profile_payload(self, team_id: str) -> dict:
        team = self.teams_by_id[team_id]
        team_names = {item.id: item.name for item in self.teams}
        cutoff = datetime.fromisoformat(self.data_cutoff)
        recent = recent_results(
            team_id,
            self.contexts.results,
            team_names,
            cutoff,
        )
        players = key_players(team_id, self.squad_players)
        probability = next(
            row
            for row in self.latest_simulation()["teams"]
            if row["team_id"] == team_id
        )
        matches = [
            self.match_payload(match.id)
            for match in self.fixtures
            if team_id in (match.home_team_id, match.away_team_id)
        ]
        group_path = []
        for match in matches:
            prediction = match["prediction"]
            if prediction is None:
                continue
            is_home = match["home_team"]["id"] == team_id
            opponent = match["away_team"] if is_home else match["home_team"]
            group_path.append(
                {
                    "match_id": match["id"],
                    "opponent_id": opponent["id"],
                    "opponent_name": opponent["name"],
                    "kickoff": match["kickoff"],
                    "team_win_probability": prediction["probabilities"][
                        "home_win" if is_home else "away_win"
                    ],
                    "draw_probability": prediction["probabilities"]["draw"],
                    "opponent_win_probability": prediction["probabilities"][
                        "away_win" if is_home else "home_win"
                    ],
                }
            )
        analysis = team_analysis(
            team,
            probability,
            recent,
            players,
            group_path,
            "experimental",
        )
        return {
            **self.team_payload(team_id),
            "matches": matches,
            "group_path": group_path,
            "tournament_probability": probability,
            "recent_results": recent,
            "form_summary": form_summary(recent),
            "key_players": player_payload(players),
            "analysis": analysis,
            "player_data_source": self.squad_metadata,
            "results_data_cutoff": self.data_cutoff,
        }


service = PredictionService()
