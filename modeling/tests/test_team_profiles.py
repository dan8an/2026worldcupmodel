import unittest
from datetime import datetime, timezone

from modeling.src.data import load_teams
from modeling.src.features.context import load_historical_results
from modeling.src.team_profiles import (
    form_summary,
    key_players,
    load_squad_players,
    recent_results,
)


class TeamProfileTests(unittest.TestCase):
    def test_all_teams_have_key_players(self):
        players = load_squad_players()
        for team in load_teams():
            selected = key_players(team.id, players)
            self.assertEqual(len(selected), 4, team.id)
            self.assertTrue(all(player.name for player in selected))

    def test_recent_results_are_chronological_and_summarized(self):
        teams = load_teams()
        names = {team.id: team.name for team in teams}
        results = recent_results(
            "MEX",
            load_historical_results(),
            names,
            datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(len(results), 8)
        self.assertEqual(
            [result["played_on"] for result in results],
            sorted((result["played_on"] for result in results), reverse=True),
        )
        summary = form_summary(results)
        self.assertEqual(
            summary["wins"] + summary["draws"] + summary["losses"],
            summary["matches"],
        )


if __name__ == "__main__":
    unittest.main()
