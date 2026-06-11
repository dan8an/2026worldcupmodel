from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "data" / "evaluation" / "elo_context_v3_validation.json"

CONFIDENCE_VERSION = "confidence-v1"
MEDIUM_THRESHOLD = 55.0
HIGH_THRESHOLD = 71.0
WEIGHTS = {
    "probability_separation": 0.40,
    "model_agreement": 0.10,
    "calibration_reliability": 0.15,
    "data_completeness": 0.15,
    "match_stability": 0.20,
}


@dataclass(frozen=True)
class DataCompleteness:
    team_ratings: bool
    attack_defense_ratings: bool
    player_ratings: bool
    context: bool

    @property
    def score(self) -> float:
        return (
            0.30 * self.team_ratings
            + 0.25 * self.attack_defense_ratings
            + 0.15 * self.player_ratings
            + 0.30 * self.context
        )

    @property
    def missing(self) -> list[str]:
        fields = (
            ("team ratings", self.team_ratings),
            ("attack/defense ratings", self.attack_defense_ratings),
            ("player ratings", self.player_ratings),
            ("match context", self.context),
        )
        return [name for name, available in fields if not available]


@lru_cache(maxsize=1)
def load_calibration_buckets() -> list[dict[str, Any]]:
    if not REPORT_PATH.exists():
        return []
    report = json.loads(REPORT_PATH.read_text())
    return list(report.get("calibration", {}).get("v3", []))


def confidence_tier(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "High"
    if score >= MEDIUM_THRESHOLD:
        return "Medium"
    return "Low"


def calibration_reliability(
    probabilities: tuple[float, float, float],
    calibration_buckets: list[dict[str, Any]],
) -> float:
    if not calibration_buckets:
        return 0.5
    top_probability = max(probabilities)
    index = min(len(calibration_buckets) - 1, int(top_probability * 10))
    bucket = calibration_buckets[index]
    count = int(bucket.get("count", 0))
    if count <= 0:
        return 0.35
    error = abs(
        float(bucket.get("mean_probability", 0.0))
        - float(bucket.get("observed_rate", 0.0))
    )
    accuracy = max(0.0, 1.0 - error / 0.15)
    support = min(1.0, count / 30.0)
    return accuracy * (0.70 + 0.30 * support)


def calculate_confidence(
    elo_probabilities: tuple[float, float, float],
    final_probabilities: tuple[float, float, float],
    completeness: DataCompleteness,
    calibration_buckets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ordered = sorted(final_probabilities, reverse=True)
    separation = min(1.0, (ordered[0] - ordered[1]) / 0.35)
    total_variation = sum(
        abs(final - elo)
        for final, elo in zip(final_probabilities, elo_probabilities)
    ) / 2.0
    agreement = max(0.0, 1.0 - total_variation / 0.15)
    calibration = calibration_reliability(
        final_probabilities,
        (
            calibration_buckets
            if calibration_buckets is not None
            else load_calibration_buckets()
        ),
    )
    closeness = 1.0 - abs(final_probabilities[0] - final_probabilities[2])
    volatility = min(
        1.0,
        0.55 * final_probabilities[1] / 0.35 + 0.45 * closeness,
    )
    stability = 1.0 - volatility
    components = {
        "probability_separation": separation,
        "model_agreement": agreement,
        "calibration_reliability": calibration,
        "data_completeness": completeness.score,
        "match_stability": stability,
    }
    score = round(
        100.0 * sum(WEIGHTS[name] * value for name, value in components.items()),
        1,
    )
    tier = confidence_tier(score)

    reasons = []
    if separation >= 0.65:
        reasons.append("the leading outcome is clearly separated")
    elif separation <= 0.25:
        reasons.append("the leading outcomes are tightly grouped")
    if agreement < 0.75:
        reasons.append("context moves the forecast away from the Elo baseline")
    if final_probabilities[1] >= 0.30:
        reasons.append("the draw probability adds volatility")
    if completeness.missing:
        reasons.append(f"{', '.join(completeness.missing)} are unavailable")
    if not reasons:
        reasons.append("the model inputs are stable and broadly complete")

    return {
        "confidence_score": score,
        "confidence_tier": tier,
        "confidence_explanation": (
            f"{tier} confidence because " + "; ".join(reasons[:3]) + "."
        ),
        "confidence_components": {
            name: round(value * 100.0, 1) for name, value in components.items()
        },
    }
