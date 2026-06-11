import inspect
import unittest

from scripts import freshness_gating
from scripts.freshness_gating import calculate_freshness_gated_prediction
from scripts.generate_predictions import calculate_prediction


class FreshnessGatingV4Tests(unittest.TestCase):
    def test_production_rest_gap_is_disabled(self):
        rating = {
            "elo_rating": 1500,
            "attack_rating": 50,
            "defense_rating": 50,
            "matches_played": 10,
        }
        current = calculate_prediction(
            rating,
            rating,
            home_rest_days=710,
            away_rest_days=240,
        )
        reversed_gap = calculate_prediction(
            rating,
            rating,
            home_rest_days=240,
            away_rest_days=710,
        )

        self.assertEqual(current, reversed_gap)
        self.assertAlmostEqual(current["context_adjustment_total"], 0.0)

    def test_stale_attack_and_defense_are_shrunk_toward_neutral(self):
        stale = {
            "elo_rating": 1500,
            "attack_rating": 20,
            "defense_rating": 80,
            "matches_played": 10,
        }
        fresh = {
            "elo_rating": 1500,
            "attack_rating": 50,
            "defense_rating": 50,
            "matches_played": 10,
        }
        result = calculate_freshness_gated_prediction(
            stale,
            fresh,
            home_rating_age_days=710,
            away_rating_age_days=30,
            gate_elo=False,
            gate_rest=False,
            gate_shot_volume=False,
        )
        effective = result["input_reliability"]["home"]

        self.assertGreater(effective["effective_attack_rating"], 20)
        self.assertLess(effective["effective_defense_rating"], 80)
        self.assertLess(effective["attack_defense"]["combined"], 0.25)

    def test_production_and_freshness_formulas_do_not_accept_market_odds(self):
        self.assertNotIn(
            "market",
            inspect.signature(calculate_prediction).parameters,
        )
        self.assertNotIn(
            "market",
            inspect.signature(
                calculate_freshness_gated_prediction
            ).parameters,
        )
        source = inspect.getsource(freshness_gating)
        self.assertNotIn("market_odds", source)
        self.assertNotIn("market_probability", source)


if __name__ == "__main__":
    unittest.main()
