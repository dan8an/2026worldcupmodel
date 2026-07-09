#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data_ingestion import (
    DataIngestionRepository,
    RateLimitError,
    create_sports_provider,
)
from scripts.database import create_database_engine

KNOCKOUT_STAGE_KEYWORDS = {
    "round of 32",
    "round_of_32",
    "round of 16",
    "round_of_16",
    "quarter",
    "semi",
    "third",
    "3rd",
    "final",
}


def is_knockout_fixture(match: dict) -> bool:
    stage = str(match.get("round") or match.get("tournament_stage") or "").lower()
    return any(keyword in stage for keyword in KNOCKOUT_STAGE_KEYWORDS)


def load_environment() -> dict[str, str]:
    """Load local server-side env files without overriding exported values."""
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update raw international football match data.",
        epilog=(
            "Examples:\n"
            "  python scripts/update_data.py "
            "--date 2026-06-09 --max-fixtures 3\n"
            "  python scripts/update_data.py --sample"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date",
        default=None,
        help=(
            "First completed-match date in YYYY-MM-DD format. By default the "
            "cron checks yesterday through today UTC."
        ),
    )
    parser.add_argument(
        "--date-to",
        default=None,
        help=(
            "Last completed-match date in YYYY-MM-DD format. Defaults to the "
            "same day when --date is explicit, otherwise today UTC."
        ),
    )
    parser.add_argument(
        "--max-fixtures",
        type=int,
        default=5,
        help="Maximum fixtures to fetch detailed stats for (default: 5).",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help=(
            "Load all local sample fixtures and stats without calling "
            "API-Football."
        ),
    )
    args = parser.parse_args()
    today = datetime.now(timezone.utc).date()
    if args.date is None:
        args.date = (today - timedelta(days=1)).isoformat()
        args.date_to = args.date_to or today.isoformat()
    else:
        args.date_to = args.date_to or args.date
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("update_data")
    if args.max_fixtures < 1:
        logger.error("--max-fixtures must be at least 1")
        return 2
    try:
        date_from = date.fromisoformat(args.date)
        date_to = date.fromisoformat(args.date_to)
    except ValueError:
        logger.error("--date and --date-to must use YYYY-MM-DD format")
        return 2
    if date_to < date_from:
        logger.error("--date-to must be on or after --date")
        return 2

    env = load_environment()
    database_url = env.get("DATABASE_URL")
    if not database_url:
        logger.error("[daily-ingestion] FAILED: DATABASE_URL is required")
        return 2

    try:
        provider = create_sports_provider(env, logger, force_sample=args.sample)
        engine = create_database_engine(database_url)
    except (TypeError, ValueError) as error:
        logger.error("[daily-ingestion] FAILED: %s", error)
        return 2
    except Exception:
        logger.exception("[daily-ingestion] FAILED: could not initialize ingestion")
        return 1

    repository = DataIngestionRepository(engine, logger)

    try:
        logger.info(
            "[daily-ingestion] START dates=%s..%s provider=%s max_fixtures=%d",
            args.date,
            args.date_to,
            provider.name,
            args.max_fixtures,
        )
        logger.info("[step 1/6] Validating the daily pipeline schema")
        repository.assert_schema()

        logger.info(
            "[step 2/6] Loading completed matches for %s from %s",
            (
                "the local sample dataset"
                if args.sample
                else f"{args.date} through {args.date_to}"
            ),
            provider.name,
        )
        if not args.sample:
            logger.info(
                "API fixture-detail limit: %d",
                args.max_fixtures,
            )
        try:
            if args.sample:
                completed_matches = provider.get_completed_matches(args.date)
            else:
                completed_matches = provider.get_completed_matches_range(
                    args.date,
                    args.date_to,
                    getattr(provider, "league_id", 1),
                    getattr(provider, "season", None) or date_to.year,
                )
        except RateLimitError as error:
            logger.warning(
                "[daily-ingestion] RATE LIMITED during fixture discovery: %s. "
                "No data was changed; exiting successfully for the next cron run.",
                error,
            )
            return 0
        logger.info("Provider returned %d completed matches", len(completed_matches))
        logger.info("[step 3/6] Loading real provider knockout fixtures")
        if hasattr(provider, "get_fixtures"):
            try:
                provider_fixtures = provider.get_fixtures(
                    league_id=getattr(provider, "league_id", 1),
                    season=getattr(provider, "season", None) or date_to.year,
                )
            except RateLimitError as error:
                logger.warning(
                    "[daily-ingestion] RATE LIMITED during knockout fixture discovery: %s. "
                    "Proceeding with completed fixtures already discovered.",
                    error,
                )
                provider_fixtures = []
        else:
            logger.info(
                "Provider %s does not expose fixture listing; skipping upcoming knockout discovery",
                provider.name,
            )
            provider_fixtures = []
        real_knockout_fixtures = [
            match for match in provider_fixtures if is_knockout_fixture(match)
        ]
        upcoming_knockouts = [
            match
            for match in real_knockout_fixtures
            if str(match.get("status") or "").strip().upper()
            not in {"FT", "AET", "PEN"}
        ]
        skipped_non_knockout = len(provider_fixtures) - len(real_knockout_fixtures)
        logger.info(
            "Real knockout fixtures loaded=%d upcoming=%d skipped_non_knockout=%d",
            len(real_knockout_fixtures),
            len(upcoming_knockouts),
            skipped_non_knockout,
        )
        if upcoming_knockouts:
            logger.info(
                "Real upcoming knockout fixture IDs: %s",
                ", ".join(str(match["provider_fixture_id"]) for match in upcoming_knockouts),
            )

        logger.info("[step 4/6] Upserting provider teams and real fixtures")
        fixtures_to_upsert = {
            match["provider_fixture_id"]: match
            for match in [*completed_matches, *real_knockout_fixtures]
        }
        if not fixtures_to_upsert:
            logger.info(
                "[daily-ingestion] SUCCESS: no completed matches or real "
                "knockout fixtures found for %s",
                (
                    args.date
                    if args.date == args.date_to
                    else f"{args.date} through {args.date_to}"
                ),
            )
            return 0
        repository.upsert_provider_matches(list(fixtures_to_upsert.values()))
        fixtures_by_id = {
            match["provider_fixture_id"]: match for match in completed_matches
        }

        logger.info("[step 5/6] Identifying completed matches missing raw stats")
        missing = repository.find_completed_matches_missing_stats(
            list(fixtures_by_id)
        )
        total_missing = len(missing)
        missing = missing if args.sample else missing[: args.max_fixtures]
        logger.info(
            "%d completed matches require team or player stats; processing %d",
            total_missing,
            len(missing),
        )
        if not missing:
            logger.info(
                "[daily-ingestion] SUCCESS: all %d completed matches already "
                "have team and player stats; real knockout fixtures upserted=%d",
                len(completed_matches),
                len(real_knockout_fixtures),
            )
            return 0

        succeeded = 0
        failed = 0
        rate_limited = False
        for match in missing:
            fixture_id = match["api_football_fixture_id"]
            fixture = fixtures_by_id[fixture_id]
            logger.info(
                "[step 5/6] Fetching fixture %s: %s vs %s",
                fixture_id,
                match["home_team"],
                match["away_team"],
            )
            try:
                statistics = provider.get_fixture_statistics(fixture_id)
                players = provider.get_fixture_players(fixture_id)
                lineups = provider.get_lineups(fixture_id)
                result = repository.ingest_fixture(
                    match,
                    fixture,
                    statistics,
                    players,
                    lineups,
                )
                succeeded += 1
                logger.info(
                    "Stored fixture %s: %d team-stat rows, %d player-stat rows",
                    fixture_id,
                    result["team_stats"],
                    result["player_stats"],
                )
            except RateLimitError as error:
                rate_limited = True
                logger.warning(
                    "%s while fetching fixture %s. Stopping further API requests; "
                    "already committed fixtures remain saved.",
                    error,
                    fixture_id,
                )
                break
            except Exception:
                failed += 1
                logger.exception("Fixture %s failed", fixture_id)

        logger.info(
            "[step 6/6] Finished: %d completed fixtures updated, %d failed, "
            "rate_limited=%s, real_knockout_fixtures=%d",
            succeeded,
            failed,
            rate_limited,
            len(real_knockout_fixtures),
        )
        if failed:
            logger.error(
                "[daily-ingestion] FAILED: %d fixture updates failed; "
                "%d completed successfully",
                failed,
                succeeded,
            )
            return 1
        if rate_limited:
            logger.warning(
                "[daily-ingestion] PARTIAL SUCCESS: %d fixtures updated before "
                "rate limiting; remaining fixtures will be retried next run",
                succeeded,
            )
            return 0
        logger.info(
            "[daily-ingestion] SUCCESS: %d fixtures updated",
            succeeded,
        )
        return 0
    except RateLimitError as error:
        logger.warning(
            "[daily-ingestion] RATE LIMITED: %s. Existing data was preserved; "
            "exiting successfully for the next cron run.",
            error,
        )
        return 0
    except Exception:
        logger.exception("[daily-ingestion] FAILED: unexpected ingestion error")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
