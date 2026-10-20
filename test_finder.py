import unittest

from finder import (consensus_price, h2h_avg_ok, odds_ok, recent_scoring_ok,
                    same_team, season_avg_ok)


class Criteria(unittest.TestCase):
    def test_season_avg(self):
        self.assertTrue(season_avg_ok([(2, 0), (1, 1)]))      # 1.5 exactly
        self.assertFalse(season_avg_ok([(1, 0), (1, 1)]))
        self.assertFalse(season_avg_ok([]))

    def test_h2h(self):
        self.assertTrue(h2h_avg_ok([1, 2]))
        self.assertFalse(h2h_avg_ok([1, 1]))
        self.assertFalse(h2h_avg_ok([]))
        self.assertTrue(h2h_avg_ok([], allow_empty=True))

    def test_recent_scoring(self):
        self.assertTrue(recent_scoring_ok([(0, 1), (1, 0), (2, 0), (1, 1), (3, 0), (1, 2)]))   # 4 of last 5
        self.assertFalse(recent_scoring_ok([(1, 0), (0, 1), (0, 0), (1, 1), (2, 0)]))           # 3 of 5
        self.assertFalse(recent_scoring_ok([(1, 0)] * 4))                                       # <5 games

    def test_odds(self):
        self.assertTrue(odds_ok(-650))
        self.assertTrue(odds_ok(-600))
        self.assertTrue(odds_ok(120))
        self.assertFalse(odds_ok(-800))
        self.assertFalse(odds_ok(None))

    def test_consensus(self):
        self.assertEqual(consensus_price([-700, -650, -600]), -650)
        self.assertIsNone(consensus_price([]))

    def test_name_match(self):
        self.assertTrue(same_team("Manchester United", "Manchester Utd"))
        self.assertTrue(same_team("Brighton and Hove Albion", "Brighton & Hove Albion"))
        self.assertFalse(same_team("Manchester United", "Manchester City"))


if __name__ == "__main__":
    unittest.main()
