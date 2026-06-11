import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import Engine, text

from modeling.src.data import ROOT, build_fixtures, load_teams, load_venues, validate_tournament
from modeling.src.features.context import ContextRepository
from modeling.src.flags import flag_for_team
from modeling.src.poisson import predict_match
from modeling.src.simulation import (
    PUBLISHED_SIMULATION_ITERATIONS,
    prediction_dict,
    simulate_tournament,
)
from modeling.src.team_profiles import (
    form_summary,
    key_players,
    load_squad_metadata,
    load_squad_players,
    player_payload,
    recent_results,
    team_analysis,
)
from scripts.database import create_database_engine

STATIC_MODEL_VERSION = "context-0.2.0"
MODEL_VERSION = STATIC_MODEL_VERSION
LOGGER = logging.getLogger(__name__)


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class DatabasePredictionSource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def from_environment(cls) -> "DatabasePredictionSource | None":
        load_dotenv(ROOT / ".env", override=False)
        load_dotenv(ROOT / "backend" / ".env", override=False)
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            LOGGER.warning(
                "DATABASE_URL is unavailable; serving static prediction fallback"
            )
            return None
        try:
            return cls(create_database_engine(database_url))
        except Exception:
            LOGGER.warning(
                "DATABASE_URL could not initialize; serving static prediction "
                "fallback",
                exc_info=True,
            )
            return None

    def load_latest(self) -> dict[str, Any] | None:
        table_prefix = "public." if self.engine.dialect.name == "postgresql" else ""
        with self.engine.connect() as connection:
            latest = connection.execute(
                text(
                    f"""
                    select
                      mr.id as model_run_id,
                      mr.model_version,
                      mr.generated_at,
                      mr.data_cutoff
                    from {table_prefix}model_runs mr
                    where coalesce(mr.status, 'completed') = 'completed'
                      and exists (
                        select 1
                        from {table_prefix}predictions p
                        where p.model_run_id = mr.id
                          and p.canonical_match_id is not null
                      )
                    order by mr.generated_at desc nulls last, mr.id desc
                    limit 1
                    """
                )
            ).mappings().one_or_none()
            if latest is None:
                return None
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"""
                        select *
                        from {table_prefix}predictions
                        where model_run_id = :model_run_id
                          and canonical_match_id is not null
                        """
                    ),
                    {"model_run_id": latest["model_run_id"]},
                ).mappings()
            ]
        return {
            "model_run_id": latest["model_run_id"],
            "model_version": latest["model_version"],
            "generated_at": latest["generated_at"],
            "data_cutoff": latest.get("data_cutoff")
            or latest["generated_at"],
            "predictions": {
                str(row["canonical_match_id"]): row
                for row in rows
                if row.get("canonical_match_id")
            },
        }


class DatabaseSimulationSource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load_latest(self) -> dict[str, Any] | None:
        table_prefix = "public." if self.engine.dialect.name == "postgresql" else ""
        with self.engine.connect() as connection:
            latest = connection.execute(
                text(
                    f"""
                    select sr.*
                    from {table_prefix}simulation_runs sr
                    where exists (
                      select 1
                      from {table_prefix}team_simulation_results tsr
                      where tsr.simulation_run_id = sr.id
                    )
                    order by sr.created_at desc, sr.id desc
                    limit 1
                    """
                )
            ).mappings().one_or_none()
            if latest is None:
                return None
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"""
                        select *
                        from {table_prefix}team_simulation_results
                        where simulation_run_id = :simulation_run_id
                        """
                    ),
                    {"simulation_run_id": latest["id"]},
                ).mappings()
            ]
        return {"run": dict(latest), "results": rows}


class PredictionService:
    def __init__(
        self,
        prediction_source: DatabasePredictionSource | None = None,
        simulation_source: DatabaseSimulationSource | None = None,
        prediction_cache_seconds: float | None = None,
    ) -> None:
        self.generated_at = datetime.now(timezone.utc).isoformat()
        self.data_cutoff = self.generated_at
        self.teams = load_teams()
        self.teams_by_id = {team.id: team for team in self.teams}
        self.contexts = ContextRepository()
        self.fixtures = build_fixtures(self.teams)
        validate_tournament(self.teams, self.fixtures)
        self.venues = load_venues()
        self.squad_players = load_squad_players()
        self.squad_metadata = load_squad_metadata()
        self.static_predictions = {
            match.id: predict_match(
                self.teams_by_id[match.home_team_id or ""],
                self.teams_by_id[match.away_team_id or ""],
                match.id,
                context=self.contexts.for_match(
                    match.home_team_id or "",
                    match.away_team_id or "",
                    datetime.fromisoformat(self.data_cutoff),
                ),
            )
            for match in self.fixtures
            if match.stage == "group"
        }
        self.predictions = self.static_predictions
        self.prediction_source = (
            prediction_source
            if prediction_source is not None
            else DatabasePredictionSource.from_environment()
        )
        self.simulation_source = simulation_source
        if self.simulation_source is None and self.prediction_source is not None:
            engine = getattr(self.prediction_source, "engine", None)
            if engine is not None:
                self.simulation_source = DatabaseSimulationSource(engine)
        try:
            configured_cache_seconds = float(
                os.getenv("PREDICTION_READ_CACHE_SECONDS", "30")
            )
        except ValueError:
            configured_cache_seconds = 30.0
            LOGGER.warning(
                "PREDICTION_READ_CACHE_SECONDS is invalid; using 30 seconds"
            )
        self.prediction_cache_seconds = max(
            0.0,
            prediction_cache_seconds
            if prediction_cache_seconds is not None
            else configured_cache_seconds,
        )
        self._database_prediction_run: dict[str, Any] | None = None
        self._database_prediction_checked_at = 0.0
        self._simulation: dict | None = None
        self._simulation_generated_at = self.generated_at
        self._simulation_data_cutoff = self.data_cutoff

    def team_payload(self, team_id: str) -> dict:
        team = self.teams_by_id[team_id]
        return {
            **asdict(team),
            "elo": round(team.elo, 1),
            "flag": flag_for_team(team_id),
        }

    def _latest_database_prediction_run(
        self,
        force: bool = False,
    ) -> dict[str, Any] | None:
        if self.prediction_source is None:
            return None
        now = time.monotonic()
        if (
            not force
            and now - self._database_prediction_checked_at
            < self.prediction_cache_seconds
        ):
            return self._database_prediction_run
        self._database_prediction_checked_at = now
        try:
            run = self.prediction_source.load_latest()
        except Exception:
            self._database_prediction_run = None
            LOGGER.warning(
                "Latest database predictions are unavailable; serving static "
                "prediction fallback",
                exc_info=True,
            )
            return None
        if run is None or not run["predictions"]:
            self._database_prediction_run = None
            LOGGER.warning(
                "No database prediction run is available; serving static "
                "prediction fallback"
            )
            return None
        self._database_prediction_run = run
        LOGGER.info(
            "Serving latest database predictions: run=%s model=%s rows=%d",
            run["model_run_id"],
            run["model_version"],
            len(run["predictions"]),
        )
        return run

    def current_prediction_run(self, force: bool = False) -> dict[str, Any]:
        database_run = self._latest_database_prediction_run(force=force)
        if database_run is not None:
            return database_run
        return {
            "model_run_id": None,
            "model_version": STATIC_MODEL_VERSION,
            "generated_at": self.generated_at,
            "data_cutoff": self.data_cutoff,
            "predictions": self.static_predictions,
            "source": "fallback_static",
        }

    def prediction_payload(
        self,
        match_id: str,
        prediction_run: dict[str, Any] | None = None,
    ) -> dict | None:
        prediction_run = prediction_run or self.current_prediction_run()
        database_prediction = (
            prediction_run["predictions"].get(match_id)
            if prediction_run["model_run_id"] is not None
            else None
        )
        prediction = self.static_predictions.get(match_id)
        if not prediction:
            return None
        payload = {
            **prediction_dict(prediction),
            "model_version": STATIC_MODEL_VERSION,
            "generated_at": self.generated_at,
            "data_cutoff": self.data_cutoff,
            "source": "fallback_static",
        }
        if database_prediction is None:
            return payload

        home = float(
            database_prediction.get("final_home_probability")
            if database_prediction.get("final_home_probability") is not None
            else database_prediction["home_win_probability"]
        )
        draw = float(
            database_prediction.get("final_draw_probability")
            if database_prediction.get("final_draw_probability") is not None
            else database_prediction["draw_probability"]
        )
        away = float(
            database_prediction.get("final_away_probability")
            if database_prediction.get("final_away_probability") is not None
            else database_prediction["away_win_probability"]
        )
        payload.update(
            {
                "probabilities": {
                    "home_win": home,
                    "draw": draw,
                    "away_win": away,
                },
                "elo_base_home_probability": database_prediction.get(
                    "elo_base_home_probability"
                ),
                "elo_base_draw_probability": database_prediction.get(
                    "elo_base_draw_probability"
                ),
                "elo_base_away_probability": database_prediction.get(
                    "elo_base_away_probability"
                ),
                "attack_defense_adjustment": database_prediction.get(
                    "attack_defense_adjustment"
                ),
                "draw_calibration_adjustment": database_prediction.get(
                    "draw_calibration_adjustment"
                ),
                "context_adjustment_total": database_prediction.get(
                    "context_adjustment_total"
                ),
                "final_home_probability": home,
                "final_draw_probability": draw,
                "final_away_probability": away,
                "confidence_score": database_prediction.get("confidence_score"),
                "confidence_tier": database_prediction.get("confidence_tier"),
                "confidence_explanation": database_prediction.get(
                    "confidence_explanation"
                ),
                "confidence": database_prediction.get("confidence_tier")
                or payload["confidence"],
                "top_factors": _json_value(
                    database_prediction.get("top_factors"),
                    [],
                ),
                "model_version": database_prediction.get("model_version")
                or prediction_run["model_version"],
                "generated_at": str(prediction_run["generated_at"]),
                "data_cutoff": str(prediction_run["data_cutoff"]),
                "source": "database_latest",
            }
        )
        return payload

    def match_payload(
        self,
        match_id: str,
        prediction_run: dict[str, Any] | None = None,
    ) -> dict:
        match = next(match for match in self.fixtures if match.id == match_id)
        return {
            "id": match.id,
            "number": match.number,
            "stage": match.stage,
            "kickoff": match.kickoff.isoformat(),
            "venue_id": match.venue_id,
            "group": match.group,
            "home_team": (
                self.team_payload(match.home_team_id) if match.home_team_id else None
            ),
            "away_team": (
                self.team_payload(match.away_team_id) if match.away_team_id else None
            ),
            "home_slot": match.home_slot,
            "away_slot": match.away_slot,
            "prediction": self.prediction_payload(match.id, prediction_run),
        }

    def latest_predictions_payload(self, force: bool = False) -> dict[str, Any]:
        prediction_run = self.current_prediction_run(force=force)
        prediction_ids = (
            prediction_run["predictions"]
            if prediction_run["model_run_id"] is not None
            else self.static_predictions
        )
        return {
            "model_version": prediction_run["model_version"],
            "generated_at": str(prediction_run["generated_at"]),
            "data_cutoff": str(prediction_run["data_cutoff"]),
            "source": (
                "database_latest"
                if prediction_run["model_run_id"] is not None
                else "fallback_static"
            ),
            "predictions": [
                self.prediction_payload(match.id, prediction_run)
                for match in self.fixtures
                if match.id in prediction_ids
            ],
        }

    def latest_simulation(self) -> dict:
        if self.simulation_source is not None:
            try:
                database_simulation = self.simulation_source.load_latest()
            except Exception:
                LOGGER.warning(
                    "Latest database simulation is unavailable; serving static "
                    "simulation fallback",
                    exc_info=True,
                )
            else:
                if database_simulation is not None:
                    run = database_simulation["run"]
                    generated_at = run.get("generated_at") or run.get("created_at")
                    iterations = int(
                        run.get("num_simulations") or run.get("iterations") or 0
                    )
                    teams = []
                    for row in database_simulation["results"]:
                        team_id = str(row["team_id"])
                        team = self.teams_by_id.get(team_id)
                        if team is None:
                            LOGGER.warning(
                                "Skipping simulation result for unknown team %s",
                                team_id,
                            )
                            continue
                        teams.append(
                            {
                                "team_id": team_id,
                                "team_name": team.name,
                                "flag": flag_for_team(team_id),
                                "group": team.group,
                                "group_stage_exit": float(
                                    row.get("group_stage_exit_probability")
                                    or row.get("group_stage_exit")
                                    or 0
                                ),
                                "round_of_32": float(
                                    row.get("round_of_32_probability")
                                    or row.get("round_of_32")
                                    or 0
                                ),
                                "round_of_16": float(
                                    row.get("round_of_16_probability")
                                    or row.get("round_of_16")
                                    or 0
                                ),
                                "quarterfinal": float(
                                    row.get("quarterfinal_probability")
                                    or row.get("quarterfinal")
                                    or 0
                                ),
                                "semifinal": float(
                                    row.get("semifinal_probability")
                                    or row.get("semifinal")
                                    or 0
                                ),
                                "final": float(
                                    row.get("final_probability")
                                    or row.get("final")
                                    or 0
                                ),
                                "champion": float(
                                    row.get("champion_probability")
                                    or row.get("champion")
                                    or 0
                                ),
                            }
                        )
                    return {
                        "iterations": iterations,
                        "seed": int(run.get("random_seed") or run.get("seed") or 2026),
                        "model_version": run.get("model_version") or "unknown",
                        "generated_at": str(generated_at),
                        "created_at": str(run.get("created_at") or generated_at),
                        "data_cutoff": str(run.get("data_cutoff") or generated_at),
                        "source": "database_latest",
                        "monte_carlo_precision": {
                            "worst_case_standard_error": (
                                (0.25 / iterations) ** 0.5 if iterations else 0.0
                            ),
                            "worst_case_95_margin": (
                                1.96 * (0.25 / iterations) ** 0.5
                                if iterations
                                else 0.0
                            ),
                        },
                        "teams": teams,
                    }

        if self._simulation is None:
            snapshot_path = ROOT / "data" / "generated" / "latest.json"
            if snapshot_path.exists():
                snapshot = json.loads(snapshot_path.read_text())
                simulation = snapshot.get("simulation", {})
                if (
                    snapshot.get("model_version") == MODEL_VERSION
                    and simulation.get("iterations") == PUBLISHED_SIMULATION_ITERATIONS
                ):
                    self._simulation = simulation
                    self._simulation_generated_at = snapshot["generated_at"]
                    self._simulation_data_cutoff = snapshot["data_cutoff"]
            if self._simulation is None:
                self._simulation = simulate_tournament(
                    iterations=PUBLISHED_SIMULATION_ITERATIONS,
                    seed=2026,
                    context_repository=self.contexts,
                    cutoff=datetime.fromisoformat(self.data_cutoff),
                )
        return {
            **self._simulation,
            "model_version": MODEL_VERSION,
            "generated_at": self._simulation_generated_at,
            "created_at": self._simulation_generated_at,
            "data_cutoff": self._simulation_data_cutoff,
            "source": "fallback_static",
        }

    def team_profile_payload(self, team_id: str) -> dict:
        team = self.teams_by_id[team_id]
        team_names = {item.id: item.name for item in self.teams}
        cutoff = datetime.fromisoformat(self.data_cutoff)
        recent = recent_results(
            team_id,
            self.contexts.results,
            team_names,
            cutoff,
        )
        players = key_players(team_id, self.squad_players)
        probability = next(
            row
            for row in self.latest_simulation()["teams"]
            if row["team_id"] == team_id
        )
        matches = [
            self.match_payload(match.id, self.current_prediction_run())
            for match in self.fixtures
            if team_id in (match.home_team_id, match.away_team_id)
        ]
        group_path = []
        for match in matches:
            prediction = match["prediction"]
            if prediction is None:
                continue
            is_home = match["home_team"]["id"] == team_id
            opponent = match["away_team"] if is_home else match["home_team"]
            group_path.append(
                {
                    "match_id": match["id"],
                    "opponent_id": opponent["id"],
                    "opponent_name": opponent["name"],
                    "kickoff": match["kickoff"],
                    "team_win_probability": prediction["probabilities"][
                        "home_win" if is_home else "away_win"
                    ],
                    "draw_probability": prediction["probabilities"]["draw"],
                    "opponent_win_probability": prediction["probabilities"][
                        "away_win" if is_home else "home_win"
                    ],
                }
            )
        analysis = team_analysis(
            team,
            probability,
            recent,
            players,
            group_path,
            "experimental",
        )
        return {
            **self.team_payload(team_id),
            "matches": matches,
            "group_path": group_path,
            "tournament_probability": probability,
            "recent_results": recent,
            "form_summary": form_summary(recent),
            "key_players": player_payload(players),
            "analysis": analysis,
            "player_data_source": self.squad_metadata,
            "results_data_cutoff": self.data_cutoff,
        }


service = PredictionService()
