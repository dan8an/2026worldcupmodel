#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, TextIO

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data_ingestion import RateLimitError
from scripts.data_ingestion.providers import ApiFootballProvider

FILTER_TERMS = {
    "euro": "Euro",
    "copa-america": "Copa America",
    "nations-league": "Nations League",
    "world-cup-qualification": "World Cup Qualification",
}


def load_environment() -> dict[str, str]:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List API-Football competitions and available seasons.",
        epilog=(
            "Examples:\n"
            "  python scripts/list_competitions.py --filter euro\n"
            "  python scripts/list_competitions.py --filter copa-america\n"
            "  python scripts/list_competitions.py --filter nations-league\n"
            "  python scripts/list_competitions.py "
            "--filter world-cup-qualification\n"
            '  python scripts/list_competitions.py --search "Gold Cup"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--filter",
        action="append",
        choices=sorted(FILTER_TERMS),
        default=[],
        help="Named competition family filter; may be supplied more than once.",
    )
    parser.add_argument(
        "--search",
        action="append",
        default=[],
        help="Free-text name or country search; may be supplied more than once.",
    )
    return parser.parse_args(argv)


def _searchable(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def filter_competitions(
    competitions: list[dict[str, Any]],
    searches: list[str],
) -> list[dict[str, Any]]:
    normalized_searches = [_searchable(search) for search in searches if search.strip()]
    if not normalized_searches:
        return competitions
    results = []
    for competition in competitions:
        haystack = _searchable(
            f"{competition['name']} {competition['country']}"
        )
        padded_haystack = f" {haystack} "
        if any(f" {search} " in padded_haystack for search in normalized_searches):
            results.append(competition)
    return results


def format_seasons(seasons: list[int]) -> str:
    if not seasons:
        return "-"
    ranges: list[tuple[int, int]] = []
    start = previous = seasons[0]
    for season in seasons[1:]:
        if season == previous + 1:
            previous = season
            continue
        ranges.append((start, previous))
        start = previous = season
    ranges.append((start, previous))
    return ", ".join(
        str(start) if start == end else f"{start}-{end}"
        for start, end in ranges
    )


def render_table(
    competitions: list[dict[str, Any]],
    output: TextIO = sys.stdout,
) -> None:
    headers = ("League ID", "Competition", "Country", "Available seasons")
    rows = [
        (
            str(competition["league_id"]),
            competition["name"],
            competition["country"],
            format_seasons(competition["seasons"]),
        )
        for competition in competitions
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]
    line = "  ".join(
        header.ljust(widths[index]) for index, header in enumerate(headers)
    )
    separator = "  ".join("-" * width for width in widths)
    print(line, file=output)
    print(separator, file=output)
    for row in rows:
        print(
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)),
            file=output,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = load_environment()
    api_key = env.get("API_FOOTBALL_KEY")
    if not api_key:
        print("API_FOOTBALL_KEY is required.", file=sys.stderr)
        return 2

    provider = ApiFootballProvider(
        api_key=api_key,
        base_url=env.get(
            "API_FOOTBALL_BASE_URL",
            "https://v3.football.api-sports.io",
        ),
        request_delay_seconds=float(
            env.get("API_FOOTBALL_REQUEST_DELAY_SECONDS", "1.0")
        ),
        logger=logging.getLogger("list_competitions"),
    )
    try:
        competitions = provider.get_competitions()
    except RateLimitError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Could not load API-Football competitions: {error}", file=sys.stderr)
        return 1

    searches = [FILTER_TERMS[name] for name in args.filter] + args.search
    competitions = filter_competitions(competitions, searches)
    competitions.sort(
        key=lambda competition: (
            competition["country"].lower(),
            competition["name"].lower(),
            competition["league_id"],
        )
    )
    render_table(competitions)
    print(f"\n{len(competitions)} competition(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
