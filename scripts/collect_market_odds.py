#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select, update
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures
from scripts.data_ingestion.providers import (
    RateLimitError,
    RequestLimitError,
    create_sports_provider,
)
from scripts.database import create_database_engine
from scripts.update_squad_availability import select_research_targets

DEFAULT_MAX_FIXTURES = 1
DEFAULT_MAX_REQUESTS = 3
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
REQUIRED_TABLES = {
    "teams",
    "matches",
    "market_odds_snapshots",
}


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def map_canonical_match_id(fixture: dict[str, Any]) -> str | None:
    home = fixture.get("canonical_home_team_code")
    away = fixture.get("canonical_away_team_code")
    candidates = [
        match
        for match in build_fixtures()
        if match.home_team_id == home and match.away_team_id == away
    ]
    if len(candidates) == 1:
        return candidates[0].id
    kickoff = _as_datetime(fixture.get("date"))
    if kickoff is None:
        return None
    same_day = [
        match
        for match in candidates
        if match.kickoff.date() == kickoff.date()
    ]
    return same_day[0].id if len(same_day) == 1 else None


def validate_odds_fixture_identity(
    fixture: dict[str, Any],
    odds_rows: list[dict[str, Any]],
) -> None:
    expected_fixture_id = int(fixture["provider_fixture_id"])
    for row in odds_rows:
        received_fixture_id = int(row["provider_fixture_id"])
        if received_fixture_id != expected_fixture_id:
            raise ValueError(
                "Odds fixture mismatch: "
                f"expected={expected_fixture_id} received={received_fixture_id}"
            )


class MarketOddsRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

    def _table(self, name: str) -> Table:
        if name not in self.tables:
            self.tables[name] = Table(
                name, self.metadata, schema=self.schema, autoload_with=self.engine
            )
        return self.tables[name]

    def assert_schema(self) -> None:
        tables = set(inspect(self.engine).get_table_names(schema=self.schema))
        missing = REQUIRED_TABLES - tables
        if missing:
            raise RuntimeError(
                f"Market v5 tables are missing: {sorted(missing)}. Apply "
                "supabase/migrations/202606110007_market_comparison_v5.sql first."
            )

    def load_diagnostic_snapshot(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        teams = self._table("teams")
        matches = self._table("matches")
        with self.engine.connect() as connection:
            return (
                [
                    dict(row)
                    for row in connection.execute(select(teams)).mappings()
                ],
                [
                    dict(row)
                    for row in connection.execute(select(matches)).mappings()
                ],
            )

    def store(
        self,
        fixtures: list[dict[str, Any]],
        odds_by_fixture: dict[int, list[dict[str, Any]]],
    ) -> int:
        table = self._table("market_odds_snapshots")
        fixture_by_provider = {
            int(fixture["provider_fixture_id"]): fixture for fixture in fixtures
        }
        written = 0
        with self.engine.begin() as connection:
            for provider_fixture_id, rows in odds_by_fixture.items():
                fixture = fixture_by_provider.get(provider_fixture_id)
                if fixture is None:
                    continue
                canonical_match_id = fixture.get("canonical_match_id")
                for row in rows:
                    collected_at = _as_datetime(row.get("collected_at")) or datetime.now(
                        timezone.utc
                    )
                    identity_values = {
                        "provider_home_team_id": fixture.get(
                            "home_provider_team_id"
                        ),
                        "provider_away_team_id": fixture.get(
                            "away_provider_team_id"
                        ),
                        "provider_home_team_name": fixture.get("home_team_name"),
                        "provider_away_team_name": fixture.get("away_team_name"),
                        "canonical_home_team_code": fixture.get(
                            "canonical_home_team_code"
                        ),
                        "canonical_away_team_code": fixture.get(
                            "canonical_away_team_code"
                        ),
                    }
                    existing = connection.execute(
                        select(table.c.id).where(
                            table.c.bookmaker == row["bookmaker"],
                            table.c.collected_at == collected_at,
                            table.c.canonical_match_id == canonical_match_id,
                        )
                    ).scalar_one_or_none()
                    if existing:
                        connection.execute(
                            update(table)
                            .where(table.c.id == existing)
                            .values(
                                **{
                                    key: value
                                    for key, value in identity_values.items()
                                    if key in table.c
                                }
                            )
                        )
                        continue
                    values = {
                        "match_id": fixture.get("fixture_id"),
                        "canonical_match_id": canonical_match_id,
                        "provider_fixture_id": provider_fixture_id,
                        **identity_values,
                        "bookmaker": row["bookmaker"],
                        "source": row.get("source") or "api_football",
                        "collected_at": collected_at,
                        "home_decimal_odds": row["home_decimal_odds"],
                        "draw_decimal_odds": row["draw_decimal_odds"],
                        "away_decimal_odds": row["away_decimal_odds"],
                        "raw_payload": row.get("raw") or {},
                    }
                    if (
                        "raw_payload" in table.c
                        and not isinstance(table.c.raw_payload.type, JSON)
                    ):
                        values["raw_payload"] = json.dumps(values["raw_payload"])
                    connection.execute(
                        table.insert().values(
                            **{
                                key: value
                                for key, value in values.items()
                                if key in table.c
                            }
                        )
                    )
                    written += 1
        return written


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect research-only 1X2 market snapshots for comparison."
    )
    parser.add_argument("--max-fixtures", type=int, default=DEFAULT_MAX_FIXTURES)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    parser.add_argument("--sample", action="store_true")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("collect_market_odds")
    logger.info(
        "[market-v5] START max_fixtures=%d max_requests=%d timeout=%.1fs",
        args.max_fixtures,
        args.max_requests,
        args.request_timeout,
    )
    if args.max_fixtures < 1 or args.max_requests < 1 or args.request_timeout <= 0:
        logger.error("[market-v5] Limits and timeout must be positive")
        return 2

    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    env = dict(os.environ)
    database_url = env.get("DATABASE_URL")
    if not database_url:
        logger.error("[market-v5] DATABASE_URL is required")
        return 2
    env["API_FOOTBALL_REQUEST_TIMEOUT_SECONDS"] = str(args.request_timeout)

    engine = create_database_engine(database_url)
    try:
        repository = MarketOddsRepository(engine)
        repository.assert_schema()
        provider = create_sports_provider(env, logger, force_sample=args.sample)
        if hasattr(provider, "max_requests"):
            provider.max_requests = args.max_requests
            provider.request_count = 0
        season = int(env.get("API_FOOTBALL_SEASON", "2026"))
        league_id = int(env.get("API_FOOTBALL_LEAGUE_ID", "1"))
        provider_fixtures = provider.get_fixtures(
            league_id=league_id,
            season=season,
        )
        database_teams, database_matches = repository.load_diagnostic_snapshot()
        _, fixtures = select_research_targets(
            provider_fixtures,
            database_teams,
            database_matches,
            now=datetime.now(timezone.utc),
            max_fixtures=args.max_fixtures,
            max_teams=args.max_fixtures * 2,
        )
        for fixture in fixtures:
            fixture["canonical_match_id"] = map_canonical_match_id(fixture)
        fixtures = [
            fixture
            for fixture in fixtures
            if fixture.get("fixture_id") is not None
            or fixture.get("canonical_match_id") is not None
        ]

        odds_by_fixture: dict[int, list[dict[str, Any]]] = {}
        request_limited = False
        for fixture in fixtures:
            provider_fixture_id = int(fixture["provider_fixture_id"])
            logger.info(
                "[market-v5] Fetching odds provider_fixture_id=%d canonical=%s",
                provider_fixture_id,
                fixture.get("canonical_match_id"),
            )
            try:
                odds_rows = provider.get_odds(
                    provider_fixture_id
                )
                validate_odds_fixture_identity(fixture, odds_rows)
                odds_by_fixture[provider_fixture_id] = odds_rows
            except (RequestLimitError, RateLimitError) as error:
                request_limited = True
                logger.warning("[market-v5] %s", error)
                break

        rows_received = sum(len(rows) for rows in odds_by_fixture.values())
        rows_written = repository.store(fixtures, odds_by_fixture)
        requests_made = int(getattr(provider, "request_count", 0))
        logger.info(
            "[market-v5] SUMMARY provider_fixtures_seen=%d "
            "selected_fixtures=%d requests_made=%d odds_rows_received=%d "
            "rows_written=%d request_limited=%s",
            len(provider_fixtures),
            len(fixtures),
            requests_made,
            rows_received,
            rows_written,
            request_limited,
        )
        return 0
    except Exception:
        logger.exception("[market-v5] FAILED")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
