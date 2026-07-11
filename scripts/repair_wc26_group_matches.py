#!/usr/bin/env python3
"""Repair the 72 canonical 2026 World Cup group fixtures in public.matches.

Seed fixtures define identity only.  Scores are copied exclusively from existing
completed database/provider rows.  Run without --apply for a diagnostic/dry run.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SAWarning

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams
from scripts.database import create_database_engine
from scripts.generate_predictions import load_team_aliases, map_database_team_ids
from scripts.run_simulations import _is_completed_match, _parse_timestamp, _stage_from_value

LOGGER = logging.getLogger("repair_wc26_group_matches")
OFFICIAL_IDS = tuple(f"WC26-{number:03d}" for number in range(1, 73))
GROUP_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
# June 27 local evening fixtures can finish after midnight on June 28 UTC.
GROUP_END = datetime(2026, 6, 29, tzinfo=timezone.utc)
FIXTURE_KICKOFF_TOLERANCE = timedelta(hours=12)
PROVIDER_REQUIRED_FIXTURE_IDS = {"WC26-008", "WC26-020"}


def _official_id(row: dict[str, Any]) -> str | None:
    raw = str(row.get("canonical_match_id") or "").upper()
    if re.fullmatch(r"WC26-0(?:0[1-9]|[1-6][0-9]|7[0-2])", raw):
        return raw
    number = row.get("match_number")
    try:
        number = int(number)
    except (TypeError, ValueError):
        return None
    return f"WC26-{number:03d}" if 1 <= number <= 72 else None


def _normalize_name(value: Any) -> str:
    return "".join(character for character in str(value or "").casefold() if character.isalnum())


def _is_wc26_provider_row(row: dict[str, Any]) -> bool:
    league = _payload(row).get("league")
    return isinstance(league, dict) and str(league.get("id")) == "1" and str(league.get("season")) == "2026"


class TeamResolver:
    def __init__(self, team_ids: dict[str, Any], database_teams: list[dict[str, Any]] | None = None):
        self.by_database_id = {
            str(database_id): canonical_id
            for canonical_id, database_id in team_ids.items()
            if database_id is not None
        }
        aliases = load_team_aliases()
        self.by_name = {
            _normalize_name(name): team.id
            for team in load_teams()
            for name in (team.id, team.name, *aliases[team.id])
        }
        self.by_provider_id: dict[str, str] = {}
        for row in database_teams or []:
            canonical_id = self.by_database_id.get(str(row.get("id")))
            provider_id = row.get("api_football_team_id")
            if canonical_id and provider_id is not None:
                self.by_provider_id[str(provider_id)] = canonical_id

    def side(self, row: dict[str, Any], side: str) -> str | None:
        canonical = self.by_database_id.get(str(row.get(f"{side}_team_id")))
        if canonical:
            return canonical
        payload_team = _payload(row).get("teams", {}).get(side, {})
        if isinstance(payload_team, dict):
            canonical = self.by_provider_id.get(str(payload_team.get("id")))
            if canonical:
                return canonical
        for value in (
            row.get(f"{side}_team"), row.get(f"{side}_team_name"),
            payload_team.get("name") if isinstance(payload_team, dict) else None,
        ):
            canonical = self.by_name.get(_normalize_name(value))
            if canonical:
                return canonical
        return None


def _has_score(row: dict[str, Any]) -> bool:
    return _score(row) is not None


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("provider_payload") or row.get("raw_payload") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _score(row: dict[str, Any]) -> tuple[int, int] | None:
    home, away = row.get("home_score"), row.get("away_score")
    if home is None or away is None:
        goals = _payload(row).get("goals")
        if isinstance(goals, dict):
            home, away = goals.get("home"), goals.get("away")
    try:
        return int(home), int(away)
    except (TypeError, ValueError):
        return None


def _completed_with_score(row: dict[str, Any]) -> bool:
    if _score(row) is None:
        return False
    if _is_completed_match(row):
        return True
    fixture = _payload(row).get("fixture")
    status = fixture.get("status") if isinstance(fixture, dict) else None
    short = str(status.get("short") if isinstance(status, dict) else "").upper()
    return short in {"FT", "AET", "PEN"}


def _row_rank(row: dict[str, Any]) -> tuple[Any, ...]:
    timestamp = _parse_timestamp(row.get("updated_at") or row.get("created_at"))
    return (
        _completed_with_score(row),
        _has_score(row),
        _official_id(row) is not None,
        timestamp or datetime.min.replace(tzinfo=timezone.utc),
    )


def _fixture_timing_reason(kickoff: datetime | None, expected: datetime) -> str | None:
    """Return the accepted timing evidence, or None when safely out of range.

    Seed fixture timestamps encode the host-date schedule and a nominal time.
    Provider timestamps are UTC, so evening host fixtures can move to the next
    UTC date. A bounded timestamp delta handles that conversion without making
    calendar-date equality part of fixture identity.
    """
    if kickoff is None:
        return None
    if kickoff == expected:
        return "exact_canonical_kickoff"
    if abs(kickoff - expected) <= FIXTURE_KICKOFF_TOLERANCE:
        return "kickoff_within_12h_tolerance"
    return None


@dataclass(frozen=True)
class CandidateEvaluation:
    row: dict[str, Any]
    accepted: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RepairAction:
    official_id: str
    fixture: Any
    keeper: dict[str, Any] | None
    score_source: dict[str, Any] | None
    duplicates: tuple[dict[str, Any], ...]
    evaluations: tuple[CandidateEvaluation, ...] = ()


def plan_repairs(
    rows: list[dict[str, Any]], team_ids: dict[str, Any],
    database_teams: list[dict[str, Any]] | None = None,
) -> list[RepairAction]:
    """Return deterministic repair actions; exposed separately for testing."""
    actions = []
    claimed: set[Any] = set()
    resolver = TeamResolver(team_ids, database_teams)
    for fixture in sorted(build_fixtures(load_teams()), key=lambda item: item.number):
        official_id = fixture.id
        candidates = []
        evaluations = []
        for row in rows:
            row_id = row.get("id")
            if row_id in claimed:
                continue
            kickoff = _parse_timestamp(row.get("kickoff") or row.get("match_date"))
            in_window = kickoff is not None and GROUP_START <= kickoff < GROUP_END
            timing_reason = _fixture_timing_reason(kickoff, fixture.kickoff)
            timing_match = timing_reason is not None
            home_id, away_id = resolver.side(row, "home"), resolver.side(row, "away")
            team_match = home_id == fixture.home_team_id and away_id == fixture.away_team_id
            identified = _official_id(row) == official_id
            groupish = _stage_from_value(row.get("stage") or row.get("tournament_stage")) == "group"
            provider_wc26 = _is_wc26_provider_row(row)
            provenance_match = (
                provider_wc26
                if official_id in PROVIDER_REQUIRED_FIXTURE_IDS
                else groupish or provider_wc26
            )
            accepted = identified or (
                team_match and in_window and timing_match and provenance_match
            )
            reasons = []
            if identified:
                reasons.append("official_identifier")
            if not team_match:
                reasons.append(f"team_mismatch:{home_id or 'unknown'}_vs_{away_id or 'unknown'}")
            if not in_window:
                reasons.append("outside_group_window")
            elif not timing_match:
                reasons.append("outside_kickoff_tolerance")
            else:
                reasons.append(timing_reason)
            if not provenance_match:
                reasons.append("no_group_stage_provenance")
            if groupish:
                reasons.append("group_stage")
            if provider_wc26:
                reasons.append("provider_world_cup_2026")
            expected_side = (
                home_id in {fixture.home_team_id, fixture.away_team_id}
                or away_id in {fixture.home_team_id, fixture.away_team_id}
            )
            relevant = identified or team_match or (
                in_window and expected_side and (groupish or provider_wc26)
            )
            if relevant:
                evaluations.append(CandidateEvaluation(row, accepted, tuple(reasons)))
            if accepted:
                candidates.append(row)
        identified_candidates = [row for row in candidates if _official_id(row) == official_id]
        ambiguous = not identified_candidates and len(candidates) > 1
        keeper = None if ambiguous else (max(candidates, key=_row_rank) if candidates else None)
        scored = [row for row in candidates if _completed_with_score(row)]
        score_source = None if ambiguous else (max(scored, key=_row_rank) if scored else None)
        duplicates = () if ambiguous else tuple(row for row in candidates if row is not keeper)
        if not ambiguous:
            claimed.update(row.get("id") for row in candidates)
        actions.append(RepairAction(
            official_id, fixture, keeper, score_source, duplicates, tuple(evaluations)
        ))
    return actions


class GroupMatchRepair:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        # Some deployed PostgREST/Supabase index metadata is returned with a
        # literal ``dialect_options`` key. Older SQLAlchemy releases warn while
        # reconstructing that reflected index even though its columns are fine.
        # Do not traverse foreign-key tables, and suppress only that known,
        # harmless reflection warning.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Can't validate argument 'dialect_options'.*",
                category=SAWarning,
            )
            self.matches = Table(
                "matches", self.metadata, schema=self.schema, autoload_with=engine,
                resolve_fks=False,
            )
            self.teams = Table(
                "teams", self.metadata, schema=self.schema, autoload_with=engine,
                resolve_fks=False,
            )

    def _team_ids(self, connection: Connection) -> dict[str, Any]:
        rows = [dict(row) for row in connection.execute(select(self.teams)).mappings()]
        self.database_teams = rows
        result = map_database_team_ids(rows)
        missing = sorted({team.id for team in load_teams()} - result.keys())
        missing.extend(
            sorted(team_id for team_id, database_id in result.items() if database_id is None)
        )
        if missing:
            raise RuntimeError(
                f"Cannot map canonical teams to public.teams: {sorted(set(missing))}"
            )
        return result

    def _merge_references(self, connection: Connection, old_id: Any, new_id: Any) -> None:
        if self.engine.dialect.name != "postgresql":
            return
        refs = connection.execute(text("""
            select quote_ident(n.nspname) || '.' || quote_ident(c.relname) as table_name,
                   quote_ident(a.attname) as column_name
            from pg_constraint fk
            join pg_class c on c.oid = fk.conrelid
            join pg_namespace n on n.oid = c.relnamespace
            join unnest(fk.conkey) with ordinality keys(attnum, ord) on true
            join pg_attribute a on a.attrelid = c.oid and a.attnum = keys.attnum
            where fk.contype = 'f' and fk.confrelid = 'public.matches'::regclass
              and array_length(fk.conkey, 1) = 1
        """)).mappings()
        for ref in refs:
            connection.execute(
                text(f"update {ref['table_name']} set {ref['column_name']} = :new where {ref['column_name']} = :old"),
                {"new": new_id, "old": old_id},
            )

    def run(self, apply: bool = False) -> dict[str, Any]:
        required = {"canonical_match_id", "match_number", "home_team_id", "away_team_id"}
        missing = required - set(self.matches.c.keys())
        if missing:
            raise RuntimeError(
                f"matches is missing {sorted(missing)}; apply migration "
                "202607100001_wc26_group_match_identity.sql"
            )
        with self.engine.begin() as connection:
            team_ids = self._team_ids(connection)
            rows = [dict(row) for row in connection.execute(select(self.matches)).mappings()]
            actions = plan_repairs(rows, team_ids, self.database_teams)
            if apply:
                for action in actions:
                    self._apply_action(connection, action, team_ids)
                repaired_rows = [
                    dict(row) for row in connection.execute(select(self.matches)).mappings()
                ]
                actions = plan_repairs(repaired_rows, team_ids, self.database_teams)
                rows = repaired_rows
            report = diagnostic_report(actions, rows)
            return report

    def _apply_action(self, connection: Connection, action: RepairAction, team_ids: dict[str, Any]) -> None:
        columns = set(self.matches.c.keys())
        values: dict[str, Any] = {
            "canonical_match_id": action.official_id,
            "match_number": action.fixture.number,
            "home_team_id": team_ids[action.fixture.home_team_id],
            "away_team_id": team_ids[action.fixture.away_team_id],
        }
        if "stage" in columns:
            values["stage"] = "group"
        if "tournament_stage" in columns:
            values["tournament_stage"] = "Group"
        if "group_code" in columns:
            values["group_code"] = action.fixture.group
        if "venue_id" in columns:
            values["venue_id"] = action.fixture.venue_id
        if "kickoff" in columns:
            values["kickoff"] = action.fixture.kickoff
        if "match_date" in columns:
            values["match_date"] = action.fixture.kickoff
        if action.score_source:
            home_score, away_score = _score(action.score_source)  # type: ignore[misc]
            values.update(home_score=home_score, away_score=away_score)
            if "completed" in columns:
                values["completed"] = True
            if "status" in columns:
                values["status"] = action.score_source.get("status") or "completed"
        # Release canonical identity from a scheduled official duplicate before
        # promoting a completed provider row into its place.
        for duplicate in action.duplicates:
            released: dict[str, Any] = {}
            if duplicate.get("canonical_match_id") == action.official_id:
                released["canonical_match_id"] = None
            if duplicate.get("match_number") == action.fixture.number:
                released["match_number"] = None
            if released:
                connection.execute(
                    self.matches.update()
                    .where(self.matches.c.id == duplicate["id"])
                    .values(**released)
                )
        if action.keeper is None:
            if "id" in columns and not self.matches.c.id.server_default:
                values["id"] = action.official_id
            for name, value in (("home_team", action.fixture.home_team_id), ("away_team", action.fixture.away_team_id)):
                if name in columns:
                    values[name] = value
            connection.execute(self.matches.insert().values(**values))
            keeper_id = values.get("id")
        else:
            keeper_id = action.keeper["id"]
            connection.execute(self.matches.update().where(self.matches.c.id == keeper_id).values(**values))
        for duplicate in action.duplicates:
            self._merge_references(connection, duplicate["id"], keeper_id)
            connection.execute(self.matches.delete().where(self.matches.c.id == duplicate["id"]))


def diagnostic_report(
    actions: list[RepairAction], rows: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    completed = [a.official_id for a in actions if a.score_source is not None]
    missing = [a.official_id for a in actions if a.keeper is None]
    plausible_rows = {
        str(evaluation.row.get("id")): evaluation.row
        for action in actions
        for evaluation in action.evaluations
        if evaluation.accepted
    }
    dirty_scored = sorted({
        row_id for row_id, row in plausible_rows.items()
        if _has_score(row) and _official_id(row) is None
    })
    unresolved_details = []
    team_names = {team.id: team.name for team in load_teams()}
    for action in actions:
        if action.keeper is not None:
            continue
        candidates = []
        for evaluation in action.evaluations:
            row = evaluation.row
            payload = _payload(row)
            league = payload.get("league") if isinstance(payload.get("league"), dict) else {}
            fixture_payload = payload.get("fixture") if isinstance(payload.get("fixture"), dict) else {}
            provider_status = fixture_payload.get("status")
            if isinstance(provider_status, dict):
                provider_status = provider_status.get("short")
            candidates.append({
                "row_id": str(row.get("id")),
                "score": _score(row),
                "status": row.get("status") or provider_status,
                "stage": row.get("stage") or row.get("tournament_stage"),
                "kickoff": str(row.get("kickoff") or row.get("match_date") or ""),
                "provider_name": row.get("provider_name"),
                "provider_league_id": league.get("id"),
                "provider_season": league.get("season"),
                "accepted": evaluation.accepted,
                "reasons": list(evaluation.reasons),
            })
        unresolved_details.append({
            "official_id": action.official_id,
            "expected_home": f"{action.fixture.home_team_id} ({team_names[action.fixture.home_team_id]})",
            "expected_away": f"{action.fixture.away_team_id} ({team_names[action.fixture.away_team_id]})",
            "expected_date": action.fixture.kickoff.date().isoformat(),
            "candidate_row_ids": [candidate["row_id"] for candidate in candidates],
            "candidates": candidates,
        })
    return {
        "official_completed_group_count": len(completed),
        "missing_official_group_identifiers": missing,
        "rows_with_scores_but_no_official_identifier": dirty_scored,
        "duplicate_rows_to_merge": sum(len(action.duplicates) for action in actions),
        "unresolved_fixture_details": unresolved_details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the repair (default: diagnostics only)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url, connect_timeout_seconds=15)
    try:
        report = GroupMatchRepair(engine).run(apply=args.apply)
    finally:
        engine.dispose()
    for key, value in report.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
