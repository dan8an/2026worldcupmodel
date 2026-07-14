"""Reliability diagnostics that retain class and bucket support."""

from __future__ import annotations

from typing import Any

CLASSES = ("home", "draw", "away")


def fixed_width_reliability(predictions, outcomes, bin_count: int = 10) -> list[dict[str, Any]]:
    rows = []
    for class_index, class_name in enumerate(CLASSES):
        for index in range(bin_count):
            lower, upper = index / bin_count, (index + 1) / bin_count
            values = [(p[class_index], int(y == class_index)) for p, y in zip(predictions, outcomes) if lower <= p[class_index] < upper or (index == bin_count - 1 and p[class_index] == 1)]
            mean = sum(v for v, _ in values) / len(values) if values else None
            observed = sum(v for _, v in values) / len(values) if values else None
            rows.append({"class": class_name, "lower": lower, "upper": upper, "count": len(values), "mean_probability": mean, "observed_rate": observed, "absolute_gap": abs(mean - observed) if values else None})
    return rows


def adaptive_reliability(predictions, outcomes, minimum_count: int = 20) -> list[dict[str, Any]]:
    if minimum_count < 2:
        raise ValueError("minimum_count must be at least two")
    rows = []
    for class_index, class_name in enumerate(CLASSES):
        values = sorted((p[class_index], int(y == class_index)) for p, y in zip(predictions, outcomes))
        bucket_count = max(1, len(values) // minimum_count)
        for bucket in range(bucket_count):
            start = bucket * len(values) // bucket_count
            end = (bucket + 1) * len(values) // bucket_count
            selected = values[start:end]
            mean = sum(v for v, _ in selected) / len(selected)
            observed = sum(v for _, v in selected) / len(selected)
            rows.append({"class": class_name, "lower": selected[0][0], "upper": selected[-1][0], "count": len(selected), "mean_probability": mean, "observed_rate": observed, "absolute_gap": abs(mean - observed)})
    return rows


def calibration_summary(fixed: list[dict[str, Any]], total_predictions: int, minimum_support: int = 20) -> dict[str, Any]:
    populated = [row for row in fixed if row["count"]]
    return {
        "expected_calibration_error": sum(row["count"] / (3 * total_predictions) * row["absolute_gap"] for row in populated),
        "maximum_calibration_error": max(row["absolute_gap"] for row in populated),
        "maximum_calibration_error_bucket": max(populated, key=lambda row: row["absolute_gap"]),
        "support_aware_minimum_count": minimum_support,
        "support_aware_maximum_calibration_error": max((row["absolute_gap"] for row in populated if row["count"] >= minimum_support), default=None),
    }
