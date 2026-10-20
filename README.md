# Goal Match Finder

Finds upcoming EPL, Championship, Champions League and MLS matches where:

1. both teams average 1.5+ goals/game this season (in that competition)
2. their recent head-to-head meetings average 1.5+ total goals
3. both teams scored in at least 4 of their last 5 matches
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
Type a price into a card on the page, press **Export SportsBetMT prices**, paste the result into `manual_odds.json`, and commit.
Remove a line once its match is played.
