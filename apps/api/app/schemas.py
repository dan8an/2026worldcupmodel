from pydantic import BaseModel, Field


class TeamResponse(BaseModel):
    id: str
    name: str
    group: str
    position: int
    rank: int
    host: bool
    elo: float
    flag: str


class ScoreProbabilityResponse(BaseModel):
    home: int
    away: int
    probability: float


class ProbabilityResponse(BaseModel):
    home_win: float
    draw: float
    away_win: float


class ContextResponse(BaseModel):
    home_form_elo: float
    away_form_elo: float
    home_h2h_elo: float
    away_h2h_elo: float
    home_availability_elo: float
    away_availability_elo: float
    historical_matches_home: int
    historical_matches_away: int
    h2h_matches: int
    availability_reports: int
    data_cutoff: str | None


class PredictionResponse(BaseModel):
    match_id: str
    home_team_id: str
    away_team_id: str
    home_xg: float
    away_xg: float
    probabilities: ProbabilityResponse
    top_scores: list[ScoreProbabilityResponse]
    confidence: str
    key_factors: list[str]
    context: ContextResponse
    model_version: str
    generated_at: str
    data_cutoff: str


class MatchResponse(BaseModel):
    id: str
    number: int
    stage: str
    kickoff: str
    venue_id: str
    group: str | None
    home_team: TeamResponse | None
    away_team: TeamResponse | None
    home_slot: str | None
    away_slot: str | None
    prediction: PredictionResponse | None


class SimulationRequest(BaseModel):
    iterations: int = Field(default=1000, ge=1, le=10000)
    seed: int = 2026
