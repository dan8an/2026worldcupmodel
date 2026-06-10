import json
from datetime import datetime, timezone
from pathlib import Path

from .data import ROOT, build_fixtures, load_teams, validate_tournament
from .features.context import ContextRepository
from .poisson import predict_match
from .simulation import (
    PUBLISHED_SIMULATION_ITERATIONS,
    prediction_dict,
    simulate_tournament,
)


def generate_snapshot(iterations: int = PUBLISHED_SIMULATION_ITERATIONS) -> Path:
    teams = load_teams()
    fixtures = build_fixtures(teams)
    validate_tournament(teams, fixtures)
    teams_by_id = {team.id: team for team in teams}
    contexts = ContextRepository()
    generated_at = datetime.now(timezone.utc)
    predictions = [
        prediction_dict(
            predict_match(
                teams_by_id[match.home_team_id or ""],
                teams_by_id[match.away_team_id or ""],
                match.id,
                context=contexts.for_match(
                    match.home_team_id or "",
                    match.away_team_id or "",
                    generated_at,
                ),
            )
        )
        for match in fixtures
        if match.stage == "group"
    ]
    generated_at_text = generated_at.isoformat()
    payload = {
        "model_version": "context-0.2.0",
        "generated_at": generated_at_text,
        "data_cutoff": generated_at_text,
        "predictions": predictions,
        "simulation": simulate_tournament(
            iterations=iterations,
            context_repository=contexts,
            cutoff=generated_at,
        ),
    }
    output_dir = ROOT / "data" / "generated"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "latest.json"
    output.write_text(json.dumps(payload, indent=2))
    return output


if __name__ == "__main__":
    print(generate_snapshot())
