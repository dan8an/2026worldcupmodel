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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams
from scripts.database import create_database_engine
from scripts.run_simulations import _is_completed_match, _parse_timestamp, _stage_from_value

LOGGER = logging.getLogger("repair_wc26_group_matches")
OFFICIAL_IDS = tuple(f"WC26-{number:03d}" for number in range(1, 73))
GROUP_START = datetime(2026, 6, 1, tzinfo=timezone.utc)
GROUP_END = datetime(2026, 6, 28, tzinfo=timezone.utc)


def _normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


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


@dataclass(frozen=True)
class RepairAction:
    official_id: str
    fixture: Any
    keeper: dict[str, Any] | None
    score_source: dict[str, Any] | None
    duplicates: tuple[dict[str, Any], ...]


def plan_repairs(
    rows: list[dict[str, Any]], team_ids: dict[str, Any]
) -> list[RepairAction]:
    """Return deterministic repair actions; exposed separately for testing."""
    actions = []
    claimed: set[Any] = set()
    for fixture in sorted(build_fixtures(load_teams()), key=lambda item: item.number):
        official_id = fixture.id
        candidates = []
        for row in rows:
            row_id = row.get("id")
            if row_id in claimed:
                continue
            kickoff = _parse_timestamp(row.get("kickoff") or row.get("match_date"))
            in_window = kickoff is not None and GROUP_START <= kickoff < GROUP_END
            team_match = (
                str(row.get("home_team_id")) == str(team_ids.get(fixture.home_team_id))
                and str(row.get("away_team_id")) == str(team_ids.get(fixture.away_team_id))
            )
            identified = _official_id(row) == official_id
            groupish = _stage_from_value(row.get("stage") or row.get("tournament_stage")) == "group"
            if identified or (team_match and in_window and groupish):
                candidates.append(row)
        keeper = max(candidates, key=_row_rank) if candidates else None
        scored = [row for row in candidates if _completed_with_score(row)]
        score_source = max(scored, key=_row_rank) if scored else None
        duplicates = tuple(row for row in candidates if row is not keeper)
        claimed.update(row.get("id") for row in candidates)
        actions.append(RepairAction(official_id, fixture, keeper, score_source, duplicates))
    return actions


class GroupMatchRepair:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        self.matches = Table("matches", self.metadata, schema=self.schema, autoload_with=engine)
        self.teams = Table("teams", self.metadata, schema=self.schema, autoload_with=engine)

    def _team_ids(self, connection: Connection) -> dict[str, Any]:
        canonical = {_normalized(team.name): team.id for team in load_teams()}
        result: dict[str, Any] = {}
        for row in connection.execute(select(self.teams)).mappings():
            row = dict(row)
            direct = str(row.get("id"))
            if direct in {team.id for team in load_teams()}:
                result[direct] = row["id"]
            name = row.get("name") or row.get("display_name")
            if _normalized(name) in canonical:
                result[canonical[_normalized(name)]] = row["id"]
        missing = sorted({team.id for team in load_teams()} - result.keys())
        if missing:
            raise RuntimeError(f"Cannot map canonical teams to public.teams: {missing}")
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
            actions = plan_repairs(rows, team_ids)
            if apply:
                for action in actions:
                    self._apply_action(connection, action, team_ids)
                repaired_rows = [
                    dict(row) for row in connection.execute(select(self.matches)).mappings()
                ]
                actions = plan_repairs(repaired_rows, team_ids)
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
    diagnostic_rows = rows if rows is not None else [
        row for action in actions for row in (action.keeper,) + action.duplicates if row
    ]
    dirty_scored = sorted({
        str(row.get("id")) for row in diagnostic_rows
        if _has_score(row) and _official_id(row) is None
        and _stage_from_value(row.get("stage") or row.get("tournament_stage")) == "group"
    })
    return {
        "official_completed_group_count": len(completed),
        "missing_official_group_identifiers": missing,
        "rows_with_scores_but_no_official_identifier": dirty_scored,
        "duplicate_rows_to_merge": sum(len(action.duplicates) for action in actions),
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
