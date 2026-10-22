import unittest

from unittest import mock
from urllib.error import HTTPError

import finder
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


def ev(eid, date, me, mine, theirs, state="post"):
    """One ESPN schedule event: team `me` scored `mine`, opponent scored `theirs`."""
    return {"id": eid, "date": date, "competitions": [{"status": {"type": {"state": state}}, "competitors": [
        {"team": {"id": me}, "score": {"value": mine}}, {"team": {"id": "99"}, "score": {"value": theirs}}]}]}


class ChampionsLeaguePooling(unittest.TestCase):
    UCL = {"espn": "uefa.champions", "pool_domestic": True}

    def setUp(self):
        finder._domestic_slug.clear()
        self.calls = []

    def fake(self, schedules):
        """get_json stand-in: schedules maps slug -> events; unknown slugs 404 like ESPN."""
        def get_json(url, params=None, retries=4, missing_ok=False):
            slug = url.split("/soccer/")[1].split("/teams/")[0]
            self.calls.append(slug)
            if slug not in schedules:
                if missing_ok:
                    return {}
                raise HTTPError(url, 404, "nf", {}, None)
            return {"events": schedules[slug]}
        return mock.patch.object(finder, "get_json", get_json)

    def test_pools_ucl_and_domestic_in_date_order(self):
        sched = {"uefa.champions": [ev("u1", "2026-09-17", "5", 3, 1), ev("u2", "2026-10-01", "5", 2, 2)],
                 "esp.1": [ev("d1", "2026-08-20", "5", 1, 0), ev("d2", "2026-09-25", "5", 4, 0),
                           ev("d3", "2026-10-20", "5", 9, 9, state="pre")]}   # unplayed game ignored
        with self.fake(sched):
            res = finder.team_results(self.UCL, "5", 2026)
        self.assertEqual(res, [(1, 0), (3, 1), (4, 0), (2, 2)])

    def test_ucl_alone_is_too_few_games_but_pooled_passes(self):
        sched = {"uefa.champions": [ev("u1", "2026-09-17", "5", 3, 1), ev("u2", "2026-10-01", "5", 2, 2)],
                 "eng.1": [ev(f"d{i}", f"2026-08-{10+i}", "5", 2, 1) for i in range(5)]}
        with self.fake(sched):
            ucl_only = finder.team_games("uefa.champions", "5", 2026)
            pooled = finder.team_results(self.UCL, "5", 2026)
        self.assertFalse(recent_scoring_ok([(g[2], g[3]) for g in ucl_only]))
        self.assertEqual(len(pooled), 7)
        self.assertTrue(recent_scoring_ok(pooled))

    def test_wrong_slugs_404_and_winner_is_remembered(self):
        sched = {"uefa.champions": [], "ger.1": [ev("d1", "2026-08-20", "5", 2, 0)]}
        with self.fake(sched):
            finder.team_results(self.UCL, "5", 2026)
            n_first = len(self.calls)
            finder.team_results(self.UCL, "5", 2026)
        self.assertIn("ger.1", self.calls)
        self.assertEqual(len(self.calls) - n_first, 2)        # second pass: UCL + remembered slug only

    def test_missing_domestic_league_marks_incomplete(self):
        with self.fake({"uefa.champions": [ev("u1", "2026-09-17", "5", 3, 1)]}):
            self.assertIsNone(finder.team_results(self.UCL, "5", 2026))

    def test_domestic_leagues_are_not_pooled(self):
        sched = {"eng.1": [ev("d1", "2026-08-20", "5", 2, 0)]}
        with self.fake(sched):
            res = finder.team_results({"espn": "eng.1"}, "5", 2026)
        self.assertEqual(res, [(2, 0)])
        self.assertEqual(self.calls, ["eng.1"])


if __name__ == "__main__":
    unittest.main()
