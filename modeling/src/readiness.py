"""Fail-safe loading of the versioned production-readiness artifact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_FIELDS = {"candidate_model_version", "current_production_model_version", "gate", "promotion_recommendation"}


def load_readiness(path: Path, active_model_version: str) -> dict[str, Any]:
    fallback = {"status": "fail", "ready": False, "message": "This model has not yet passed the project’s production calibration gate.", "failed_conditions": ["A valid, matching readiness artifact is unavailable."]}
    try:
        payload = json.loads(path.read_text())
        if not REQUIRED_FIELDS.issubset(payload):
            return fallback
        evaluated_version = payload["candidate_model_version"] if payload.get("promotion_recommendation") == "promote" else payload["current_production_model_version"]
        if evaluated_version != active_model_version:
            return fallback
        conditions = payload["gate"]["conditions"]
        failed = [condition["explanation"] for condition in conditions if condition.get("required", True) and not condition.get("passed")]
        passed = payload["gate"].get("overall_status") == "pass" and not failed
        if not passed:
            return {"status": "fail", "ready": False, "message": "This model has not yet passed the project’s production calibration gate.", "failed_conditions": failed or ["The overall gate did not pass."]}
        return {"status": "pass", "ready": True, "message": "This release passed the project’s defined historical calibration gate. Predictions remain probabilistic; historical calibration does not guarantee future accuracy.", "failed_conditions": []}
    except (OSError, ValueError, TypeError, KeyError):
        return fallback
