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
    elo_base_home_probability: float | None = None
    elo_base_draw_probability: float | None = None
    elo_base_away_probability: float | None = None
    attack_defense_adjustment: float | None = None
    draw_calibration_adjustment: float | None = None
    context_adjustment_total: float | None = None
    final_home_probability: float | None = None
    final_draw_probability: float | None = None
    final_away_probability: float | None = None
    confidence_score: float | None = None
    confidence_tier: str | None = None
    confidence_explanation: str | None = None
    top_factors: list[dict[str, str]] = Field(default_factory=list)
    top_scores: list[ScoreProbabilityResponse]
    confidence: str
    key_factors: list[str]
    context: ContextResponse
    model_version: str
    source: str
    generated_at: str
    data_cutoff: str
    generation_mode: str = "standard"
    historical_cutoff: str | None = None
    backfilled_at: str | None = None


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
    status: str
    home_score: int | None
    away_score: int | None
    prediction: PredictionResponse | None


class SimulationRequest(BaseModel):
    iterations: int = Field(default=1000, ge=1, le=10000)
    seed: int = 2026
