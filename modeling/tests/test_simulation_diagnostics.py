from modeling.tests.test_run_simulations import (
    canonical_knockout_prediction,
    canonical_predictions,
)
from scripts.diagnose_simulations import (
    PROBABILITY_FIELDS,
    replay_with_diagnostics,
    validate_persisted_results,
)


def test_diagnostic_replay_preserves_stage_invariants():
    replay = replay_with_diagnostics(
        canonical_predictions(),
        100,
        7,
        canonical_knockout_prediction(),
    )

    expected = {
        "round_of_32_probability": 32,
        "round_of_16_probability": 16,
        "quarterfinal_probability": 8,
        "semifinal_probability": 4,
        "final_probability": 2,
        "champion_probability": 1,
    }
    for field, total in expected.items():
        assert abs(
            sum(row[field] for row in replay["probabilities"].values()) - total
        ) < 1e-9

    czechia = replay["focus"]
    assert 1 <= czechia["average_group_finish"] <= 4
    assert abs(sum(czechia["group_finish_distribution"].values()) - 1) < 1e-9
    assert sum(
        row["count"] for row in czechia["round_of_32_opponents"]
    ) == round(
        replay["probabilities"]["CZE"]["round_of_32_probability"] * 100
    )


def test_persisted_probability_validation_detects_monotonic_violation():
    valid = {
        "team_id": "AAA",
        **{
            field: value
            for field, value in zip(
                PROBABILITY_FIELDS,
                (0.9, 0.7, 0.5, 0.3, 0.2, 0.1),
            )
        },
    }
    invalid = {
        "team_id": "BBB",
        **{
            field: value
            for field, value in zip(
                PROBABILITY_FIELDS,
                (0.8, 0.6, 0.7, 0.2, 0.1, 0.05),
            )
        },
    }

    validation = validate_persisted_results([valid, invalid])

    assert validation["monotonic_checks_pass"] is False
    assert validation["monotonic_violations"] == ["BBB"]
