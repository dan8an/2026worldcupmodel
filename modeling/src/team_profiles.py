import json
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .data import ROOT
from .domain import Team
from .features.context import HistoricalResult

SQUAD_PATH = ROOT / "data" / "raw" / "world_cup_squads.json"
SQUAD_METADATA_PATH = ROOT / "data" / "raw" / "world_cup_squads.metadata.json"


@dataclass(frozen=True)
class PlayerProfile:
    name: str
    position: str
    club: str
    caps: int
    goals: int
    age: int | None
    why_key: str


def load_squad_players(path: Path = SQUAD_PATH) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def load_squad_metadata(path: Path = SQUAD_METADATA_PATH) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def key_players(team_id: str, players: list[dict], limit: int = 4) -> list[PlayerProfile]:
    team_players = [player for player in players if player["team_id"] == team_id]

    def established_score(player: dict) -> float:
        position_bonus = 4 if player["position"] in ("MF", "FW") else 0
        return player["goals"] * 4 + player["caps"] * 0.3 + position_bonus

    established = sorted(
        team_players,
        key=lambda player: (-established_score(player), player["name"]),
    )
    selected = established[: max(0, limit - 1)]
    emerging_selection: dict | None = None
    emerging = [
        player
        for player in team_players
        if player.get("age") is not None
        and player["age"] <= 25
        and player["position"] in ("MF", "FW")
        and player not in selected
    ]
    if emerging:
        emerging.sort(
            key=lambda player: (
                -(
                    player["goals"] * 3
                    + player["caps"] * 0.15
                    + max(0, 26 - player["age"]) * 3
                ),
                player["name"],
            )
        )
        emerging_selection = emerging[0]
        selected.append(emerging_selection)
    for player in established:
        if len(selected) >= limit:
            break
        if player not in selected:
            selected.append(player)
    profiles = []
    for player in selected:
        is_emerging = player is emerging_selection
        if is_emerging:
            reason = "High-upside younger squad contributor"
        elif player["goals"] >= 20:
            reason = "Primary international scoring threat"
        elif player["caps"] >= 70:
            reason = "High-experience tournament leader"
        elif player["position"] == "GK":
            reason = "Experienced last line of defense"
        elif player["position"] == "DF":
            reason = "Experienced defensive reference point"
        else:
            reason = "Important experienced contributor"
        profiles.append(
            PlayerProfile(
                **{
                    key: player.get(key)
                    for key in ("name", "position", "club", "caps", "goals", "age")
                },
                why_key=reason,
            )
        )
    return profiles


def recent_results(
    team_id: str,
    results: list[HistoricalResult],
    team_names: dict[str, str],
    cutoff: datetime,
    limit: int = 8,
) -> list[dict]:
    relevant = [
        result
        for result in results
        if result.played_on < cutoff.date()
        and team_id in (result.home_team_id, result.away_team_id)
    ]
    relevant.sort(key=lambda result: result.played_on, reverse=True)
    output = []
    for result in relevant[:limit]:
        is_home = result.home_team_id == team_id
        opponent_id = result.away_team_id if is_home else result.home_team_id
        goals_for = result.home_score if is_home else result.away_score
        goals_against = result.away_score if is_home else result.home_score
        outcome = "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L"
        output.append(
            {
                "played_on": result.played_on.isoformat(),
                "opponent_id": opponent_id,
                "opponent_name": team_names.get(opponent_id, opponent_id),
                "goals_for": goals_for,
                "goals_against": goals_against,
                "outcome": outcome,
                "tournament": result.tournament,
                "neutral": result.neutral,
            }
        )
    return output


def form_summary(results: list[dict]) -> dict:
    outcomes = Counter(result["outcome"] for result in results)
    goals_for = sum(result["goals_for"] for result in results)
    goals_against = sum(result["goals_against"] for result in results)
    return {
        "matches": len(results),
        "wins": outcomes["W"],
        "draws": outcomes["D"],
        "losses": outcomes["L"],
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goal_difference": goals_for - goals_against,
        "points_per_match": round(
            (outcomes["W"] * 3 + outcomes["D"]) / len(results), 2
        )
        if results
        else 0.0,
    }


def _probability_band(probability: float) -> str:
    if probability >= 0.2:
        return "one of the leading contenders"
    if probability >= 0.08:
        return "a credible title contender"
    if probability >= 0.025:
        return "a dangerous outside contender"
    if probability >= 0.008:
        return "a long-shot knockout threat"
    return "an underdog whose first objective is reaching the knockout stage"


def team_analysis(
    team: Team,
    probability: dict,
    results: list[dict],
    players: list[PlayerProfile],
    group_matches: list[dict],
    model_status: str,
) -> dict:
    summary = form_summary(results)
    easiest = max(
        group_matches,
        key=lambda match: match["team_win_probability"],
        default=None,
    )
    hardest = min(
        group_matches,
        key=lambda match: match["team_win_probability"],
        default=None,
    )
    player_names = ", ".join(player.name for player in players[:3]) or "the final squad"
    form_phrase = (
        f"{summary['wins']} wins, {summary['draws']} draws and {summary['losses']} losses "
        f"across the latest {summary['matches']} mapped matches"
        if summary["matches"]
        else "limited mapped recent-result coverage"
    )
    overview = (
        f"{team.name} enters Group {team.group} as {_probability_band(probability['champion'])}. "
        f"The simulation gives them a {probability['round_of_32']:.1%} chance to reach the "
        f"Round of 32 and a {probability['champion']:.1%} chance to win the tournament. "
        f"Those figures should be read as model estimates rather than guarantees because the "
        f"current model status is {model_status}."
    )
    form = (
        f"Recent form is summarized by {form_phrase}, with {summary['goals_for']} goals scored "
        f"and {summary['goals_against']} conceded. The model uses a four-year decayed history, "
        f"while this page shows the most recent results for readability. Opponent strength is "
        f"not fully separated in the displayed record, so the raw win-loss line should not be "
        f"treated as a standalone rating."
    )
    path = (
        f"The most favorable modeled group matchup is against {easiest['opponent_name']} "
        f"({easiest['team_win_probability']:.1%} win probability), while the most difficult is "
        f"{hardest['opponent_name']} ({hardest['team_win_probability']:.1%}). "
        f"The practical tournament goal is to accumulate enough points to finish in the top two "
        f"or rank among the eight best third-place teams; avoiding a damaging result in the "
        f"most favorable fixture is especially important."
        if easiest and hardest
        else "Group-path analysis will update when fixture predictions are available."
    )
    personnel = (
        f"The key-player view highlights {player_names} using international caps and goals from "
        f"the current squad snapshot. This identifies experience and established output, not "
        f"necessarily the exact starting lineup or current club form. Late squad replacements "
        f"and sourced injury reports can still change the outlook."
    )
    return {
        "headline": f"{team.name}: tournament outlook",
        "overview": overview,
        "form": form,
        "path": path,
        "personnel": personnel,
        "objectives": [
            f"Reach the Round of 32: {probability['round_of_32']:.1%}",
            f"Reach the quarterfinals: {probability['quarterfinal']:.1%}",
            f"Reach the final: {probability['final']:.1%}",
            f"Win the tournament: {probability['champion']:.1%}",
        ],
        "method": "Structured AI-style analysis generated only from model outputs and sourced team data.",
    }


def player_payload(players: list[PlayerProfile]) -> list[dict]:
    return [asdict(player) for player in players]
