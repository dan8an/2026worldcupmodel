#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data_ingestion import DataIngestionRepository, RateLimitError
from scripts.data_ingestion.providers import ApiFootballProvider
from scripts.database import create_database_engine


@dataclass
class BackfillSummary:
    fixtures_discovered: int = 0
    fixtures_skipped_complete: int = 0
    fixtures_selected: int = 0
    fixtures_processed: int = 0
    fixtures_inserted: int = 0
    fixtures_updated: int = 0
    team_stats_inserted: int = 0
    team_stats_updated: int = 0
    failed: int = 0
    rate_limited: bool = False


def load_environment() -> dict[str, str]:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    parser = argparse.ArgumentParser(
        description="Backfill completed fixtures and team statistics for v4 research.",
        epilog=(
            "Example:\n"
            "  python scripts/backfill_historical_stats.py --league-id 1 "
            "--season 2022 --date-from 2022-11-20 --date-to 2022-12-18 "
            "--max-fixtures 10"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--league-id", type=int, default=1)
    parser.add_argument("--season", type=int, default=yesterday.year)
    parser.add_argument("--date-from", default=yesterday.isoformat())
    parser.add_argument("--date-to", default=yesterday.isoformat())
    parser.add_argument(
        "--max-fixtures",
        type=int,
        default=5,
        help="Maximum fixture-stat detail requests (default: 5).",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.league_id < 1:
        raise ValueError("--league-id must be at least 1")
    if args.season < 1900:
        raise ValueError("--season must be at least 1900")
    if args.max_fixtures < 1:
        raise ValueError("--max-fixtures must be at least 1")
    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    if date_from > date_to:
        raise ValueError("--date-from cannot be after --date-to")


def run_backfill(
    args: argparse.Namespace,
    provider: Any,
    repository: Any,
    logger: logging.Logger,
) -> BackfillSummary:
    summary = BackfillSummary()
    try:
        fixtures = provider.get_completed_matches_range(
            args.date_from,
            args.date_to,
            args.league_id,
            args.season,
        )
    except RateLimitError as error:
        summary.rate_limited = True
        logger.warning(
            "[historical-backfill] RATE LIMITED during fixture discovery: %s. "
            "No data was changed.",
            error,
        )
        return summary

    fixtures.sort(
        key=lambda fixture: (
            fixture.get("date") or "",
            fixture["provider_fixture_id"],
        )
    )
    summary.fixtures_discovered = len(fixtures)
    complete_fixture_ids = (
        repository.find_provider_fixtures_with_complete_team_stats(
            [fixture["provider_fixture_id"] for fixture in fixtures]
        )
    )
    summary.fixtures_skipped_complete = len(complete_fixture_ids)
    selected = [
        fixture
        for fixture in fixtures
        if fixture["provider_fixture_id"] not in complete_fixture_ids
    ][: args.max_fixtures]
    summary.fixtures_selected = len(selected)

    for fixture in selected:
        fixture_id = fixture["provider_fixture_id"]
        logger.info(
            "[historical-backfill] Fetching fixture %s: %s vs %s",
            fixture_id,
            fixture["home_team"]["name"],
            fixture["away_team"]["name"],
        )
        try:
            statistics = provider.get_fixture_statistics(fixture_id)
            result = repository.ingest_historical_team_fixture(fixture, statistics)
        except RateLimitError as error:
            summary.rate_limited = True
            logger.warning(
                "[historical-backfill] RATE LIMITED at fixture %s: %s. "
                "Stopping; committed rows are preserved.",
                fixture_id,
                error,
            )
            break
        except Exception:
            summary.failed += 1
            logger.exception("[historical-backfill] Fixture %s failed", fixture_id)
            continue

        summary.fixtures_processed += 1
        summary.fixtures_inserted += result["fixtures_inserted"]
        summary.fixtures_updated += result["fixtures_updated"]
        summary.team_stats_inserted += result["team_stats_inserted"]
        summary.team_stats_updated += result["team_stats_updated"]

    return summary


def log_summary(summary: BackfillSummary, logger: logging.Logger) -> None:
    logger.info(
        "[historical-backfill] SUMMARY fixtures_discovered=%d "
        "fixtures_skipped_complete=%d fixtures_selected=%d "
        "fixtures_processed=%d fixtures_inserted=%d "
        "fixtures_updated=%d team_stat_rows_inserted=%d "
        "team_stat_rows_updated=%d failed=%d rate_limited=%s",
        summary.fixtures_discovered,
        summary.fixtures_skipped_complete,
        summary.fixtures_selected,
        summary.fixtures_processed,
        summary.fixtures_inserted,
        summary.fixtures_updated,
        summary.team_stats_inserted,
        summary.team_stats_updated,
        summary.failed,
        summary.rate_limited,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("backfill_historical_stats")
    try:
        validate_args(args)
    except ValueError as error:
        logger.error("[historical-backfill] FAILED: %s", error)
        return 2

    env = load_environment()
    database_url = env.get("DATABASE_URL")
    api_key = env.get("API_FOOTBALL_KEY")
    if not database_url or not api_key:
        logger.error(
            "[historical-backfill] FAILED: DATABASE_URL and API_FOOTBALL_KEY "
            "are required"
        )
        return 2

    try:
        provider = ApiFootballProvider(
            api_key=api_key,
            base_url=env.get(
                "API_FOOTBALL_BASE_URL",
                "https://v3.football.api-sports.io",
            ),
            league_id=args.league_id,
            season=args.season,
            request_delay_seconds=float(
                env.get("API_FOOTBALL_REQUEST_DELAY_SECONDS", "1.0")
            ),
            logger=logger,
        )
        engine = create_database_engine(database_url)
    except (TypeError, ValueError) as error:
        logger.error("[historical-backfill] FAILED: %s", error)
        return 2
    except Exception:
        logger.exception("[historical-backfill] FAILED during initialization")
        return 1

    repository = DataIngestionRepository(engine, logger)
    try:
        repository.assert_schema()
        logger.info(
            "[historical-backfill] START league=%d season=%d from=%s to=%s "
            "max_fixtures=%d request_delay=%.2fs",
            args.league_id,
            args.season,
            args.date_from,
            args.date_to,
            args.max_fixtures,
            provider.request_delay_seconds,
        )
        summary = run_backfill(args, provider, repository, logger)
        log_summary(summary, logger)
        return 1 if summary.failed else 0
    except Exception:
        logger.exception("[historical-backfill] FAILED: unexpected error")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
