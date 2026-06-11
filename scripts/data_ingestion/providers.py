from __future__ import annotations

import json
import logging
import ssl
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DATA = ROOT / "backend" / "ingestion" / "sample-data" / "api-football.json"
COMPLETED_STATUSES = {"FT", "AET", "PEN"}


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


class RateLimitError(RuntimeError):
    def __init__(self, retry_after: str | None = None) -> None:
        message = "API-Football rate limit reached"
        if retry_after:
            message += f"; retry after {retry_after} seconds"
        super().__init__(message)
        self.retry_after = retry_after


class SportsProvider(ABC):
    """Normalized interface implemented by every sports data provider."""

    name: str

    @abstractmethod
    def get_completed_matches(self, date: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_completed_matches_range(
        self,
        date_from: str,
        date_to: str,
        league_id: int,
        season: int,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_fixture_statistics(self, fixture_id: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_fixture_players(self, fixture_id: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_lineups(self, fixture_id: int) -> list[dict[str, Any]]:
        raise NotImplementedError


def _team(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    return {
        "provider_id": int(payload["id"]),
        "name": payload.get("name") or "Unknown team",
    }


class ApiFootballProvider(SportsProvider):
    name = "api_football"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://v3.football.api-sports.io",
        league_id: int = 1,
        season: int | None = None,
        request_delay_seconds: float = 1.0,
        logger: logging.Logger | None = None,
        opener: Any = urlopen,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        if not api_key:
            raise ValueError("API_FOOTBALL_KEY is required")
        if request_delay_seconds < 0:
            raise ValueError("API_FOOTBALL_REQUEST_DELAY_SECONDS cannot be negative")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.league_id = league_id
        self.season = season
        self.request_delay_seconds = request_delay_seconds
        self.logger = logger or logging.getLogger(__name__)
        self.tls_context = ssl.create_default_context(cafile=certifi.where())
        self.opener = opener
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_request_at: float | None = None

    def _wait_for_rate_limit(self) -> None:
        if self.last_request_at is None or self.request_delay_seconds == 0:
            return
        elapsed = self.monotonic() - self.last_request_at
        remaining = self.request_delay_seconds - elapsed
        if remaining > 0:
            self.logger.info(
                "[provider:api_football] Waiting %.2fs before the next request",
                remaining,
            )
            self.sleep(remaining)

    def _request(self, path: str, **params: object) -> list[dict[str, Any]]:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.base_url}{path}?{query}"
        self._wait_for_rate_limit()
        self.logger.info("[provider:api_football] GET %s?%s", path, query)
        request = Request(
            url,
            headers={
                "x-apisports-key": self.api_key,
                "User-Agent": "football-oracle-ingestion/0.1",
            },
        )
        try:
            with self.opener(
                request,
                timeout=30,
                context=self.tls_context,
            ) as response:
                payload = json.loads(response.read())
        except HTTPError as error:
            if error.code == 429:
                retry_after = (
                    error.headers.get("Retry-After") if error.headers else None
                )
                raise RateLimitError(retry_after) from None
            raise
        finally:
            self.last_request_at = self.monotonic()
        errors = payload.get("errors")
        if errors:
            values = errors if isinstance(errors, list) else list(errors.values())
            if values:
                message = "; ".join(map(str, values))
                if "rate limit" in message.lower() or "too many requests" in message.lower():
                    raise RateLimitError()
                raise RuntimeError(f"API-Football error: {message}")
        return payload.get("response", [])

    @staticmethod
    def _normalize_completed_matches(
        response: list[dict[str, Any]],
        league_id: int,
    ) -> list[dict[str, Any]]:
        matches = []
        for item in response:
            status = item.get("fixture", {}).get("status", {}).get("short")
            if status not in COMPLETED_STATUSES:
                continue
            response_league_id = item.get("league", {}).get("id")
            if (
                response_league_id is not None
                and int(response_league_id) != league_id
            ):
                continue
            matches.append(
                {
                    "provider_fixture_id": int(item["fixture"]["id"]),
                    "date": item["fixture"]["date"],
                    "status": status,
                    "competition": item.get("league", {}).get("name"),
                    "league_id": item.get("league", {}).get("id"),
                    "season": item.get("league", {}).get("season"),
                    "round": item.get("league", {}).get("round"),
                    "home_team": _team(item.get("teams", {}).get("home")),
                    "away_team": _team(item.get("teams", {}).get("away")),
                    "home_score": item.get("goals", {}).get("home"),
                    "away_score": item.get("goals", {}).get("away"),
                    "raw": item,
                }
            )
        return matches

    def get_completed_matches(self, date: str) -> list[dict[str, Any]]:
        season = self.season or int(date[:4])
        response = self._request(
            "/fixtures",
            date=date,
            league=self.league_id,
            season=season,
        )
        return self._normalize_completed_matches(response, self.league_id)

    def get_completed_matches_range(
        self,
        date_from: str,
        date_to: str,
        league_id: int,
        season: int,
    ) -> list[dict[str, Any]]:
        response = self._request(
            "/fixtures",
            **{
                "league": league_id,
                "season": season,
                "from": date_from,
                "to": date_to,
            },
        )
        return self._normalize_completed_matches(response, league_id)

    def get_competitions(self) -> list[dict[str, Any]]:
        competitions = []
        for item in self._request("/leagues"):
            league = item.get("league") or {}
            country = item.get("country") or {}
            seasons = sorted(
                {
                    int(season["year"])
                    for season in item.get("seasons", [])
                    if season.get("year") is not None
                }
            )
            if league.get("id") is None:
                continue
            competitions.append(
                {
                    "league_id": int(league["id"]),
                    "name": league.get("name") or "Unknown competition",
                    "country": country.get("name") or "World",
                    "seasons": seasons,
                }
            )
        return competitions

    def get_fixture_statistics(self, fixture_id: int) -> list[dict[str, Any]]:
        rows = []
        for item in self._request("/fixtures/statistics", fixture=fixture_id):
            rows.append(
                {
                    "team": _team(item.get("team")),
                    "statistics": {
                        stat["type"]: stat.get("value")
                        for stat in item.get("statistics", [])
                    },
                    "raw": item,
                }
            )
        return rows

    def get_fixture_players(self, fixture_id: int) -> list[dict[str, Any]]:
        rows = []
        for team_entry in self._request("/fixtures/players", fixture=fixture_id):
            team = _team(team_entry.get("team"))
            for item in team_entry.get("players", []):
                stats = (item.get("statistics") or [{}])[0]
                rows.append(
                    {
                        "team": team,
                        "player": {
                            "provider_id": int(item["player"]["id"]),
                            "name": item["player"].get("name") or "Unknown player",
                        },
                        "appearance": {
                            "minutes": stats.get("games", {}).get("minutes"),
                            "position": stats.get("games", {}).get("position"),
                            "substitute": bool(stats.get("games", {}).get("substitute")),
                            "shots": stats.get("shots", {}).get("total"),
                            "shots_on_target": stats.get("shots", {}).get("on"),
                            "goals": stats.get("goals", {}).get("total"),
                            "assists": stats.get("goals", {}).get("assists"),
                            "saves": stats.get("goals", {}).get("saves"),
                            "key_passes": stats.get("passes", {}).get("key"),
                            "tackles": stats.get("tackles", {}).get("total"),
                            "interceptions": stats.get("tackles", {}).get("interceptions"),
                            "yellow_cards": stats.get("cards", {}).get("yellow"),
                            "red_cards": stats.get("cards", {}).get("red"),
                        },
                        "raw": item,
                    }
                )
        return rows

    def get_lineups(self, fixture_id: int) -> list[dict[str, Any]]:
        rows = []
        for item in self._request("/fixtures/lineups", fixture=fixture_id):
            rows.append(
                {
                    "team": _team(item.get("team")),
                    "formation": item.get("formation"),
                    "starters": [
                        {
                            "provider_id": int(entry["player"]["id"]),
                            "name": entry["player"].get("name") or "Unknown player",
                            "position": entry["player"].get("pos"),
                        }
                        for entry in item.get("startXI", [])
                    ],
                    "substitutes": [
                        {
                            "provider_id": int(entry["player"]["id"]),
                            "name": entry["player"].get("name") or "Unknown player",
                            "position": entry["player"].get("pos"),
                        }
                        for entry in item.get("substitutes", [])
                    ],
                    "raw": item,
                }
            )
        return rows


class SampleSportsProvider(SportsProvider):
    name = "sample"

    def __init__(
        self,
        sample_path: Path = SAMPLE_DATA,
        logger: logging.Logger | None = None,
        ignore_date: bool = False,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.payload = json.loads(sample_path.read_text())
        self.ignore_date = ignore_date

    @staticmethod
    def _convert(value: Any) -> Any:
        if isinstance(value, list):
            return [SampleSportsProvider._convert(item) for item in value]
        if isinstance(value, dict):
            converted = {}
            for key, item in value.items():
                if " " in key:
                    converted[key] = SampleSportsProvider._convert(item)
                    continue
                snake = []
                for character in key:
                    if character.isupper():
                        snake.extend(("_", character.lower()))
                    else:
                        snake.append(character)
                converted["".join(snake)] = SampleSportsProvider._convert(item)
            return converted
        return value

    def get_completed_matches(self, date: str) -> list[dict[str, Any]]:
        if self.ignore_date:
            self.logger.info(
                "[provider:sample] Loading all local sample completed matches"
            )
        else:
            self.logger.info("[provider:sample] Loading completed matches for %s", date)
        return [
            self._convert(match)
            for match in self.payload["matches"]
            if self.ignore_date or match["date"].startswith(date)
        ]

    def get_completed_matches_range(
        self,
        date_from: str,
        date_to: str,
        league_id: int,
        season: int,
    ) -> list[dict[str, Any]]:
        del league_id, season
        return [
            self._convert(match)
            for match in self.payload["matches"]
            if date_from <= match["date"][:10] <= date_to
        ]

    def get_fixture_statistics(self, fixture_id: int) -> list[dict[str, Any]]:
        return self._convert(self.payload["statistics"].get(str(fixture_id), []))

    def get_fixture_players(self, fixture_id: int) -> list[dict[str, Any]]:
        return self._convert(self.payload["players"].get(str(fixture_id), []))

    def get_lineups(self, fixture_id: int) -> list[dict[str, Any]]:
        return self._convert(self.payload["lineups"].get(str(fixture_id), []))


def create_sports_provider(
    env: dict[str, str],
    logger: logging.Logger | None = None,
    force_sample: bool = False,
) -> SportsProvider:
    logger = logger or logging.getLogger(__name__)
    sample_configured = force_sample or _enabled(env.get("INGESTION_USE_SAMPLE"))
    if sample_configured:
        logger.info(
            "[provider] Sample mode explicitly enabled; API-Football will not be contacted"
        )
        return SampleSportsProvider(logger=logger, ignore_date=True)
    provider_name = env.get("SPORTS_PROVIDER", "api_football")
    if provider_name != "api_football":
        raise ValueError(f"Unsupported SPORTS_PROVIDER: {provider_name}")
    api_key = env.get("API_FOOTBALL_KEY")
    if not api_key:
        production = _enabled(env.get("RENDER")) or (
            env.get("APP_ENV", "").strip().lower() == "production"
        )
        if production:
            raise ValueError(
                "API_FOOTBALL_KEY is required in production. Set "
                "INGESTION_USE_SAMPLE=true only for an intentional sample run."
            )
        logger.warning(
            "[provider] API_FOOTBALL_KEY is missing; using local sample data"
        )
        return SampleSportsProvider(logger=logger)
    logger.info("[provider] Using API-Football")
    league_id = int(env.get("API_FOOTBALL_LEAGUE_ID", "1"))
    season_value = env.get("API_FOOTBALL_SEASON")
    season = int(season_value) if season_value else None
    request_delay = float(env.get("API_FOOTBALL_REQUEST_DELAY_SECONDS", "1.0"))
    logger.info(
        "[provider] Fixture filter: league=%s, season=%s, request delay=%.2fs",
        league_id,
        season or "date year",
        request_delay,
    )
    return ApiFootballProvider(
        api_key=api_key,
        base_url=env.get(
            "API_FOOTBALL_BASE_URL",
            "https://v3.football.api-sports.io",
        ),
        league_id=league_id,
        season=season,
        request_delay_seconds=request_delay,
        logger=logger,
    )
