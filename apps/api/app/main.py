import os
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .schemas import MatchResponse, SimulationRequest, TeamResponse
from .service import STATIC_MODEL_VERSION, service
from modeling.src.simulation import simulate_tournament

# Local development, production, and optional deployment-specific frontend origins.
allowed_origins = [
    "https://footballoracle.vercel.app",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
if web_origin := os.getenv("WEB_ORIGIN"):
    if web_origin not in allowed_origins:
        allowed_origins.append(web_origin)

app = FastAPI(
    title="2026 World Cup Prediction API",
    version="0.1.0",
    description="Educational match and tournament probabilities. Not betting advice.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "model_version": service.current_prediction_run()["model_version"],
    }


@app.get("/v1/tournament")
def tournament() -> dict:
    return {
        "id": "FIFA-WC-2026",
        "name": "2026 FIFA World Cup",
        "starts_on": "2026-06-11",
        "ends_on": "2026-07-19",
        "team_count": len(service.teams),
        "match_count": len(service.fixtures),
        "groups": list("ABCDEFGHIJKL"),
        "venues": service.venues,
    }


@app.get("/api/teams", response_model=list[TeamResponse], include_in_schema=False)
@app.get("/v1/teams", response_model=list[TeamResponse])
def teams() -> list[dict]:
    return [service.team_payload(team.id) for team in service.teams]


@app.get("/v1/teams/{team_id}")
def team(team_id: str) -> dict:
    if team_id not in service.teams_by_id:
        raise HTTPException(status_code=404, detail="Team not found")
    return service.team_profile_payload(team_id)


@app.get("/api/matches", response_model=list[MatchResponse], include_in_schema=False)
@app.get("/v1/matches", response_model=list[MatchResponse])
def matches(
    stage: str | None = Query(default=None),
    group: str | None = Query(default=None, min_length=1, max_length=1),
    team_id: str | None = Query(default=None),
) -> list[dict]:
    fixtures = service.fixtures
    if stage:
        fixtures = [match for match in fixtures if match.stage == stage]
    if group:
        fixtures = [match for match in fixtures if match.group == group.upper()]
    if team_id:
        fixtures = [
            match
            for match in fixtures
            if team_id in (match.home_team_id, match.away_team_id)
        ]
    prediction_run = service.current_prediction_run()
    return [
        service.match_payload(match.id, prediction_run)
        for match in fixtures
    ]


@app.get(
    "/api/matches/{match_id}",
    response_model=MatchResponse,
    include_in_schema=False,
)
@app.get("/v1/matches/{match_id}", response_model=MatchResponse)
def match(match_id: str) -> dict:
    if not any(fixture.id == match_id for fixture in service.fixtures):
        raise HTTPException(status_code=404, detail="Match not found")
    return service.match_payload(match_id)


@app.get("/v1/predictions/latest")
def predictions() -> dict:
    return service.latest_predictions_payload()


@app.get("/api/simulations/latest", include_in_schema=False)
@app.get("/v1/simulations/latest")
def latest_simulation() -> dict:
    return service.latest_simulation()


@app.post("/v1/simulations/custom")
def custom_simulation(request: SimulationRequest) -> dict:
    return {
        **simulate_tournament(
            iterations=request.iterations,
            seed=request.seed,
            context_repository=service.contexts,
            cutoff=datetime.fromisoformat(service.data_cutoff),
        ),
        "model_version": STATIC_MODEL_VERSION,
        "generated_at": service.generated_at,
        "data_cutoff": service.data_cutoff,
    }


@app.get("/v1/model/versions/current")
def model_version() -> dict:
    current_version = service.current_prediction_run()["model_version"]
    return {
        "name": "Context-adjusted Poisson model",
        "semantic_version": current_version,
        "feature_schema_version": "1",
        "training_cutoff": None,
        "status": "experimental",
    }


@app.get("/v1/model/performance")
def model_performance() -> dict:
    report_path = Path(__file__).resolve().parents[3] / "data" / "evaluation" / "latest.json"
    if not report_path.exists():
        return {
            "status": "not_backtested",
            "message": "Run python -m modeling.src.evaluation.backtest.",
            "metrics": {},
        }
    report = json.loads(report_path.read_text())
    return {
        "status": "evaluated",
        "message": (
            "Context model passed the promotion gate."
            if report["promotion_gate"]["status"] == "pass"
            else "Context model did not beat walk-forward Elo and remains experimental."
        ),
        **report,
    }


@app.get("/v1/data/status")
def data_status() -> dict:
    latest_result = (
        max(result.played_on for result in service.contexts.results).isoformat()
        if service.contexts.results
        else None
    )
    latest_report = (
        max(report.published_at for report in service.contexts.reports).isoformat()
        if service.contexts.reports
        else None
    )
    return {
        "historical_results": len(service.contexts.results),
        "latest_historical_result": latest_result,
        "availability_reports": len(service.contexts.reports),
        "latest_availability_report": latest_report,
        "squad_selections": len(service.contexts.squads),
        "latest_squad_selection": (
            max(selection.published_at for selection in service.contexts.squads).isoformat()
            if service.contexts.squads
            else None
        ),
        "data_cutoff": service.data_cutoff,
    }
