import unittest
from datetime import date, timedelta

from scripts.build_xg_proxy_features import (
    build_team_ratings,
    calculate_chance_quality_rating,
)
from scripts.audit_input_ratings import cap_shot_volume, shrink_shot_volume
from scripts.generate_predictions import calculate_prediction
from scripts.validate_xg_proxy_v4 import (
    ABLATIONS,
    build_promotion_config,
    build_report,
    select_best_validated_ablation,
)


class XgProxyFeatureTests(unittest.TestCase):
    def test_promotion_selects_lowest_holdout_brier(self):
        results = {
            "v3_only": {
                "status": "evaluated",
                "selected_weight": 0.0,
                "validation_metrics": {
                    "brier_score": 0.568618,
                    "log_loss": 0.960027,
                },
            },
            "v3_plus_shot_volume": {
                "status": "evaluated",
                "selected_weight": 0.2,
                "validation_metrics": {
                    "brier_score": 0.557889,
                    "log_loss": 0.945067,
                },
            },
            "v3_plus_all_xg_proxy_features": {
                "status": "evaluated",
                "selected_weight": 0.2,
                "validation_metrics": {
                    "brier_score": 0.562098,
                    "log_loss": 0.950870,
                },
            },
        }

        selected = select_best_validated_ablation(results)
        config = build_promotion_config(results, selected)

        self.assertEqual(selected, "v3_plus_shot_volume")
        self.assertEqual(config["selected_weight"], 0.2)
        self.assertEqual(config["features_used"], ["shot_volume_rating"])

    def test_log_loss_breaks_equal_brier_scores(self):
        results = {
            "v3_only": {
                "status": "evaluated",
                "validation_metrics": {
                    "brier_score": 0.56,
                    "log_loss": 0.96,
                },
            },
            "v3_plus_shot_volume": {
                "status": "evaluated",
                "validation_metrics": {
                    "brier_score": 0.56,
                    "log_loss": 0.94,
                },
            },
        }

        self.assertEqual(
            select_best_validated_ablation(results),
            "v3_plus_shot_volume",
        )

    def test_feature_builder_uses_shot_location_and_opponent_pressure(self):
        rows = [
            {
                "shots": 14,
                "shots_on_target": 6,
                "shots_inside_box": 10,
                "shots_outside_box": 4,
                "blocked_shots": 3,
                "corners": 6,
                "possession": 57,
                "passes_attempted": 500,
                "passes_completed": 430,
                "goalkeeper_saves": 3,
                "opponent_shots": 8,
                "opponent_shots_on_target": 3,
            },
            {
                "shots": 12,
                "shots_on_target": 5,
                "shots_inside_box": 8,
                "shots_outside_box": 4,
                "blocked_shots": 2,
                "corners": 5,
                "possession": 54,
                "pass_accuracy": 84,
                "goalkeeper_saves": 4,
                "opponent_shots": 10,
                "opponent_shots_on_target": 4,
            },
        ]

        rating = calculate_chance_quality_rating(rows)

        self.assertEqual(rating["sample_matches"], 2)
        self.assertAlmostEqual(rating["box_shot_rate"], 18 / 26, places=6)
        self.assertAlmostEqual(
            rating["shots_on_target_rate"], 11 / 26, places=6
        )
        self.assertAlmostEqual(
            rating["components"]["pass_accuracy"], 85.0, places=4
        )
        for field in (
            "shot_volume_rating",
            "shot_quality_proxy",
            "chance_creation_rating",
            "defensive_shot_suppression",
            "keeper_pressure_allowed",
        ):
            self.assertGreaterEqual(rating[field], 0)
            self.assertLessEqual(rating[field], 100)

    def test_pairing_team_rows_adds_defensive_features(self):
        ratings = build_team_ratings(
            [
                {
                    "match_id": "m1",
                    "team_id": "a",
                    "shots": 15,
                    "shots_on_target": 7,
                    "shots_inside_box": 11,
                },
                {
                    "match_id": "m1",
                    "team_id": "b",
                    "shots": 5,
                    "shots_on_target": 1,
                    "shots_inside_box": 2,
                },
            ]
        )

        by_team = {rating["team_id"]: rating for rating in ratings}
        self.assertGreater(
            by_team["a"]["defensive_shot_suppression"],
            by_team["b"]["defensive_shot_suppression"],
        )
        self.assertLess(
            by_team["a"]["keeper_pressure_allowed"],
            by_team["b"]["keeper_pressure_allowed"],
        )

    def test_saturated_volume_transformations_reduce_single_feature_edge(self):
        rating = {
            "elo_rating": 1500,
            "attack_rating": 50,
            "defense_rating": 50,
        }
        saturated = calculate_prediction(
            rating,
            rating,
            home_shot_volume_rating=100,
            away_shot_volume_rating=0,
        )
        capped = calculate_prediction(
            rating,
            rating,
            home_shot_volume_rating=cap_shot_volume(100),
            away_shot_volume_rating=cap_shot_volume(0),
        )
        shrunk = calculate_prediction(
            rating,
            rating,
            home_shot_volume_rating=shrink_shot_volume(100, 10),
            away_shot_volume_rating=shrink_shot_volume(0, 10),
        )

        self.assertLess(saturated["home_win_probability"], 0.45)
        self.assertLess(
            capped["home_win_probability"],
            saturated["home_win_probability"],
        )
        self.assertLess(
            shrunk["home_win_probability"],
            capped["home_win_probability"],
        )

    def test_insufficient_history_blocks_promotion_and_lists_ablations(self):
        matches = []
        for index in range(8):
            matches.append(
                {
                    "match_id": f"m{index}",
                    "played_on": date(2026, 1, 1) + timedelta(days=index),
                    "home_team_id": "a",
                    "away_team_id": "b",
                    "home_goals": 1,
                    "away_goals": 0,
                    "home_stats": {
                        "shots": 12,
                        "shots_on_target": 5,
                        "shots_inside_box": 8,
                    },
                    "away_stats": {
                        "shots": 8,
                        "shots_on_target": 2,
                        "shots_inside_box": 4,
                    },
                }
            )

        report = build_report(matches, {"team_stat_rows": 16})

        self.assertEqual(report["status"], "insufficient_data")
        self.assertEqual(set(report["ablations"]), set(ABLATIONS))
        self.assertFalse(report["promotion"]["recommend_promotion"])
        self.assertTrue(
            all(
                result["brier_score"] is None
                and result["log_loss"] is None
                for result in report["ablations"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
