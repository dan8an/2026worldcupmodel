"""Small persisted multiclass temperature calibrator."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def _softmax(values: list[float]) -> tuple[float, float, float]:
    peak = max(values)
    exp = [math.exp(value - peak) for value in values]
    total = sum(exp)
    return tuple(value / total for value in exp)  # type: ignore[return-value]


@dataclass(frozen=True)
class TemperatureCalibrator:
    temperature: float
    model_version: str

    def transform(self, probabilities: tuple[float, float, float]) -> tuple[float, float, float]:
        if not math.isfinite(self.temperature) or self.temperature <= 0:
            raise ValueError("temperature must be finite and positive")
        clipped = [min(1.0, max(1e-12, float(value))) for value in probabilities]
        result = _softmax([math.log(value) / self.temperature for value in clipped])
        if not all(math.isfinite(value) and value >= 0 for value in result):
            raise ValueError("calibration produced invalid probabilities")
        return result

    def to_dict(self) -> dict[str, float | str]:
        return {"type": "temperature_scaling", "temperature": self.temperature, "model_version": self.model_version}

    @classmethod
    def from_dict(cls, payload: dict, expected_model_version: str) -> "TemperatureCalibrator":
        if payload.get("model_version") != expected_model_version:
            raise ValueError("calibration artifact model version mismatch")
        return cls(float(payload["temperature"]), expected_model_version)


def fit_temperature(predictions: list[tuple[float, float, float]], outcomes: list[int], model_version: str) -> TemperatureCalibrator:
    if not predictions or len(predictions) != len(outcomes):
        raise ValueError("calibration data must be nonempty and aligned")
    def loss(temperature: float) -> float:
        calibrator = TemperatureCalibrator(temperature, model_version)
        return sum(-math.log(max(1e-15, calibrator.transform(p)[y])) for p, y in zip(predictions, outcomes)) / len(outcomes)
    # Deterministic log-spaced search; one scalar limits overfit on modest samples.
    candidates = [math.exp(math.log(0.25) + i * (math.log(4.0) - math.log(0.25)) / 2000) for i in range(2001)]
    best = min(candidates, key=loss)
    return TemperatureCalibrator(best, model_version)


def temperature_log_loss(predictions, outcomes, temperature: float) -> float:
    calibrator = TemperatureCalibrator(temperature, "objective")
    return sum(-math.log(max(1e-15, calibrator.transform(p)[y])) for p, y in zip(predictions, outcomes)) / len(outcomes)


@dataclass(frozen=True)
class MultinomialCalibrator:
    coefficients: tuple[tuple[float, float, float], ...]
    regularization: float
    model_version: str
    identity: bool = False

    def transform(self, probabilities: tuple[float, float, float]) -> tuple[float, float, float]:
        if self.identity:
            total = sum(probabilities)
            return tuple(max(0.0, value) / total for value in probabilities)  # type: ignore[return-value]
        clipped = [max(1e-12, min(1.0, value)) for value in probabilities]
        features = [1.0, math.log(clipped[0] / clipped[2]), math.log(clipped[1] / clipped[2])]
        return _softmax([sum(weight * value for weight, value in zip(row, features)) for row in self.coefficients])

    def to_dict(self) -> dict[str, Any]:
        return {"type": "regularized_multinomial_logistic", "coefficients": self.coefficients, "regularization": self.regularization, "model_version": self.model_version, "identity": self.identity}

    @classmethod
    def from_dict(cls, payload: dict[str, Any], expected_model_version: str) -> "MultinomialCalibrator":
        if payload.get("model_version") != expected_model_version:
            raise ValueError("calibration artifact model version mismatch")
        coefficients = tuple(tuple(float(value) for value in row) for row in payload["coefficients"])
        if len(coefficients) != 3 or any(len(row) != 3 for row in coefficients):
            raise ValueError("multinomial coefficient shape must be 3x3")
        return cls(coefficients, float(payload["regularization"]), expected_model_version, bool(payload.get("identity")))


def fit_multinomial(predictions, outcomes, model_version: str, regularization: float, iterations: int = 3000, learning_rate: float = 0.02) -> MultinomialCalibrator:
    if not predictions or len(predictions) != len(outcomes):
        raise ValueError("calibration data must be nonempty and aligned")
    weights = [[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]]
    features = [[1.0, math.log(max(1e-12, p[0]) / max(1e-12, p[2])), math.log(max(1e-12, p[1]) / max(1e-12, p[2]))] for p in predictions]
    for iteration in range(iterations):
        gradient = [[0.0] * 3 for _ in range(3)]
        for values, outcome in zip(features, outcomes):
            probabilities = _softmax([sum(w * x for w, x in zip(row, values)) for row in weights])
            for class_index in range(3):
                error = probabilities[class_index] - int(outcome == class_index)
                for feature_index in range(3):
                    gradient[class_index][feature_index] += error * values[feature_index] / len(outcomes)
        rate = learning_rate / math.sqrt(1.0 + iteration / 500.0)
        for class_index in range(3):
            for feature_index in range(3):
                penalty = 0.0 if feature_index == 0 else regularization * weights[class_index][feature_index] / len(outcomes)
                weights[class_index][feature_index] -= rate * (gradient[class_index][feature_index] + penalty)
    return MultinomialCalibrator(tuple(tuple(row) for row in weights), regularization, model_version)
