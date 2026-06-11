#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams, validate_tournament
from scripts.database import create_database_engine
from scripts.generate_predictions import PredictionRepository, canonical_prior_elo
from scripts.run_simulations import (
    SimulationRepository,
    build_knockout_prediction_provider,
    build_round_of_32,
    compile_score_sampler,
    knockout_winner,
    load_environment,
    rank_group,
    sample_group_score,
)

REPORT_PATH = ROOT / "data" / "evaluation" / "simulation_diagnostics_latest.json"
FOCUS_TEAM_ID = "CZE"
GROUPS = "ABCDEFGHIJKL"
PROBABILITY_FIELDS = (
    "round_of_32_probability",
    "round_of_16_probability",
    "quarterfinal_probability",
    "semifinal_probability",
    "final_probability",
    "champion_probability",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diagnose the latest persisted tournament simulation."
    )
    parser.add_argument(
        "--simulations",
        type=int,
        help="Replay iteration count (defaults to the persisted run count).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPORT_PATH,
        help="JSON report path.",
    )
    return parser.parse_args()


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _normalize_name(value: Any) -> str:
    return "".join(
        character for character in str(value or "").lower() if character.isalnum()
    )


def _probability(row: dict[str, Any], field: str) -> float:
    value = row.get(field)
    return float(value) if value is not None else 0.0


def _knockout_home_probability(
    prediction: dict[str, Any],
) -> dict[str, float]:
    home_regulation = _probability(prediction, "home_win_probability")
    draw_probability = _probability(prediction, "draw_probability")
    away_regulation = _probability(prediction, "away_win_probability")
    decisive_total = home_regulation + away_regulation
    decisive_home = home_regulation / decisive_total if decisive_total else 0.5
    penalty_home = 0.5 + 0.35 * (decisive_home - 0.5)
    home_advance = (
        home_regulation
        + draw_probability
        * (0.40 * decisive_home + 0.60 * penalty_home)
    )
    return {
        "regulation_draw_probability": draw_probability,
        "decisive_home_probability": decisive_home,
        "penalty_home_probability": penalty_home,
        "home_advance_probability": home_advance,
        "away_advance_probability": 1.0 - home_advance,
    }


def load_latest_run(
    engine: Engine,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    repository = SimulationRepository(engine)
    repository.assert_schema()
    runs = repository._table("simulation_runs")
    results = repository._table("team_simulation_results")
    predictions = repository._table("predictions")
    with engine.connect() as connection:
        run = connection.execute(
            select(runs).order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(1)
        ).mappings().one_or_none()
        if run is None:
            raise RuntimeError("No simulation run is available")
        result_rows = [
            dict(row)
            for row in connection.execute(
                select(results).where(results.c.simulation_run_id == run["id"])
            ).mappings()
        ]
        prediction_rows = [
            dict(row)
            for row in connection.execute(
                select(predictions).where(
                    predictions.c.model_run_id == run["model_run_id"]
                )
            ).mappings()
            if row.get("canonical_match_id")
        ]
    return (
        dict(run),
        result_rows,
        {str(row["canonical_match_id"]): row for row in prediction_rows},
    )


def load_previous_simulation_results(
    engine: Engine,
    latest_run_id: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    repository = SimulationRepository(engine)
    runs = repository._table("simulation_runs")
    results = repository._table("team_simulation_results")
    with engine.connect() as connection:
        previous = connection.execute(
            select(runs)
            .where(runs.c.id != latest_run_id)
            .order_by(runs.c.created_at.desc(), runs.c.id.desc())
            .limit(1)
        ).mappings().one_or_none()
        if previous is None:
            return None
        rows = [
            dict(row)
            for row in connection.execute(
                select(results).where(
                    results.c.simulation_run_id == previous["id"]
                )
            ).mappings()
        ]
    return dict(previous), rows


def champion_probability_comparison(
    before_run: dict[str, Any],
    before_rows: list[dict[str, Any]],
    after_run: dict[str, Any],
    after_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    teams = {team.id: team.name for team in load_teams()}
    before = {
        str(row["team_id"]): _probability(row, "champion_probability")
        for row in before_rows
    }
    after = {
        str(row["team_id"]): _probability(row, "champion_probability")
        for row in after_rows
    }

    def ranked(values: dict[str, float]) -> list[dict[str, Any]]:
        return [
            {
                "team_id": team_id,
                "team_name": teams[team_id],
                "champion_probability": probability,
            }
            for team_id, probability in sorted(
                values.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:20]
        ]

    changes = [
        {
            "team_id": team_id,
            "team_name": teams[team_id],
            "before": before.get(team_id, 0.0),
            "after": after.get(team_id, 0.0),
            "change": after.get(team_id, 0.0) - before.get(team_id, 0.0),
        }
        for team_id in teams
    ]
    return {
        "before_run_id": before_run["id"],
        "before_created_at": before_run.get("created_at"),
        "after_run_id": after_run["id"],
        "after_created_at": after_run.get("created_at"),
        "before_top_20": ranked(before),
        "after_top_20": ranked(after),
        "biggest_risers": sorted(
            changes, key=lambda row: row["change"], reverse=True
        )[:10],
        "biggest_fallers": sorted(changes, key=lambda row: row["change"])[:10],
    }


def mapping_diagnostics(engine: Engine) -> dict[str, Any]:
    repository = PredictionRepository(engine)
    current_mapping = repository.load_database_team_ids()
    metadata = MetaData()
    schema = None if engine.dialect.name == "sqlite" else "public"
    teams_table = Table("teams", metadata, schema=schema, autoload_with=engine)
    with engine.connect() as connection:
        database_teams = [
            dict(row) for row in connection.execute(select(teams_table)).mappings()
        ]

    aliases = json.loads((ROOT / "data" / "seed" / "team_aliases.json").read_text())
    database_by_name = {
        _normalize_name(row.get("name")): row for row in database_teams if row.get("name")
    }
    missing = []
    for team in load_teams():
        if current_mapping.get(team.id) is not None:
            continue
        alias_matches = [
            database_by_name[_normalize_name(alias)]
            for alias in aliases.get(team.id, [])
            if _normalize_name(alias) in database_by_name
        ]
        missing.append(
            {
                "team_id": team.id,
                "team_name": team.name,
                "canonical_prior_elo": canonical_prior_elo(team.rank),
                "available_alias_matches": [
                    {"database_team_id": row["id"], "database_name": row["name"]}
                    for row in alias_matches
                ],
            }
        )

    ratings = repository.load_current_team_ratings(current_mapping)
    mapped_elos = [
        float(ratings[team.id]["elo_rating"])
        for team in load_teams()
        if current_mapping.get(team.id) is not None
    ]
    mapped_elos.sort()
    mapped_median = mapped_elos[len(mapped_elos) // 2] if mapped_elos else None
    rating_sources = Counter(
        str(ratings[team.id].get("_rating_source")) for team in load_teams()
    )
    database_name_counts = Counter(
        _normalize_name(row.get("name"))
        for row in database_teams
        if row.get("name")
    )
    mapped_database_ids = Counter(
        str(database_id)
        for database_id in current_mapping.values()
        if database_id is not None
    )
    return {
        "canonical_team_count": len(load_teams()),
        "database_team_count": len(database_teams),
        "mapped_team_count": sum(
            current_mapping.get(team.id) is not None for team in load_teams()
        ),
        "unmapped_team_count": len(missing),
        "unmapped_teams": missing,
        "mapped_database_elo_median": mapped_median,
        "rating_source_counts": dict(rating_sources),
        "teams_by_rating_source": {
            source: [
                team.id
                for team in load_teams()
                if ratings[team.id].get("_rating_source") == source
            ]
            for source in sorted(rating_sources)
        },
        "duplicate_normalized_database_names": [
            name for name, count in database_name_counts.items() if count > 1
        ],
        "canonical_mapping_collisions": [
            database_id
            for database_id, count in mapped_database_ids.items()
            if count > 1
        ],
        "focus_team": {
            "team_id": FOCUS_TEAM_ID,
            "database_team_id": current_mapping.get(FOCUS_TEAM_ID),
            "rating": {
                key: ratings[FOCUS_TEAM_ID].get(key)
                for key in (
                    "elo_rating",
                    "attack_rating",
                    "defense_rating",
                    "form_rating",
                    "matches_played",
                    "_rating_source",
                )
            },
        },
    }


def validate_persisted_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sums = {
        field: sum(_probability(row, field) for row in rows)
        for field in PROBABILITY_FIELDS
    }
    expected_sums = dict(zip(PROBABILITY_FIELDS, (32, 16, 8, 4, 2, 1)))
    monotonic_violations = []
    for row in rows:
        values = [_probability(row, field) for field in PROBABILITY_FIELDS]
        if any(left + 1e-12 < right for left, right in zip(values, values[1:])):
            monotonic_violations.append(str(row["team_id"]))
    return {
        "team_count": len(rows),
        "stage_probability_sums": sums,
        "expected_stage_probability_sums": expected_sums,
        "stage_sum_checks_pass": all(
            abs(sums[field] - expected) < 1e-8
            for field, expected in expected_sums.items()
        ),
        "monotonic_checks_pass": not monotonic_violations,
        "monotonic_violations": monotonic_violations,
        "champion_probability_sum": sums["champion_probability"],
    }


def replay_with_diagnostics(
    predictions: dict[str, dict[str, Any]],
    num_simulations: int,
    seed: int,
    knockout_prediction: Any,
) -> dict[str, Any]:
    teams = load_teams()
    teams_by_id = {team.id: team for team in teams}
    fixtures = build_fixtures(teams)
    validate_tournament(teams, fixtures)
    group_fixtures = [fixture for fixture in fixtures if fixture.stage == "group"]
    missing = [fixture.id for fixture in group_fixtures if fixture.id not in predictions]
    if missing:
        raise ValueError(f"Prediction run is missing {len(missing)} group fixtures")

    team_groups = {team.id: team.group for team in teams}
    score_samplers = {
        fixture.id: compile_score_sampler(predictions[fixture.id])
        for fixture in group_fixtures
    }
    counts = {
        stage: Counter()
        for stage in (
            "round_of_32",
            "round_of_16",
            "quarterfinal",
            "semifinal",
            "final",
            "champion",
        )
    }
    finish_counts: Counter[int] = Counter()
    finish_title_counts: Counter[int] = Counter()
    r32_opponents: Counter[str] = Counter()
    r32_opponent_titles: Counter[str] = Counter()
    knockout_opponents: dict[str, Counter[str]] = defaultdict(Counter)
    knockout_attempts: Counter[str] = Counter()
    knockout_wins: Counter[str] = Counter()
    same_group_rematches: Counter[str] = Counter()
    iterations_with_same_group_rematch = 0
    rng = random.Random(seed)

    def resolve(stage: str, home_id: str, away_id: str) -> str:
        if FOCUS_TEAM_ID in (home_id, away_id):
            opponent = away_id if home_id == FOCUS_TEAM_ID else home_id
            knockout_opponents[stage][opponent] += 1
            knockout_attempts[stage] += 1
        winner = knockout_winner(
            home_id,
            away_id,
            knockout_prediction(home_id, away_id),
            rng,
        )
        if winner == FOCUS_TEAM_ID:
            knockout_wins[stage] += 1
        return winner

    for _ in range(num_simulations):
        results_by_group: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
        for fixture in group_fixtures:
            home_goals, away_goals = sample_group_score(
                score_samplers[fixture.id],
                rng,
            )
            results_by_group[fixture.group].append(
                (
                    fixture.home_team_id,
                    fixture.away_team_id,
                    home_goals,
                    away_goals,
                )
            )

        group_tables = {
            group: rank_group(
                [team.id for team in teams if team.group == group],
                results_by_group[group],
            )
            for group in GROUPS
        }
        focus_finish = next(
            index + 1
            for index, row in enumerate(group_tables[team_groups[FOCUS_TEAM_ID]])
            if row["team_id"] == FOCUS_TEAM_ID
        )
        finish_counts[focus_finish] += 1

        pairings = build_round_of_32(group_tables, team_groups)
        iteration_has_same_group_rematch = False
        for home_id, away_id in pairings:
            if team_groups[home_id] == team_groups[away_id]:
                iteration_has_same_group_rematch = True
                same_group_rematches[f"{home_id}-{away_id}"] += 1
        if iteration_has_same_group_rematch:
            iterations_with_same_group_rematch += 1
        qualified = {team_id for pairing in pairings for team_id in pairing}
        counts["round_of_32"].update(qualified)
        focus_r32_opponent = None
        for home_id, away_id in pairings:
            if FOCUS_TEAM_ID in (home_id, away_id):
                focus_r32_opponent = (
                    away_id if home_id == FOCUS_TEAM_ID else home_id
                )
                r32_opponents[focus_r32_opponent] += 1
                break

        current = [
            resolve("round_of_32", home_id, away_id)
            for home_id, away_id in pairings
        ]
        counts["round_of_16"].update(current)
        for game_stage, reached_stage in (
            ("round_of_16", "quarterfinal"),
            ("quarterfinal", "semifinal"),
            ("semifinal", "final"),
        ):
            current = [
                resolve(game_stage, current[index], current[index + 1])
                for index in range(0, len(current), 2)
            ]
            counts[reached_stage].update(current)
        champion = resolve("final", current[0], current[1])
        counts["champion"].update([champion])
        if champion == FOCUS_TEAM_ID:
            finish_title_counts[focus_finish] += 1
            if focus_r32_opponent is not None:
                r32_opponent_titles[focus_r32_opponent] += 1

    probabilities = {
        team.id: {
            field: counts[field.removesuffix("_probability")][team.id]
            / num_simulations
            for field in PROBABILITY_FIELDS
        }
        for team in teams
    }
    focus_probabilities = probabilities[FOCUS_TEAM_ID]
    average_finish = sum(
        finish * count for finish, count in finish_counts.items()
    ) / num_simulations
    def opponent_rows(counter: Counter[str]) -> list[dict[str, Any]]:
        total = sum(counter.values())
        return [
            {
                "team_id": team_id,
                "team_name": teams_by_id[team_id].name,
                "count": count,
                "share": count / total if total else 0.0,
                "focus_advance_probability": _knockout_home_probability(
                    knockout_prediction(FOCUS_TEAM_ID, team_id)
                )["home_advance_probability"],
            }
            for team_id, count in counter.most_common()
        ]

    stage_conditionals = {}
    previous = focus_probabilities["round_of_32_probability"]
    for field in PROBABILITY_FIELDS[1:]:
        current_probability = focus_probabilities[field]
        stage_conditionals[field] = (
            current_probability / previous if previous else 0.0
        )
        previous = current_probability

    all_opponents = sum(knockout_opponents.values(), Counter())
    all_opponent_count = sum(all_opponents.values())
    return {
        "probabilities": probabilities,
        "focus": {
            "average_group_finish": average_finish,
            "group_finish_distribution": {
                str(finish): count / num_simulations
                for finish, count in sorted(finish_counts.items())
            },
            "round_of_32_opponents": opponent_rows(r32_opponents),
            "knockout_opponents_by_stage": {
                stage: opponent_rows(counter)
                for stage, counter in knockout_opponents.items()
            },
            "path_difficulty": {
                "average_focus_advance_probability": (
                    sum(
                        _knockout_home_probability(
                            knockout_prediction(FOCUS_TEAM_ID, team_id)
                        )["home_advance_probability"]
                        * count
                        for team_id, count in all_opponents.items()
                    )
                    / all_opponent_count
                    if all_opponent_count
                    else None
                ),
                "average_round_of_32_advance_probability": (
                    sum(
                        _knockout_home_probability(
                            knockout_prediction(FOCUS_TEAM_ID, team_id)
                        )["home_advance_probability"]
                        * count
                        for team_id, count in r32_opponents.items()
                    )
                    / sum(r32_opponents.values())
                    if r32_opponents
                    else None
                ),
            },
            "title_probability_decomposition": {
                "stage_probabilities": focus_probabilities,
                "conditional_advancement": stage_conditionals,
                "by_group_finish": [
                    {
                        "finish": finish,
                        "iterations": count,
                        "title_count": finish_title_counts[finish],
                        "title_probability_given_finish": (
                            finish_title_counts[finish] / count if count else 0.0
                        ),
                        "title_probability_contribution": (
                            finish_title_counts[finish] / num_simulations
                        ),
                    }
                    for finish, count in sorted(finish_counts.items())
                ],
                "by_round_of_32_opponent": [
                    {
                        **row,
                        "title_count": r32_opponent_titles[row["team_id"]],
                        "title_probability_given_opponent": (
                            r32_opponent_titles[row["team_id"]] / row["count"]
                            if row["count"]
                            else 0.0
                        ),
                        "title_probability_contribution": (
                            r32_opponent_titles[row["team_id"]] / num_simulations
                        ),
                    }
                    for row in opponent_rows(r32_opponents)
                ],
            },
            "knockout_resolution": {
                stage: {
                    "attempts": knockout_attempts[stage],
                    "wins": knockout_wins[stage],
                    "conditional_advance_rate": (
                        knockout_wins[stage] / knockout_attempts[stage]
                        if knockout_attempts[stage]
                        else 0.0
                    ),
                }
                for stage in (
                    "round_of_32",
                    "round_of_16",
                    "quarterfinal",
                    "semifinal",
                    "final",
                )
            },
        },
        "bracket_validation": {
            "same_group_round_of_32_rematch_count": sum(
                same_group_rematches.values()
            ),
            "iterations_with_same_group_round_of_32_rematch": (
                iterations_with_same_group_rematch
            ),
            "iteration_rate": iterations_with_same_group_rematch / num_simulations,
            "focus_team_same_group_rematch_count": sum(
                count
                for pairing, count in same_group_rematches.items()
                if FOCUS_TEAM_ID in pairing.split("-")
            ),
            "pairings": [
                {"pairing": pairing, "count": count}
                for pairing, count in same_group_rematches.most_common()
            ],
        },
    }


def group_match_diagnostics(
    predictions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    teams = {team.id: team for team in load_teams()}
    fixtures = [
        fixture
        for fixture in build_fixtures(list(teams.values()))
        if fixture.stage == "group"
        and FOCUS_TEAM_ID in (fixture.home_team_id, fixture.away_team_id)
    ]
    output = []
    for fixture in fixtures:
        prediction = predictions[fixture.id]
        output.append(
            {
                "match_id": fixture.id,
                "home_team_id": fixture.home_team_id,
                "home_team_name": teams[fixture.home_team_id].name,
                "away_team_id": fixture.away_team_id,
                "away_team_name": teams[fixture.away_team_id].name,
                "home_win_probability": _probability(
                    prediction, "home_win_probability"
                ),
                "draw_probability": _probability(prediction, "draw_probability"),
                "away_win_probability": _probability(
                    prediction, "away_win_probability"
                ),
                "home_xg": _probability(prediction, "home_xg"),
                "away_xg": _probability(prediction, "away_xg"),
            }
        )
    return output


def build_anomalies(
    persisted: dict[str, Any],
    mappings: dict[str, Any],
    replay: dict[str, Any],
    czechia_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    anomalies = []
    focus_mapping = mappings["focus_team"]
    if focus_mapping["database_team_id"] is None:
        anomalies.append(
            {
                "severity": "critical",
                "code": "focus_team_unmapped",
                "message": (
                    "CZE/Czechia is not mapped to the database team named Czech "
                    "Republic, despite that alias existing in team_aliases.json."
                ),
            }
        )
    fallback_elo = float(focus_mapping["rating"]["elo_rating"])
    median_elo = mappings["mapped_database_elo_median"]
    if median_elo is not None and abs(fallback_elo - median_elo) > 150:
        anomalies.append(
            {
                "severity": "critical",
                "code": "mixed_elo_scales",
                "message": (
                    f"Czechia uses fallback Elo {fallback_elo:.2f} while the mapped "
                    f"database-team median is {median_elo:.2f}."
                ),
            }
        )
    if any(
        (
            row["away_team_id"] == FOCUS_TEAM_ID
            and row["away_win_probability"] > 0.5
        )
        or (
            row["home_team_id"] == FOCUS_TEAM_ID
            and row["home_win_probability"] > 0.6
        )
        for row in czechia_matches
    ):
        anomalies.append(
            {
                "severity": "high",
                "code": "focus_match_probabilities_extreme",
                "message": (
                    "The v4 run makes Czechia a strong favorite in multiple group "
                    "matches; these probabilities flow into group sampling."
                ),
            }
        )
    anomalies.append(
        {
            "severity": "high",
            "code": "non_official_round_of_32_mapping",
            "message": (
                "Round-of-32 pairings use deterministic strength seeding and a "
                "greedy no-rematch rule, not the official FIFA Annex C mapping."
            ),
        }
    )
    anomalies.append(
        {
            "severity": "medium",
            "code": "incomplete_group_tiebreakers",
            "message": (
                "Group ranking implements points, goal difference, and goals "
                "scored, then falls back to team ID; official head-to-head, fair "
                "play, and drawing-lots steps are not implemented."
            ),
        }
    )
    if mappings["canonical_mapping_collisions"]:
        anomalies.append(
            {
                "severity": "critical",
                "code": "canonical_team_mapping_collision",
                "message": "Multiple canonical teams map to one database team ID.",
            }
        )
    if replay["bracket_validation"][
        "same_group_round_of_32_rematch_count"
    ]:
        count = replay["bracket_validation"][
            "same_group_round_of_32_rematch_count"
        ]
        anomalies.append(
            {
                "severity": "high",
                "code": "same_group_round_of_32_rematches",
                "message": (
                    f"The greedy pairing fallback produced {count} same-group "
                    "Round-of-32 rematches in the diagnostic replay."
                ),
            }
        )
    if not persisted["stage_sum_checks_pass"] or not persisted["monotonic_checks_pass"]:
        anomalies.append(
            {
                "severity": "critical",
                "code": "persisted_probability_invariants_failed",
                "message": "Persisted stage totals or monotonicity checks failed.",
            }
        )
    focus_replay = replay["focus"]["title_probability_decomposition"][
        "stage_probabilities"
    ]["champion_probability"]
    if focus_replay > 0.05:
        anomalies.append(
            {
                "severity": "high",
                "code": "focus_title_probability_implausible",
                "message": (
                    f"The exact replay gives Czechia a {focus_replay:.2%} title "
                    "probability."
                ),
            }
        )
    return anomalies


def main() -> int:
    args = parse_args()
    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        run, persisted_rows, predictions = load_latest_run(engine)
        previous = load_previous_simulation_results(engine, run["id"])
        iterations = args.simulations or int(
            run.get("num_simulations") or run.get("iterations") or 50_000
        )
        seed = int(run.get("random_seed") or 2026)
        prediction_repository = PredictionRepository(engine)
        database_team_ids = prediction_repository.load_database_team_ids()
        team_ratings = prediction_repository.load_current_team_ratings(
            database_team_ids
        )
        shot_volume_ratings = (
            prediction_repository.load_current_shot_volume_ratings(
                database_team_ids
            )
        )
        knockout_prediction = build_knockout_prediction_provider(
            team_ratings,
            shot_volume_ratings,
        )
        mappings = mapping_diagnostics(engine)
        persisted_validation = validate_persisted_results(persisted_rows)
        replay = replay_with_diagnostics(
            predictions,
            iterations,
            seed,
            knockout_prediction,
        )
        teams = {team.id: team for team in load_teams()}
        persisted_by_id = {str(row["team_id"]): row for row in persisted_rows}
        top_20 = sorted(
            persisted_rows,
            key=lambda row: _probability(row, "champion_probability"),
            reverse=True,
        )[:20]
        czechia_matches = group_match_diagnostics(predictions)
        persisted_focus = persisted_by_id[FOCUS_TEAM_ID]
        replay_focus = replay["focus"]["title_probability_decomposition"][
            "stage_probabilities"
        ]
        report = {
            "generated_at": datetime.now().astimezone().isoformat(),
            "source": "database_latest",
            "api_verification": {
                "simulation_run_id": run["id"],
                "model_run_id": run.get("model_run_id"),
                "model_version": run.get("model_version"),
                "created_at": run.get("created_at"),
                "team_result_rows": len(persisted_rows),
                "assessment": (
                    "/api/simulations/latest selects this newest simulation_runs "
                    "row and its team_simulation_results rows."
                ),
            },
            "tournament_structure": {
                "teams": len(load_teams()),
                "unique_team_ids": len({team.id for team in load_teams()}),
                "groups": len(GROUPS),
                "teams_per_group": {
                    group: sum(team.group == group for team in load_teams())
                    for group in GROUPS
                },
                "advancement_rule": "Top two in each group plus best eight third-place teams",
                "group_tiebreakers_implemented": [
                    "points",
                    "goal_difference",
                    "goals_for",
                    "canonical_team_id_stable_fallback",
                ],
                "round_of_32_mapping": {
                    "implementation": (
                        "12 group winners plus the four best runners are seeded "
                        "against the remaining runners and best third-place teams; "
                        "a greedy pass avoids same-group rematches."
                    ),
                    "official_fifa_annex_c": False,
                    "documented_in": "docs/DATA_AND_LIMITATIONS.md",
                },
            },
            "persisted_probability_validation": persisted_validation,
            "top_20_champion_probabilities": [
                {
                    "team_id": str(row["team_id"]),
                    "team_name": teams[str(row["team_id"])].name,
                    "champion_probability": _probability(
                        row, "champion_probability"
                    ),
                }
                for row in top_20
            ],
            "team_mapping_diagnostics": mappings,
            "czechia": {
                "team_id": FOCUS_TEAM_ID,
                "team_name": teams[FOCUS_TEAM_ID].name,
                "group": teams[FOCUS_TEAM_ID].group,
                "group_members": [
                    {"team_id": team.id, "team_name": team.name}
                    for team in load_teams()
                    if team.group == teams[FOCUS_TEAM_ID].group
                ],
                "group_match_probabilities": czechia_matches,
                "persisted_stage_probabilities": {
                    field: _probability(persisted_focus, field)
                    for field in PROBABILITY_FIELDS
                },
                "replayed_stage_probabilities": replay_focus,
                "replay_minus_persisted": {
                    field: replay_focus[field] - _probability(persisted_focus, field)
                    for field in PROBABILITY_FIELDS
                },
                **replay["focus"],
            },
            "knockout_model": {
                "matchup_definition": (
                    "Each simulated knockout matchup uses a cached neutral-site "
                    "elo-context-v4.1 prediction computed from the current team "
                    "ratings and validated shot-volume inputs."
                ),
                "draw_handling": (
                    "A modeled regulation draw is always resolved: 40% via extra "
                    "time using the pair-specific decisive probability and 60% "
                    "via penalties with that edge compressed toward 50%."
                ),
                "focus_vs_spain": {
                    "regulation": knockout_prediction(FOCUS_TEAM_ID, "ESP"),
                    "resolution": _knockout_home_probability(
                        knockout_prediction(FOCUS_TEAM_ID, "ESP")
                    ),
                },
            },
            "bracket_replay_validation": replay["bracket_validation"],
        }
        if previous is not None:
            previous_run, previous_rows = previous
            report["champion_probability_comparison"] = (
                champion_probability_comparison(
                    previous_run,
                    previous_rows,
                    run,
                    persisted_rows,
                )
            )
        report["anomalies"] = build_anomalies(
            persisted_validation,
            mappings,
            replay,
            czechia_matches,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, default=_json_default) + "\n"
        )
        print(json.dumps(report["top_20_champion_probabilities"], indent=2))
        print(
            json.dumps(
                {
                    "czechia": {
                        "group": report["czechia"]["group"],
                        "group_match_probabilities": czechia_matches,
                        "average_group_finish": report["czechia"][
                            "average_group_finish"
                        ],
                        "group_finish_distribution": report["czechia"][
                            "group_finish_distribution"
                        ],
                        "path_difficulty": report["czechia"]["path_difficulty"],
                        "title_probability_decomposition": {
                            "stage_probabilities": report["czechia"][
                                "title_probability_decomposition"
                            ]["stage_probabilities"],
                            "conditional_advancement": report["czechia"][
                                "title_probability_decomposition"
                            ]["conditional_advancement"],
                        },
                    },
                    "bracket_validation": {
                        key: value
                        for key, value in replay["bracket_validation"].items()
                        if key != "pairings"
                    },
                    "top_same_group_pairings": replay["bracket_validation"][
                        "pairings"
                    ][:10],
                    "anomalies": report["anomalies"],
                },
                indent=2,
                default=_json_default,
            )
        )
        print(f"Wrote {args.output.relative_to(ROOT)}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
