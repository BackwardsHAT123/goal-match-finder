# Goal Match Finder

Finds upcoming EPL, Championship, Champions League and MLS matches where:

1. both teams average 1.5+ goals/game this season (in that competition; Champions League clubs also count their domestic league games, since the league phase is only 8 matches)
2. their recent head-to-head meetings average 1.5+ total goals
3. both teams scored in at least 4 of their last 5 matches (same pooling for Champions League clubs)
4. Over 1.5 total goals is priced at -650 or better (best US book, or a manual SportsBetMT price), or, using the page's Over 2.5 toggle, Over 2.5 at -300 or better (`--odds-25` changes the cutoff)

Data: ESPN public feed (fixtures, results, head-to-head) and The Odds API (Over 1.5 prices).

## Run locally

    echo 'ODDS_API_KEY=your_key' > .env
    python3 finder.py --days 7      # writes site/index.html and site/matches.json
    python3 -m unittest

## Daily refresh

`.github/workflows/daily.yml` runs the tests, rebuilds the page and publishes it to GitHub Pages every day at 12:00 UTC.
It needs one repository secret, `ODDS_API_KEY`. Without it the page still builds, but Over 1.5 prices are missing.

## Manual prices

`manual_odds.json` holds prices from books the API does not cover, e.g. `"Home v Away": -625` for Over 1.5 and `"Home v Away [2.5]": -250` for Over 2.5.
Type a price into a card on the page and press **Enter** (or click away) to apply it; nothing updates while you type. Then press **Export SportsBetMT prices**, paste the result into `manual_odds.json`, and commit.
Remove a line once its match is played.

## Track record

With `--log` (the daily build does this) every flagged match is saved to `picks.json`. Once a match has finished,
the final score is added and the page shows hits and misses. Each settled match also gets a short post-mortem from
ESPN's event feed: red cards, penalties, own goals, 2+ injury delays, first-half substitutions, goals after 85',
and shot counts (many shots on target but few goals, or very low shot volume). The page tallies how often each
factor appeared in misses versus hits. Shot stats are missing for some matches. Samples are small, so treat the tallies as context.
