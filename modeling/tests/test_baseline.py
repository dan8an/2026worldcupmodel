import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from modeling.src.data import build_fixtures, load_teams, validate_tournament
from modeling.src.domain import Team
from modeling.src.features.context import (
    AvailabilityReport,
    ContextRepository,
    HistoricalResult,
    SquadSelection,
    load_historical_results,
)
from modeling.src.poisson import HOST_ELO_ADVANTAGE, expected_goals, predict_match
from modeling.src.simulation import sample_poisson, simulate_tournament
from modeling.src.standings import build_table


class TournamentDataTests(unittest.TestCase):
    def test_seed_has_official_shape(self):
        teams = load_teams()
        fixtures = build_fixtures(teams)
        validate_tournament(teams, fixtures)
        self.assertEqual(len(teams), 48)
        self.assertEqual(len(fixtures), 104)
        self.assertEqual(sum(match.stage == "group" for match in fixtures), 72)
        group_dates = [match.kickoff.date() for match in fixtures if match.stage == "group"]
        self.assertEqual(str(min(group_dates)), "2026-06-11")
        self.assertEqual(str(max(group_dates)), "2026-06-27")
        self.assertEqual(
            fixtures,
            sorted(fixtures, key=lambda match: (match.kickoff, match.number)),
        )
        first_groups = [match.group for match in fixtures[:5]]
        self.assertEqual(first_groups, ["A", "A", "B", "D", "B"])


class PoissonTests(unittest.TestCase):
    def test_probabilities_sum_to_one(self):
        teams = {team.id: team for team in load_teams()}
        prediction = predict_match(teams["MEX"], teams["RSA"], "WC26-001")
        total = prediction.home_win + prediction.draw + prediction.away_win
        self.assertAlmostEqual(total, 1.0, places=5)
        self.assertGreater(prediction.home_xg, prediction.away_xg)

    def test_fixture_order_does_not_create_home_advantage(self):
        first = Team("AAA", "First", "A", 1, 20, False)
        second = Team("BBB", "Second", "A", 2, 20, False)
        first_xg, second_xg = expected_goals(first, second)
        reversed_second_xg, reversed_first_xg = expected_goals(second, first)
        self.assertAlmostEqual(first_xg, second_xg)
        self.assertAlmostEqual(first_xg, reversed_first_xg)
        self.assertAlmostEqual(second_xg, reversed_second_xg)

    def test_only_host_flag_creates_venue_advantage_on_either_side(self):
        host = Team("USA", "United States", "D", 1, 20, True)
        visitor = Team("AAA", "Visitor", "D", 2, 20, False)
        host_home_xg, visitor_away_xg = expected_goals(host, visitor)
        visitor_home_xg, host_away_xg = expected_goals(visitor, host)
        self.assertEqual(HOST_ELO_ADVANTAGE, 50.0)
        self.assertGreater(host_home_xg, visitor_away_xg)
        self.assertGreater(host_away_xg, visitor_home_xg)
        self.assertAlmostEqual(host_home_xg, host_away_xg)
        self.assertAlmostEqual(visitor_away_xg, visitor_home_xg)

    def test_sampler_is_deterministic(self):
        first = [sample_poisson(1.4, random.Random(12)) for _ in range(4)]
        second = [sample_poisson(1.4, random.Random(12)) for _ in range(4)]
        self.assertEqual(first, second)


class ContextFeatureTests(unittest.TestCase):
    def setUp(self):
        self.cutoff = datetime(2026, 6, 9, tzinfo=timezone.utc)
        self.results = [
            HistoricalResult(
                played_on=date(2026, 5, 1),
                home_team_id="USA",
                away_team_id="PAR",
                home_score=3,
                away_score=0,
                tournament="FIFA World Cup qualification",
                neutral=True,
            ),
            HistoricalResult(
                played_on=date(2026, 7, 1),
                home_team_id="PAR",
                away_team_id="USA",
                home_score=5,
                away_score=0,
                tournament="Friendly",
                neutral=True,
            ),
            HistoricalResult(
                played_on=date(2026, 6, 9),
                home_team_id="PAR",
                away_team_id="USA",
                home_score=5,
                away_score=0,
                tournament="Friendly",
                neutral=True,
            ),
        ]

    def test_future_results_do_not_leak_into_features(self):
        context = ContextRepository(results=self.results, reports=[]).for_match(
            "USA", "PAR", self.cutoff
        )
        self.assertEqual(context.historical_matches_home, 1)
        self.assertEqual(context.historical_matches_away, 1)
        self.assertGreater(context.home_form_elo, context.away_form_elo)

    def test_sourced_availability_report_reduces_team_strength(self):
        report = AvailabilityReport(
            team_id="USA",
            player_name="Key Player",
            status="out",
            importance=1.0,
            confidence=1.0,
            published_at=self.cutoff - timedelta(hours=2),
            effective_from=self.cutoff - timedelta(hours=2),
            effective_until=self.cutoff + timedelta(days=2),
            source_url="https://example.com/report",
            source_name="Official federation",
            note="Test report",
        )
        context = ContextRepository(results=[], reports=[report], squads=[]).for_match(
            "USA", "PAR", self.cutoff
        )
        self.assertEqual(context.home_availability_elo, -35.0)
        self.assertEqual(context.availability_reports, 1)

    def test_context_changes_expected_goals(self):
        teams = {team.id: team for team in load_teams()}
        repository = ContextRepository(results=self.results, reports=[], squads=[])
        context = repository.for_match("USA", "PAR", self.cutoff)
        baseline = expected_goals(teams["USA"], teams["PAR"])
        adjusted = expected_goals(teams["USA"], teams["PAR"], context)
        self.assertGreater(adjusted[0], baseline[0])
        self.assertLess(adjusted[1], baseline[1])

    def test_incomplete_scheduled_results_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results_path = root / "results.csv"
            aliases_path = root / "aliases.json"
            aliases_path.write_text('{"USA":["United States"],"PAR":["Paraguay"]}')
            results_path.write_text(
                "date,home_team,away_team,home_score,away_score,tournament,city,country,neutral\n"
                "2026-05-01,United States,Paraguay,2,1,Friendly,Austin,USA,TRUE\n"
                "2026-06-12,United States,Paraguay,NA,NA,FIFA World Cup,Los Angeles,USA,TRUE\n"
            )
            loaded = load_historical_results(results_path, aliases_path)
            self.assertEqual(len(loaded), 1)

    def test_squad_omission_reduces_team_strength(self):
        selection = SquadSelection(
            team_id="PAR",
            player_name="Expected Starter",
            selection_status="omitted",
            importance=0.8,
            confidence=1.0,
            published_at=self.cutoff - timedelta(days=1),
            source_url="https://example.com/squad",
            source_name="Official federation",
            note="Test selection",
        )
        context = ContextRepository(
            results=[],
            reports=[],
            squads=[selection],
        ).for_match("USA", "PAR", self.cutoff)
        self.assertLess(context.away_availability_elo, 0)


class StandingsTests(unittest.TestCase):
    def test_points_goal_difference_and_goals_sort(self):
        table = build_table(
            ["A", "B", "C", "D"],
            [
                ("A", "B", 2, 0),
                ("C", "D", 1, 1),
                ("A", "C", 0, 0),
                ("B", "D", 3, 0),
                ("D", "A", 0, 1),
                ("B", "C", 1, 2),
            ],
        )
        self.assertEqual(table[0].team_id, "A")
        self.assertEqual(table[0].points, 7)


class SimulationTests(unittest.TestCase):
    def test_simulation_is_reproducible(self):
        first = simulate_tournament(iterations=5, seed=7)
        second = simulate_tournament(iterations=5, seed=7)
        self.assertEqual(first, second)
        self.assertAlmostEqual(
            sum(team["champion"] for team in first["teams"]),
            1.0,
            places=6,
        )
        self.assertEqual(
            first["monte_carlo_precision"]["worst_case_95_margin"],
            0.438269,
        )


if __name__ == "__main__":
    unittest.main()
