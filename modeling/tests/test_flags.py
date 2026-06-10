import unittest

from modeling.src.data import load_teams
from modeling.src.flags import TEAM_FLAG_CODES, flag_for_team


class FlagTests(unittest.TestCase):
    def test_every_tournament_team_has_a_flag(self):
        teams = load_teams()
        self.assertEqual({team.id for team in teams}, set(TEAM_FLAG_CODES))
        self.assertTrue(all(flag_for_team(team.id) for team in teams))

    def test_common_and_subdivision_flags(self):
        self.assertEqual(flag_for_team("USA"), "🇺🇸")
        self.assertEqual(flag_for_team("MEX"), "🇲🇽")
        self.assertEqual(
            [hex(ord(character)) for character in flag_for_team("ENG")],
            ["0x1f3f4", "0xe0067", "0xe0062", "0xe0065", "0xe006e", "0xe0067", "0xe007f"],
        )
        self.assertEqual(
            [hex(ord(character)) for character in flag_for_team("SCO")],
            ["0x1f3f4", "0xe0067", "0xe0062", "0xe0073", "0xe0063", "0xe0074", "0xe007f"],
        )


if __name__ == "__main__":
    unittest.main()
