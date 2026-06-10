import math

ProbabilityVector = tuple[float, float, float]


def _validate(predictions: list[ProbabilityVector], outcomes: list[int]) -> None:
    if not predictions:
        raise ValueError("At least one prediction is required")
    if len(predictions) != len(outcomes):
        raise ValueError("Predictions and outcomes must have equal lengths")
    if any(outcome not in (0, 1, 2) for outcome in outcomes):
        raise ValueError("Outcomes must use 0=home, 1=draw, or 2=away")


def log_loss(predictions: list[ProbabilityVector], outcomes: list[int]) -> float:
    _validate(predictions, outcomes)
    losses = [
        -math.log(max(1e-15, probabilities[outcome]))
        for probabilities, outcome in zip(predictions, outcomes)
    ]
    return sum(losses) / len(losses)


def brier_score(predictions: list[ProbabilityVector], outcomes: list[int]) -> float:
    _validate(predictions, outcomes)
    scores = []
    for probabilities, outcome in zip(predictions, outcomes):
        scores.append(
            sum(
                (probability - (1.0 if index == outcome else 0.0)) ** 2
                for index, probability in enumerate(probabilities)
            )
        )
    return sum(scores) / len(scores)


def ranked_probability_score(
    predictions: list[ProbabilityVector], outcomes: list[int]
) -> float:
    _validate(predictions, outcomes)
    scores = []
    for probabilities, outcome in zip(predictions, outcomes):
        observed = [1.0 if index == outcome else 0.0 for index in range(3)]
        first = probabilities[0] - observed[0]
        second = probabilities[0] + probabilities[1] - observed[0] - observed[1]
        scores.append((first**2 + second**2) / 2)
    return sum(scores) / len(scores)


def accuracy(predictions: list[ProbabilityVector], outcomes: list[int]) -> float:
    _validate(predictions, outcomes)
    correct = sum(
        max(range(3), key=lambda index: probabilities[index]) == outcome
        for probabilities, outcome in zip(predictions, outcomes)
    )
    return correct / len(predictions)


def calibration_bins(
    predictions: list[ProbabilityVector],
    outcomes: list[int],
    bin_count: int = 10,
) -> tuple[list[dict], float]:
    _validate(predictions, outcomes)
    bins = [
        {"lower": index / bin_count, "upper": (index + 1) / bin_count, "values": []}
        for index in range(bin_count)
    ]
    for probabilities, outcome in zip(predictions, outcomes):
        for class_index, probability in enumerate(probabilities):
            index = min(bin_count - 1, int(probability * bin_count))
            bins[index]["values"].append((probability, 1.0 if class_index == outcome else 0.0))
    total = len(predictions) * 3
    output = []
    expected_calibration_error = 0.0
    for bucket in bins:
        values = bucket.pop("values")
        count = len(values)
        mean_probability = sum(value[0] for value in values) / count if count else 0.0
        observed_rate = sum(value[1] for value in values) / count if count else 0.0
        expected_calibration_error += (
            count / total * abs(mean_probability - observed_rate)
        )
        output.append(
            {
                **bucket,
                "count": count,
                "mean_probability": round(mean_probability, 6),
                "observed_rate": round(observed_rate, 6),
            }
        )
    return output, expected_calibration_error


def evaluate(
    predictions: list[ProbabilityVector],
    outcomes: list[int],
) -> dict:
    bins, calibration_error = calibration_bins(predictions, outcomes)
    return {
        "matches": len(predictions),
        "log_loss": round(log_loss(predictions, outcomes), 6),
        "brier_score": round(brier_score(predictions, outcomes), 6),
        "ranked_probability_score": round(
            ranked_probability_score(predictions, outcomes), 6
        ),
        "accuracy": round(accuracy(predictions, outcomes), 6),
        "expected_calibration_error": round(calibration_error, 6),
        "calibration_bins": bins,
    }
