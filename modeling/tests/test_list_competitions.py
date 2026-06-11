import io
import unittest

from scripts.list_competitions import (
    FILTER_TERMS,
    filter_competitions,
    format_seasons,
    parse_args,
    render_table,
)


COMPETITIONS = [
    {
        "league_id": 4,
        "name": "Euro Championship",
        "country": "World",
        "seasons": [2016, 2020, 2024],
    },
    {
        "league_id": 9,
        "name": "Copa America",
        "country": "World",
        "seasons": [2019, 2020, 2021, 2024],
    },
    {
        "league_id": 5,
        "name": "UEFA Nations League",
        "country": "World",
        "seasons": [2022, 2024],
    },
    {
        "league_id": 32,
        "name": "World Cup - Qualification Europe",
        "country": "World",
        "seasons": [2022, 2026],
    },
]


class ListCompetitionsTests(unittest.TestCase):
    def test_named_filters_are_available(self):
        args = parse_args(
            [
                "--filter",
                "euro",
                "--filter",
                "world-cup-qualification",
            ]
        )
        searches = [FILTER_TERMS[name] for name in args.filter]
        results = filter_competitions(COMPETITIONS, searches)
        self.assertEqual(
            [competition["league_id"] for competition in results],
            [4, 32],
        )

    def test_euro_filter_does_not_match_europe(self):
        results = filter_competitions(COMPETITIONS, ["Euro"])
        self.assertEqual([competition["league_id"] for competition in results], [4])

    def test_search_matches_name_or_country_and_ignores_punctuation(self):
        results = filter_competitions(
            COMPETITIONS,
            ["World Cup Qualification"],
        )
        self.assertEqual([competition["league_id"] for competition in results], [32])

    def test_seasons_are_compacted_without_hiding_gaps(self):
        self.assertEqual(
            format_seasons([2019, 2020, 2021, 2024]),
            "2019-2021, 2024",
        )

    def test_table_contains_required_columns(self):
        output = io.StringIO()
        render_table(COMPETITIONS[:1], output)
        table = output.getvalue()
        self.assertIn("League ID", table)
        self.assertIn("Competition", table)
        self.assertIn("Country", table)
        self.assertIn("Available seasons", table)
        self.assertIn("Euro Championship", table)


if __name__ == "__main__":
    unittest.main()
