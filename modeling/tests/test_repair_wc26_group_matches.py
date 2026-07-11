import unittest

from modeling.src.data import build_fixtures, load_teams
from scripts.generate_predictions import map_database_team_ids
from scripts.repair_wc26_group_matches import diagnostic_report, plan_repairs


class GroupMatchRepairTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = build_fixtures(load_teams())
        self.team_ids = {team.id: team.id for team in load_teams()}

    def test_promotes_completed_legacy_row_and_preserves_actual_score(self):
        fixture = self.fixtures[0]
        legacy = {
            "id": "provider-123",
            "tournament_stage": "Group Stage - 1",
            "match_date": fixture.kickoff.isoformat(),
            "home_team_id": fixture.home_team_id,
            "away_team_id": fixture.away_team_id,
            "home_score": 2,
            "away_score": 1,
            "completed": True,
            "status": "FT",
        }

        action = plan_repairs([legacy], self.team_ids)[0]

        self.assertIs(action.keeper, legacy)
        self.assertIs(action.score_source, legacy)
        self.assertEqual((action.score_source["home_score"], action.score_source["away_score"]), (2, 1))
        self.assertEqual(action.official_id, "WC26-001")

    def test_merges_unofficial_result_into_existing_official_row(self):
        fixture = self.fixtures[0]
        official = {
            "id": "official-uuid", "canonical_match_id": "WC26-001",
            "match_number": 1, "tournament_stage": "Group", "kickoff": fixture.kickoff,
            "home_team_id": fixture.home_team_id, "away_team_id": fixture.away_team_id,
            "completed": False,
        }
        result = {
            "id": "legacy-uuid", "tournament_stage": "group_stage", "match_date": fixture.kickoff,
            "home_team_id": fixture.home_team_id, "away_team_id": fixture.away_team_id,
            "home_score": 0, "away_score": 3, "status": "FT", "updated_at": "2026-06-12T22:00:00Z",
        }

        action = plan_repairs([official, result], self.team_ids)[0]

        self.assertIs(action.keeper, result)
        self.assertIs(action.score_source, result)
        self.assertEqual({row["id"] for row in action.duplicates}, {"official-uuid"})

    def test_never_invents_scores_for_missing_fixtures(self):
        actions = plan_repairs([], self.team_ids)
        report = diagnostic_report(actions)

        self.assertEqual(len(actions), 72)
        self.assertTrue(all(action.score_source is None for action in actions))
        self.assertEqual(report["official_completed_group_count"], 0)
        self.assertEqual(report["missing_official_group_identifiers"], [f"WC26-{n:03d}" for n in range(1, 73)])

    def test_uses_completed_score_from_provider_payload(self):
        fixture = self.fixtures[0]
        provider = {
            "id": "provider-payload", "tournament_stage": "Group Stage",
            "match_date": fixture.kickoff, "home_team_id": fixture.home_team_id,
            "away_team_id": fixture.away_team_id,
            "provider_payload": {
                "fixture": {"status": {"short": "FT"}},
                "goals": {"home": 4, "away": 2},
            },
        }

        action = plan_repairs([provider], self.team_ids)[0]

        self.assertIs(action.score_source, provider)

    def test_maps_production_team_name_and_code_variants(self):
        variants = {
            "BIH": ["BIH", "Bosnia and Herzegovina"],
            "COD": ["COD", "DR Congo", "Congo DR"],
            "CPV": ["CPV", "Cape Verde", "Cabo Verde"],
            "CUW": ["CUW", "Curaçao", "Curacao"],
            "CZE": ["CZE", "Czechia", "Czech Republic"],
            "TUR": ["TUR", "Turkey", "Türkiye"],
            "USA": ["USA", "United States", "United States of America"],
        }
        for code, names in variants.items():
            for name in names:
                with self.subTest(code=code, name=name):
                    mapping = map_database_team_ids([
                        {"id": f"database-{code}", "name": name,
                         "api_football_team_id": 1000}
                    ])
                    self.assertEqual(mapping[code], f"database-{code}")

    def test_team_mapping_rejects_ambiguous_alias_rows(self):
        rows = [
            {"id": "usa-one", "name": "USA", "api_football_team_id": 1},
            {"id": "usa-two", "name": "USA", "api_football_team_id": 2},
        ]

        with self.assertRaisesRegex(
            RuntimeError,
            "Canonical team USA maps to multiple database teams:.*usa-one.*usa-two",
        ):
            map_database_team_ids(rows)

    def test_team_mapping_prefers_single_provider_linked_alias_row(self):
        rows = [
            {"id": "legacy", "name": "United States", "api_football_team_id": None},
            {"id": "provider", "name": "USA", "api_football_team_id": 2384},
        ]

        mapping = map_database_team_ids(rows)

        # Canonical-code identity has priority over display-name identity.
        self.assertEqual(mapping["USA"], "provider")


if __name__ == "__main__":
    unittest.main()
