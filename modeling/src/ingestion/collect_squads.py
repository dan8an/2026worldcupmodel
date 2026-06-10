import hashlib
import json
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import certifi
from bs4 import BeautifulSoup

from ..data import ROOT
from ..features.context import _load_aliases

SOURCE_URL = "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup_squads"
OUTPUT = ROOT / "data" / "raw" / "world_cup_squads.json"
METADATA = ROOT / "data" / "raw" / "world_cup_squads.metadata.json"


def _integer(value: str) -> int:
    match = re.search(r"\d+", value.replace(",", ""))
    return int(match.group()) if match else 0


def _position(value: str) -> str:
    for code in ("GK", "DF", "MF", "FW"):
        if code in value.upper():
            return code
    return value.strip()


def _player_name(value: str) -> str:
    return re.sub(r"\s*\(\s*(?:captain|vice-captain)\s*\)\s*", "", value, flags=re.I).strip()


def _age(value: str) -> int | None:
    match = re.search(r"aged\s+(\d+)", value, flags=re.I)
    return int(match.group(1)) if match else None


def collect(url: str = SOURCE_URL, output: Path = OUTPUT) -> Path:
    request = Request(url, headers={"User-Agent": "2026worldcupmodel/0.1"})
    tls_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=30, context=tls_context) as response:
        payload = response.read()
    soup = BeautifulSoup(payload, "html.parser")
    aliases = _load_aliases()
    players = []
    for table in soup.select("table.wikitable"):
        heading = table.find_previous("h3")
        if heading is None:
            continue
        team_name = heading.get_text(" ", strip=True)
        team_id = aliases.get(team_name.casefold())
        if not team_id:
            continue
        rows = table.select("tr")
        if not rows:
            continue
        headers = [
            cell.get_text(" ", strip=True).lower()
            for cell in rows[0].select("th,td")
        ]
        if "player" not in headers or "caps" not in headers or "goals" not in headers:
            continue
        indices = {header: index for index, header in enumerate(headers)}
        for row in rows[1:]:
            cells = [cell.get_text(" ", strip=True) for cell in row.select("th,td")]
            if len(cells) < len(headers):
                continue
            name = _player_name(cells[indices["player"]])
            if not name:
                continue
            players.append(
                {
                    "team_id": team_id,
                    "name": name,
                    "position": _position(cells[indices.get("pos.", 1)]),
                    "club": cells[indices.get("club", len(cells) - 1)],
                    "caps": _integer(cells[indices["caps"]]),
                    "goals": _integer(cells[indices["goals"]]),
                    "age": _age(cells[indices.get("date of birth (age)", 3)]),
                }
            )
    covered = {player["team_id"] for player in players}
    if len(covered) < 40 or len(players) < 900:
        raise ValueError(
            f"Squad payload coverage is too low: {len(covered)} teams, {len(players)} players"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(players, indent=2, ensure_ascii=False))
    metadata = {
        "source_url": url,
        "source_name": "Wikipedia: 2026 FIFA World Cup squads",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "player_count": len(players),
        "team_count": len(covered),
        "note": "Squad tables cite official team and FIFA announcements; verify late replacements.",
    }
    METADATA.write_text(json.dumps(metadata, indent=2))
    return output


if __name__ == "__main__":
    print(collect())
