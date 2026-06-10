import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import Match, Team

ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = ROOT / "data" / "seed"

GROUP_MATCH_DATES = {
    "A": (11, 18, 24),
    "B": (12, 18, 24),
    "C": (13, 19, 24),
    "D": (12, 19, 25),
    "E": (14, 20, 25),
    "F": (14, 20, 25),
    "G": (15, 21, 26),
    "H": (15, 21, 26),
    "I": (16, 22, 26),
    "J": (16, 22, 27),
    "K": (17, 23, 27),
    "L": (17, 23, 27),
}
VENUE_IDS = [
    "MEX", "TOR", "LA", "BOS", "SF", "DAL", "HOU", "KC",
    "ATL", "MIA", "NYNJ", "PHI", "SEA", "VAN", "GDL", "MTY",
]
GROUP_PAIRINGS = (((1, 2), (3, 4)), ((1, 3), (4, 2)), ((4, 1), (2, 3)))


def load_teams() -> list[Team]:
    payload = json.loads((SEED_DIR / "teams.json").read_text())
    return [Team(**item) for item in payload]


def load_venues() -> list[dict]:
    return json.loads((SEED_DIR / "venues.json").read_text())


def build_fixtures(teams: list[Team] | None = None) -> list[Match]:
    teams = teams or load_teams()
    by_group: dict[str, dict[int, Team]] = {}
    for team in teams:
        by_group.setdefault(team.group, {})[team.position] = team

    fixtures: list[Match] = []
    number = 1
    for group in "ABCDEFGHIJKL":
        for matchday, pairings in enumerate(GROUP_PAIRINGS):
            day = GROUP_MATCH_DATES[group][matchday]
            for index, (home_position, away_position) in enumerate(pairings):
                kickoff = datetime(2026, 6, day, 17 + index * 3, tzinfo=timezone.utc)
                fixtures.append(
                    Match(
                        id=f"WC26-{number:03d}",
                        number=number,
                        stage="group",
                        kickoff=kickoff,
                        venue_id=VENUE_IDS[(number - 1) % len(VENUE_IDS)],
                        home_team_id=by_group[group][home_position].id,
                        away_team_id=by_group[group][away_position].id,
                        group=group,
                    )
                )
                number += 1

    round_specs = (
        ("round_of_32", 16, datetime(2026, 6, 28, 16, tzinfo=timezone.utc)),
        ("round_of_16", 8, datetime(2026, 7, 4, 16, tzinfo=timezone.utc)),
        ("quarterfinal", 4, datetime(2026, 7, 9, 19, tzinfo=timezone.utc)),
        ("semifinal", 2, datetime(2026, 7, 14, 19, tzinfo=timezone.utc)),
        ("third_place", 1, datetime(2026, 7, 18, 19, tzinfo=timezone.utc)),
        ("final", 1, datetime(2026, 7, 19, 19, tzinfo=timezone.utc)),
    )
    previous_numbers: list[int] = []
    semifinal_numbers: list[int] = []
    for stage, count, start in round_specs:
        current_numbers = list(range(number, number + count))
        for index, match_number in enumerate(current_numbers):
            if stage == "round_of_32":
                home_slot, away_slot = f"R32-H{index + 1}", f"R32-A{index + 1}"
            elif stage == "third_place":
                home_slot = f"Loser M{semifinal_numbers[0]}"
                away_slot = f"Loser M{semifinal_numbers[1]}"
            elif stage == "final":
                home_slot = f"Winner M{semifinal_numbers[0]}"
                away_slot = f"Winner M{semifinal_numbers[1]}"
            else:
                parent_a = previous_numbers[index * 2]
                parent_b = previous_numbers[index * 2 + 1]
                home_slot, away_slot = f"Winner M{parent_a}", f"Winner M{parent_b}"
            fixtures.append(
                Match(
                    id=f"WC26-{match_number:03d}",
                    number=match_number,
                    stage=stage,
                    kickoff=start + timedelta(days=index // 3, hours=(index % 3) * 3),
                    venue_id=VENUE_IDS[(match_number - 1) % len(VENUE_IDS)],
                    home_slot=home_slot,
                    away_slot=away_slot,
                )
            )
        if stage == "semifinal":
            semifinal_numbers = current_numbers
        if stage != "third_place":
            previous_numbers = current_numbers
        number += count
    return sorted(fixtures, key=lambda match: (match.kickoff, match.number))


def validate_tournament(teams: list[Team], fixtures: list[Match]) -> None:
    if len(teams) != 48 or len({team.id for team in teams}) != 48:
        raise ValueError("Tournament must contain 48 unique teams")
    if len(fixtures) != 104 or len({match.id for match in fixtures}) != 104:
        raise ValueError("Tournament must contain 104 unique fixtures")
    for group in "ABCDEFGHIJKL":
        members = [team for team in teams if team.group == group]
        if len(members) != 4 or {team.position for team in members} != {1, 2, 3, 4}:
            raise ValueError(f"Group {group} must contain positions 1 through 4")
    host_ids = {team.id for team in teams if team.host}
    if host_ids != {"MEX", "CAN", "USA"}:
        raise ValueError("Only Mexico, Canada, and the United States may be marked as hosts")
    group_matches = [match for match in fixtures if match.stage == "group"]
    if len(group_matches) != 72:
        raise ValueError("Group stage must contain 72 fixtures")
    if fixtures != sorted(fixtures, key=lambda match: (match.kickoff, match.number)):
        raise ValueError("Fixtures must be ordered chronologically")
