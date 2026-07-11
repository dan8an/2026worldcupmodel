import unittest
from datetime import timedelta

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

    def test_recovers_59_and_60_from_wc26_provider_rows_with_legacy_stage(self):
        expected = {
            59: ("Jordan", "Argentina", 1, 2),
            60: ("Algeria", "Austria", 0, 0),
        }
        rows = []
        for number, (home, away, home_score, away_score) in expected.items():
            fixture = next(item for item in self.fixtures if item.number == number)
            rows.append({
                "id": f"provider-{number}",
                "match_date": fixture.kickoff,
                "home_team": home,
                "away_team": away,
                "home_score": home_score,
                "away_score": away_score,
                "status": "FT",
                "tournament_stage": "FIFA World Cup",
                "provider_name": "api_football",
                "provider_payload": {"league": {"id": 1, "season": 2026}},
            })

        actions = plan_repairs(rows, self.team_ids)

        for number in expected:
            action = next(item for item in actions if item.fixture.number == number)
            self.assertEqual(action.keeper["id"], f"provider-{number}")
            self.assertIs(action.score_source, action.keeper)

    def test_historical_scored_rows_are_excluded_from_dirty_diagnostic(self):
        historical = {
            "id": "historical", "match_date": "2022-12-18T15:00:00Z",
            "home_team": "Argentina", "away_team": "France",
            "home_score": 3, "away_score": 3, "status": "FT",
            "tournament_stage": "Group Stage",
        }

        actions = plan_repairs([historical], self.team_ids)
        report = diagnostic_report(actions, [historical])

        self.assertEqual(report["rows_with_scores_but_no_official_identifier"], [])

    def test_ambiguous_recovery_candidates_remain_unresolved(self):
        fixture = next(item for item in self.fixtures if item.number == 59)
        rows = [
            {
                "id": f"candidate-{index}", "match_date": fixture.kickoff,
                "home_team": "Jordan", "away_team": "Argentina",
                "home_score": index, "away_score": 0, "status": "FT",
                "provider_payload": {"league": {"id": 1, "season": 2026}},
            }
            for index in (1, 2)
        ]

        action = next(
            item for item in plan_repairs(rows, self.team_ids)
            if item.fixture.number == 59
        )
        report = diagnostic_report([action], rows)

        self.assertIsNone(action.keeper)
        self.assertIsNone(action.score_source)
        self.assertEqual(
            report["unresolved_fixture_details"][0]["candidate_row_ids"],
            ["candidate-1", "candidate-2"],
        )

    def test_local_date_fixture_crossing_midnight_utc_is_accepted(self):
        fixture = next(item for item in self.fixtures if item.number == 2)
        row = {
            "id": "rollover-002",
            "match_date": fixture.kickoff + timedelta(hours=6),
            "home_team": "South Korea", "away_team": "Czechia",
            "home_score": 1, "away_score": 0, "status": "FT",
            "provider_payload": {"league": {"id": 1, "season": 2026}},
        }

        action = next(
            item for item in plan_repairs([row], self.team_ids)
            if item.fixture.number == 2
        )

        self.assertIs(action.keeper, row)
        self.assertIn("kickoff_within_12h_tolerance", action.evaluations[0].reasons)

    def test_june_27_local_fixture_on_june_28_utc_is_in_group_window(self):
        fixture = next(item for item in self.fixtures if item.number == 59)
        row = {
            "id": "rollover-059",
            "match_date": fixture.kickoff + timedelta(hours=9),
            "home_team": "Jordan", "away_team": "Argentina",
            "home_score": 1, "away_score": 2, "status": "FT",
            "tournament_stage": "Group Stage",
        }

        action = next(
            item for item in plan_repairs([row], self.team_ids)
            if item.fixture.number == 59
        )

        self.assertIs(action.keeper, row)
        self.assertNotIn("outside_group_window", action.evaluations[0].reasons)

    def test_wrong_teams_are_rejected_inside_kickoff_tolerance(self):
        fixture = next(item for item in self.fixtures if item.number == 2)
        row = {
            "id": "wrong-teams",
            "match_date": fixture.kickoff + timedelta(hours=6),
            "home_team": "Mexico", "away_team": "Czechia",
            "home_score": 1, "away_score": 0, "status": "FT",
            "provider_payload": {"league": {"id": 1, "season": 2026}},
        }

        action = next(
            item for item in plan_repairs([row], self.team_ids)
            if item.fixture.number == 2
        )

        self.assertIsNone(action.keeper)
        self.assertFalse(action.evaluations[0].accepted)
        self.assertTrue(any(reason.startswith("team_mismatch") for reason in action.evaluations[0].reasons))

    def test_ambiguous_rollover_candidates_remain_unresolved(self):
        fixture = next(item for item in self.fixtures if item.number == 60)
        rows = [
            {
                "id": f"rollover-060-{index}",
                "match_date": fixture.kickoff + timedelta(hours=6),
                "home_team": "Algeria", "away_team": "Austria",
                "home_score": index, "away_score": 0, "status": "FT",
                "provider_payload": {"league": {"id": 1, "season": 2026}},
            }
            for index in (1, 2)
        ]

        action = next(
            item for item in plan_repairs(rows, self.team_ids)
            if item.fixture.number == 60
        )

        self.assertIsNone(action.keeper)
        self.assertIsNone(action.score_source)

    def test_authoritative_overrides_map_only_expected_provider_rows(self):
        expected = {
            8: (
                "9482f662-0f3e-447f-9367-5eba1ad7d5e0",
                "Qatar", "Switzerland", "2026-06-13T19:00:00+00:00",
            ),
            20: (
                "7b1fc893-14e9-46d7-94b0-0aa4cf34c1f8",
                "Australia", "Turkey", "2026-06-14T04:00:00+00:00",
            ),
        }
        for number, (row_id, home, away, kickoff) in expected.items():
            with self.subTest(number=number):
                fixture = next(item for item in self.fixtures if item.number == number)
                self.assertEqual(fixture.kickoff.isoformat(), kickoff)
                correct = {
                    "id": row_id, "match_date": kickoff,
                    "home_team": home, "away_team": away,
                    "home_score": 1 if number == 8 else 2,
                    "away_score": 1 if number == 8 else 0,
                    "status": "FT", "tournament_stage": "Group Stage - 1",
                    "provider_name": "api_football",
                    "provider_payload": {"league": {"id": 1, "season": 2026}},
                }
                wrong_teams = {
                    **correct,
                    "id": f"wrong-teams-{number}",
                    "home_team": "Mexico",
                }

                action = next(
                    item for item in plan_repairs([correct, wrong_teams], self.team_ids)
                    if item.fixture.number == number
                )

                self.assertEqual(action.keeper["id"], row_id)
                wrong_evaluation = next(
                    evaluation for evaluation in action.evaluations
                    if evaluation.row["id"] == f"wrong-teams-{number}"
                )
                self.assertFalse(wrong_evaluation.accepted)
                self.assertTrue(any(
                    reason.startswith("team_mismatch")
                    for reason in wrong_evaluation.reasons
                ))

    def test_override_rows_require_wc26_provider_provenance(self):
        fixture = next(item for item in self.fixtures if item.number == 8)
        group_only = {
            "id": "group-only-008", "match_date": fixture.kickoff,
            "home_team": "Qatar", "away_team": "Switzerland",
            "home_score": 1, "away_score": 1, "status": "FT",
            "tournament_stage": "Group Stage - 1",
        }

        action = next(
            item for item in plan_repairs([group_only], self.team_ids)
            if item.fixture.number == 8
        )

        self.assertIsNone(action.keeper)


if __name__ == "__main__":
    unittest.main()
