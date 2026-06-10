import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from ..data import ROOT
from ..domain import MatchContext

RAW_RESULTS = ROOT / "data" / "raw" / "international_results.csv"
AVAILABILITY_REPORTS = ROOT / "data" / "context" / "availability_reports.json"
SQUAD_SELECTIONS = ROOT / "data" / "context" / "squad_selections.json"
TEAM_ALIASES = ROOT / "data" / "seed" / "team_aliases.json"

COMPETITION_WEIGHTS = {
    "FIFA World Cup": 1.35,
    "FIFA World Cup qualification": 1.2,
    "UEFA Euro": 1.25,
    "UEFA Euro qualification": 1.1,
    "Copa América": 1.25,
    "African Cup of Nations": 1.25,
    "AFC Asian Cup": 1.25,
    "CONCACAF Gold Cup": 1.2,
    "Friendly": 0.65,
}
STATUS_IMPACT = {
    "out": 1.0,
    "omitted": 0.9,
    "doubtful": 0.65,
    "questionable": 0.4,
    "limited": 0.25,
    "available": 0.0,
}


@dataclass(frozen=True)
class HistoricalResult:
    played_on: date
    home_team_id: str
    away_team_id: str
    home_score: int
    away_score: int
    tournament: str
    neutral: bool


@dataclass(frozen=True)
class AvailabilityReport:
    team_id: str
    player_name: str
    status: str
    importance: float
    confidence: float
    published_at: datetime
    effective_from: datetime
    effective_until: datetime | None
    source_url: str
    source_name: str
    note: str


@dataclass(frozen=True)
class SquadSelection:
    team_id: str
    player_name: str
    selection_status: str
    importance: float
    confidence: float
    published_at: datetime
    source_url: str
    source_name: str
    note: str


def _load_aliases(path: Path = TEAM_ALIASES) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return {
        alias.casefold(): team_id
        for team_id, aliases in payload.items()
        for alias in aliases
    }


def load_historical_results(
    path: Path = RAW_RESULTS,
    aliases_path: Path = TEAM_ALIASES,
) -> list[HistoricalResult]:
    if not path.exists():
        return []
    aliases = _load_aliases(aliases_path)
    results: list[HistoricalResult] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            home_id = aliases.get(row["home_team"].strip().casefold())
            away_id = aliases.get(row["away_team"].strip().casefold())
            if not home_id or not away_id:
                continue
            if not row["home_score"].isdigit() or not row["away_score"].isdigit():
                continue
            results.append(
                HistoricalResult(
                    played_on=date.fromisoformat(row["date"]),
                    home_team_id=home_id,
                    away_team_id=away_id,
                    home_score=int(row["home_score"]),
                    away_score=int(row["away_score"]),
                    tournament=row["tournament"].strip(),
                    neutral=row["neutral"].strip().upper() == "TRUE",
                )
            )
    return results


def load_availability_reports(path: Path = AVAILABILITY_REPORTS) -> list[AvailabilityReport]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    reports: list[AvailabilityReport] = []
    for item in payload:
        status = item["status"].lower()
        if status not in STATUS_IMPACT:
            raise ValueError(f"Unsupported availability status: {status}")
        source_url = item["source_url"]
        if not source_url.startswith(("https://", "http://")):
            raise ValueError("Availability reports require an HTTP source URL")
        importance = float(item["importance"])
        confidence = float(item["confidence"])
        if not 0 <= importance <= 1 or not 0 <= confidence <= 1:
            raise ValueError("Availability importance and confidence must be between 0 and 1")
        reports.append(
            AvailabilityReport(
                team_id=item["team_id"],
                player_name=item["player_name"],
                status=status,
                importance=importance,
                confidence=confidence,
                published_at=datetime.fromisoformat(item["published_at"]),
                effective_from=datetime.fromisoformat(item["effective_from"]),
                effective_until=(
                    datetime.fromisoformat(item["effective_until"])
                    if item.get("effective_until")
                    else None
                ),
                source_url=source_url,
                source_name=item["source_name"],
                note=item.get("note", ""),
            )
        )
    return reports


def load_squad_selections(path: Path = SQUAD_SELECTIONS) -> list[SquadSelection]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    selections: list[SquadSelection] = []
    supported = {"preliminary", "final", "reserve", "omitted", "removed"}
    for item in payload:
        status = item["selection_status"].lower()
        if status not in supported:
            raise ValueError(f"Unsupported squad selection status: {status}")
        source_url = item["source_url"]
        if not source_url.startswith(("https://", "http://")):
            raise ValueError("Squad selections require an HTTP source URL")
        importance = float(item["importance"])
        confidence = float(item["confidence"])
        if not 0 <= importance <= 1 or not 0 <= confidence <= 1:
            raise ValueError("Squad importance and confidence must be between 0 and 1")
        selections.append(
            SquadSelection(
                team_id=item["team_id"],
                player_name=item["player_name"],
                selection_status=status,
                importance=importance,
                confidence=confidence,
                published_at=datetime.fromisoformat(item["published_at"]),
                source_url=source_url,
                source_name=item["source_name"],
                note=item.get("note", ""),
            )
        )
    return selections


def _competition_weight(tournament: str) -> float:
    return COMPETITION_WEIGHTS.get(tournament, 0.9)


def _team_match_value(result: HistoricalResult, team_id: str) -> tuple[float, int]:
    is_home = result.home_team_id == team_id
    goals_for = result.home_score if is_home else result.away_score
    goals_against = result.away_score if is_home else result.home_score
    if goals_for > goals_against:
        points = 1.0
    elif goals_for == goals_against:
        points = 0.5
    else:
        points = 0.0
    goal_component = max(-3, min(3, goals_for - goals_against)) / 6
    return 0.75 * points + 0.25 * (0.5 + goal_component), goals_for - goals_against


def _weighted_form(
    team_id: str,
    results: list[HistoricalResult],
    cutoff: datetime,
    lookback_days: int = 1460,
) -> tuple[float, int]:
    weighted_value = 0.0
    total_weight = 0.0
    count = 0
    for result in results:
        age_days = (cutoff.date() - result.played_on).days
        # Historical inputs must be strictly earlier than the prediction date.
        # Excluding the entire match date also prevents same-day ordering leakage.
        if age_days <= 0 or age_days > lookback_days:
            continue
        if team_id not in (result.home_team_id, result.away_team_id):
            continue
        value, _ = _team_match_value(result, team_id)
        recency = math.exp(-math.log(2) * age_days / 365)
        weight = recency * _competition_weight(result.tournament)
        weighted_value += value * weight
        total_weight += weight
        count += 1
    if total_weight == 0:
        return 0.0, 0
    # A .50 performance is neutral; shrink sparse samples toward neutral.
    sample_reliability = min(1.0, count / 12)
    elo_adjustment = (weighted_value / total_weight - 0.5) * 180 * sample_reliability
    return max(-65.0, min(65.0, elo_adjustment)), count


def _head_to_head(
    home_id: str,
    away_id: str,
    results: list[HistoricalResult],
    cutoff: datetime,
    lookback_days: int = 2920,
) -> tuple[float, int]:
    weighted_value = 0.0
    total_weight = 0.0
    count = 0
    for result in results:
        age_days = (cutoff.date() - result.played_on).days
        if age_days <= 0 or age_days > lookback_days:
            continue
        if {result.home_team_id, result.away_team_id} != {home_id, away_id}:
            continue
        value, _ = _team_match_value(result, home_id)
        weight = math.exp(-math.log(2) * age_days / 730)
        weighted_value += value * weight
        total_weight += weight
        count += 1
    if total_weight == 0:
        return 0.0, 0
    reliability = min(1.0, count / 6)
    adjustment = (weighted_value / total_weight - 0.5) * 40 * reliability
    return max(-15.0, min(15.0, adjustment)), count


def _availability_adjustment(
    team_id: str,
    reports: list[AvailabilityReport],
    squads: list[SquadSelection],
    cutoff: datetime,
) -> tuple[float, int]:
    impacts_by_player: dict[str, float] = {}
    latest_by_player: dict[str, AvailabilityReport] = {}
    for report in reports:
        if report.team_id != team_id or report.published_at > cutoff:
            continue
        if report.effective_from > cutoff:
            continue
        if report.effective_until and report.effective_until < cutoff:
            continue
        current = latest_by_player.get(report.player_name.casefold())
        if current is None or report.published_at > current.published_at:
            latest_by_player[report.player_name.casefold()] = report
    for report in latest_by_player.values():
        impact = (
            35
            * STATUS_IMPACT[report.status]
            * report.importance
            * report.confidence
        )
        if impact > 0:
            impacts_by_player[report.player_name.casefold()] = impact
    latest_selection: dict[str, SquadSelection] = {}
    for selection in squads:
        if selection.team_id != team_id or selection.published_at > cutoff:
            continue
        current = latest_selection.get(selection.player_name.casefold())
        if current is None or selection.published_at > current.published_at:
            latest_selection[selection.player_name.casefold()] = selection
    for selection in latest_selection.values():
        status_impact = {"omitted": 0.9, "removed": 1.0}.get(
            selection.selection_status, 0.0
        )
        impact = 35 * status_impact * selection.importance * selection.confidence
        player_key = selection.player_name.casefold()
        if impact > impacts_by_player.get(player_key, 0.0):
            impacts_by_player[player_key] = impact
    total_impact = -sum(impacts_by_player.values())
    return max(-90.0, min(0.0, total_impact)), len(impacts_by_player)


class ContextRepository:
    def __init__(
        self,
        results: list[HistoricalResult] | None = None,
        reports: list[AvailabilityReport] | None = None,
        squads: list[SquadSelection] | None = None,
    ) -> None:
        self.results = results if results is not None else load_historical_results()
        self.reports = reports if reports is not None else load_availability_reports()
        self.squads = squads if squads is not None else load_squad_selections()
        self._context_cache: dict[tuple[str, str, datetime], MatchContext] = {}

    def for_match(self, home_id: str, away_id: str, cutoff: datetime) -> MatchContext:
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        cache_key = (home_id, away_id, cutoff)
        if cache_key in self._context_cache:
            return self._context_cache[cache_key]
        home_form, home_count = _weighted_form(home_id, self.results, cutoff)
        away_form, away_count = _weighted_form(away_id, self.results, cutoff)
        home_h2h, h2h_count = _head_to_head(home_id, away_id, self.results, cutoff)
        home_availability, home_reports = _availability_adjustment(
            home_id, self.reports, self.squads, cutoff
        )
        away_availability, away_reports = _availability_adjustment(
            away_id, self.reports, self.squads, cutoff
        )
        context = MatchContext(
            home_form_elo=round(home_form, 3),
            away_form_elo=round(away_form, 3),
            home_h2h_elo=round(home_h2h, 3),
            away_h2h_elo=round(-home_h2h, 3),
            home_availability_elo=round(home_availability, 3),
            away_availability_elo=round(away_availability, 3),
            historical_matches_home=home_count,
            historical_matches_away=away_count,
            h2h_matches=h2h_count,
            availability_reports=home_reports + away_reports,
            data_cutoff=cutoff,
        )
        self._context_cache[cache_key] = context
        return context
