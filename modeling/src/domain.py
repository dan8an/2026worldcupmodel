from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    group: str
    position: int
    rank: int
    host: bool = False

    @property
    def elo(self) -> float:
        # Transparent rank-derived launch prior. Replace with rating snapshots.
        return 2050.0 - 12.0 * (self.rank - 1) ** 0.72


@dataclass(frozen=True)
class Match:
    id: str
    number: int
    stage: str
    kickoff: datetime
    venue_id: str
    home_team_id: str | None = None
    away_team_id: str | None = None
    group: str | None = None
    home_slot: str | None = None
    away_slot: str | None = None


@dataclass(frozen=True)
class MatchContext:
    home_form_elo: float = 0.0
    away_form_elo: float = 0.0
    home_h2h_elo: float = 0.0
    away_h2h_elo: float = 0.0
    home_availability_elo: float = 0.0
    away_availability_elo: float = 0.0
    historical_matches_home: int = 0
    historical_matches_away: int = 0
    h2h_matches: int = 0
    availability_reports: int = 0
    data_cutoff: datetime | None = None

    @property
    def home_adjustment(self) -> float:
        return self.home_form_elo + self.home_h2h_elo + self.home_availability_elo

    @property
    def away_adjustment(self) -> float:
        return self.away_form_elo + self.away_h2h_elo + self.away_availability_elo


@dataclass(frozen=True)
class ScoreProbability:
    home: int
    away: int
    probability: float


@dataclass(frozen=True)
class MatchPrediction:
    match_id: str
    home_team_id: str
    away_team_id: str
    home_xg: float
    away_xg: float
    home_win: float
    draw: float
    away_win: float
    top_scores: tuple[ScoreProbability, ...]
    confidence: str
    key_factors: tuple[str, ...]
    context: MatchContext


@dataclass
class Standing:
    team_id: str
    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0
    points: int = 0
    fair_play: int = 0
    lots: float = field(default=0.0, repr=False)

    @property
    def goal_difference(self) -> int:
        return self.goals_for - self.goals_against
