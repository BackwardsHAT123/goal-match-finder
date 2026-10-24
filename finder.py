#!/usr/bin/env python3
"""Find upcoming soccer matches (EPL, Championship, UEFA, MLS) that pass four goal-friendly filters.

  1. Both teams average >= 1.5 goals scored per game this season (in that competition; for the
     Champions League, a club's domestic league games count too)
  2. Past head-to-head meetings average >= 1.5 total goals
  3. Both teams scored in >= 4 of their last 5 matches (in that competition)
  4. Over 1.5 pays -650 or better (e.g. -600 passes, -700 fails), or
     Over 2.5 pays -300 or better, at the best available book

Data: ESPN public feed (fixtures, results, last-5 H2H)  -> no key needed
      The Odds API (Over 1.5 line via alternate_totals, ~1 credit per candidate match; ESPN only carries the main total) -> optional env ODDS_API_KEY
"""
import argparse
import http.client
import difflib
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from statistics import median
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LEAGUES = {
    "epl": {"name": "Premier League", "espn": "eng.1", "odds": "soccer_epl", "euro": True},
    "championship": {"name": "Championship", "espn": "eng.2", "odds": "soccer_efl_champ", "euro": True},
    "ucl": {"name": "Champions League", "espn": "uefa.champions", "odds": "soccer_uefa_champs_league", "euro": True,
            "pool_domestic": True},  # league phase is only 8 games, so UCL form also counts each club's domestic league games
    "mls": {"name": "MLS", "espn": "usa.1", "odds": "soccer_usa_mls", "euro": False},
}

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
CACHE_DIR = Path(__file__).parent / ".cache"


def load_env_file():
    """Read KEY=value lines from soccer-finder/.env without overriding real environment variables."""
    f = Path(__file__).parent / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env_file()
CACHE_TTL = 3600  # seconds


# ---------------------------------------------------------------- criteria (pure)

def goals_for(games):
    """games: chronological list of (scored, conceded) for one team."""
    return [gf for gf, _ in games]


def season_avg_ok(games, minimum=1.5):
    gf = goals_for(games)
    return bool(gf) and sum(gf) / len(gf) >= minimum


def h2h_avg_ok(h2h_totals, minimum=1.5, allow_empty=False):
    if not h2h_totals:
        return allow_empty
    return sum(h2h_totals) / len(h2h_totals) >= minimum


def recent_scoring_ok(games, window=5, need=4):
    last = goals_for(games)[-window:]
    return len(last) == window and sum(1 for g in last if g >= 1) >= need


def odds_ok(american, threshold=-650):
    """True when the price pays at least as well as the threshold (-600 beats -650)."""
    return american is not None and american >= threshold


def consensus_price(prices):
    return int(median(prices)) if prices else None


# ---------------------------------------------------------------- name matching

def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|afc|cf|sc|club|the)\b", " ", s)
    s = s.replace("utd", "united").replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def same_team(a, b):
    na, nb = norm(a), norm(b)
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.82


# ---------------------------------------------------------------- HTTP

def get_json(url, params=None, retries=4, missing_ok=False):
    """GET JSON with cache and retries. None = failed. With missing_ok, a 404 returns {} (no retries)."""
    params = params or {}
    CACHE_DIR.mkdir(exist_ok=True)
    full = f"{url}?{urlencode(params)}" if params else url
    f = CACHE_DIR / hashlib.sha1(full.encode()).hexdigest()
    if f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL:
        return json.loads(f.read_text())
    for attempt in range(retries + 1):
        try:
            with urlopen(Request(full, headers={"User-Agent": "soccer-finder", "Accept-Encoding": "gzip"}), timeout=30) as r:
                body = r.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            data = json.loads(body)
            f.write_text(json.dumps(data))
            return data
        except (HTTPError, URLError, TimeoutError, http.client.HTTPException) as e:
            if missing_ok and isinstance(e, HTTPError) and e.code == 404:
                return {}
            if attempt == retries:
                print(f"warn: {full.split('?')[0]} failed: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))


def score(comp):
    s = comp.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def season_for(league, today):
    return today.year if not league["euro"] or today.month >= 7 else today.year - 1


def upcoming_fixtures(league, start, days):
    seen, out = set(), []
    for i in range(days + 1):
        d = (start + timedelta(days=i)).strftime("%Y%m%d")
        data = get_json(f"{ESPN}/{league['espn']}/scoreboard", {"dates": d})
        for ev in (data or {}).get("events", []):
            c = ev["competitions"][0]
            if ev["id"] in seen or c["status"]["type"]["state"] != "pre":
                continue
            seen.add(ev["id"])
            home = next(t for t in c["competitors"] if t["homeAway"] == "home")["team"]
            away = next(t for t in c["competitors"] if t["homeAway"] == "away")["team"]
            o = (c.get("odds") or [None])[0] or {}
            out.append({"id": ev["id"], "date": ev["date"], "home": home, "away": away,
                        "espn_total": o.get("overUnder"), "espn_over": o.get("overOdds"),
                        "espn_book": (o.get("provider") or {}).get("name")})
    return out


# Tried in order to find a Champions League club's domestic league (unknown slugs just 404 and are skipped).
DOMESTIC_SLUGS = ["eng.1", "esp.1", "ger.1", "ita.1", "fra.1", "ned.1", "por.1", "tur.1", "bel.1", "sco.1",
                  "aut.1", "gre.1", "den.1", "nor.1", "swe.1", "sui.1", "cze.1", "ukr.1", "cro.1", "srb.1",
                  "svk.1", "aze.1", "cyp.1", "isr.1", "bul.1", "kaz.1"]
_domestic_slug = {}  # team id -> slug that worked, so each club is probed once per run


def team_games(slug, team_id, season):
    """Completed games for a team in one ESPN competition -> chronological [(date, event_id, gf, ga)].
    None = fetch failed; [] = nothing there (including a 404 for the wrong competition)."""
    data = get_json(f"{ESPN}/{slug}/teams/{team_id}/schedule", {"season": season}, missing_ok=True)
    if data is None:
        return None  # fetch failed after retries; caller flags the match as incomplete
    games = []
    for ev in data.get("events", []):
        c = ev["competitions"][0]
        if c["status"]["type"]["state"] != "post":
            continue
        me = next((t for t in c["competitors"] if str(t["team"]["id"]) == str(team_id)), None)
        opp = next((t for t in c["competitors"] if t is not me), None)
        if me and opp and score(me) is not None and score(opp) is not None:
            games.append((ev["date"], str(ev["id"]), score(me), score(opp)))
    return sorted(games)


def domestic_games(team_id, season):
    """A Champions League club's domestic league games. None if they can't be found or a fetch failed."""
    for slug in ([_domestic_slug[team_id]] if team_id in _domestic_slug else DOMESTIC_SLUGS):
        games = team_games(slug, team_id, season)
        if games is None:
            return None
        if games:
            _domestic_slug[team_id] = slug
            return games
    return None


def team_results(league, team_id, season):
    """Completed games for a team -> chronological [(gf, ga)].
    Champions League clubs also get their domestic league games (only 8 league-phase matches exist)."""
    games = team_games(league["espn"], team_id, season)
    if games is None:
        return None
    if league.get("pool_domestic"):
        domestic = domestic_games(team_id, season)
        if domestic is None:
            return None  # can't see the club's league form: flag the match as incomplete instead of guessing
        games = sorted({g[1]: g for g in games + domestic}.values())
    return [(gf, ga) for _, _, gf, ga in games]


def h2h_totals(league, event_id):
    """Total goals in each of the last (up to 5) meetings, per ESPN's event summary."""
    data = get_json(f"{ESPN}/{league['espn']}/summary", {"event": event_id})
    totals = []
    for series in (data or {}).get("seasonseries", []):
        if series.get("type") != "head-to-head":
            continue
        for ev in series.get("events", []):
            if ev.get("status") != "post":
                continue
            sc = [score(c) for c in ev.get("competitors", [])]
            if len(sc) == 2 and None not in sc:
                totals.append(sum(sc))
    return totals


def over_prices(sport_key, home, away):
    """{line: [(American Over price, bookmaker), ...]} for the 1.5 and 2.5 lines; {} if no ODDS_API_KEY or no event.

    Both lines live in the alternate_totals market, which The Odds API only serves per event
    (1 credit per call for both lines), so this is called only for matches that already passed filters 1-3.
    """
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return {}
    base = f"https://api.the-odds-api.com/v4/sports/{sport_key}"
    events = get_json(f"{base}/events", {"apiKey": key}) or []  # free, no credit cost
    ev = next((e for e in events if same_team(e["home_team"], home) and same_team(e["away_team"], away)), None)
    if not ev:
        return {}
    data = get_json(f"{base}/events/{ev['id']}/odds",
                    {"apiKey": key, "regions": "us", "markets": "alternate_totals", "oddsFormat": "american"}) or {}
    out = {1.5: [], 2.5: []}
    for bk in data.get("bookmakers", []):
        for m in bk.get("markets", []):
            for o in m["outcomes"]:
                if o["name"] == "Over" and o.get("point") in out:
                    out[o["point"]].append((o["price"], bk["title"]))
    return out


# ---------------------------------------------------------------- pipeline

MANUAL_BOOK = "SportsBetMT (manual)"
LINES = (1.5, 2.5)


def load_manual_odds():
    """manual_odds.json: {"Home v Away": price} for Over 1.5 and {"Home v Away [2.5]": price} for Over 2.5."""
    f = Path(__file__).parent / "manual_odds.json"
    return json.loads(f.read_text()) if f.exists() else {}


def manual_price(manual, home, away, line=1.5):
    suffix = " [2.5]"
    for key, price in manual.items():
        if key.endswith(suffix) != (line == 2.5):
            continue
        h, _, a = key.removesuffix(suffix).partition(" v ")
        if same_team(h.strip(), home) and same_team(a.strip(), away):
            return price
    return None


def price_info(prices, manual_p, cutoff):
    """Summarise one market: API best, manual price, overall best, and whether it clears the cutoff."""
    info = {"manual": manual_p, "api": None, "api_book": None, "api_books": 0, "api_range": None,
            "best": None, "book": None, "ok": None}
    if prices:
        top = max(prices)  # highest American price pays the most
        info.update(api=top[0], api_book=top[1], api_books=len(prices), api_range=[min(p for p, _ in prices), top[0]])
    if manual_p is not None:
        prices = prices + [(manual_p, MANUAL_BOOK)]
    if prices:
        top = max(prices)
        info.update(best=top[0], book=top[1], ok=odds_ok(top[0], cutoff))
    return info


def avg(games):
    gf = goals_for(games)
    return round(sum(gf) / len(gf), 2) if gf else None


def evaluate(args):
    today = date.today()
    manual = load_manual_odds()
    cutoffs = {1.5: args.odds, 2.5: args.odds_25}
    rows = []
    for code in args.leagues:
        lg = LEAGUES[code]
        season = args.season or season_for(lg, today)
        fixtures = upcoming_fixtures(lg, today, args.days)
        for fx in fixtures:
            h, a = fx["home"], fx["away"]
            gh, ga = team_results(lg, h["id"], season), team_results(lg, a["id"], season)
            incomplete = gh is None or ga is None
            gh, ga = gh or [], ga or []
            row = {"league": lg["name"], "kickoff": fx["date"], "home": h["displayName"], "away": a["displayName"],
                   "home_logo": h.get("logo"), "away_logo": a.get("logo"),
                   "event_id": fx["id"], "league_code": code,
                   "avg": [avg(gh), avg(ga)], "games": [len(gh), len(ga)],
                   "last5": [sum(1 for g in goals_for(x)[-5:] if g >= 1) for x in (gh, ga)],
                   "espn_line": fx["espn_total"], "espn_over": fx["espn_over"], "espn_book": fx["espn_book"],
                   "incomplete": incomplete,
                   "c1": season_avg_ok(gh, args.min_avg) and season_avg_ok(ga, args.min_avg),
                   "c3": recent_scoring_ok(gh) and recent_scoring_ok(ga),
                   "h2h": None, "h2h_n": 0, "c2": None, "mk": None}
            if row["c1"] and row["c3"]:  # only spend extra calls on teams that pass the cheap checks
                tot = h2h_totals(lg, fx["id"])
                row["h2h_n"] = len(tot)
                row["h2h"] = round(sum(tot) / len(tot), 2) if tot else None
                row["c2"] = h2h_avg_ok(tot, args.min_h2h, args.allow_no_h2h)
                lines = over_prices(lg["odds"], h["displayName"], a["displayName"])
                row["mk"] = {str(line): price_info(lines.get(line, []), manual_price(manual, h["displayName"], a["displayName"], line),
                                                   cutoffs[line]) for line in LINES}
            rows.append(row)
    rows.sort(key=lambda r: r["kickoff"])
    return rows


# ---------------------------------------------------------------- pick log and settlement

LOG_FILE = Path(__file__).parent / "picks.json"


def load_log():
    return json.loads(LOG_FILE.read_text()) if LOG_FILE.exists() else {}


def save_log(log):
    LOG_FILE.write_text(json.dumps(log, indent=1, sort_keys=True) + "\n")


def result_from_score(home, away):
    total = home + away
    return {"home": home, "away": away, "total": total, "over15": total >= 2, "over25": total >= 3}


def log_picks(log, rows):
    """Record every match that passes filters 1-3; prices refresh on each run until the match kicks off."""
    today = date.today().isoformat()
    for r in rows:
        if not (r["c1"] and r["c3"] and r["c2"] is not False and r["mk"]):
            continue
        rec = log.setdefault(r["event_id"], {"flagged_on": today})
        if "result" in rec:
            continue
        rec.update({
            "event_id": r["event_id"], "league": r["league"], "league_code": r["league_code"],
            "kickoff": r["kickoff"], "home": r["home"], "away": r["away"],
            "stats": {"avg": r["avg"], "last5": r["last5"], "h2h": r["h2h"], "h2h_n": r["h2h_n"]},
            "markets": {ln: {"best": r["mk"][ln]["best"], "book": r["mk"][ln]["book"], "status": bucket(r, ln)}
                        for ln in ("1.5", "2.5")},
        })


FLAG_LABELS = {
    "red_card": "Red card", "penalty": "Penalty", "own_goal": "Own goal", "injury_delay": "2+ injury delays",
    "first_half_sub": "First-half sub", "late_goal": "Goal after 85'", "wasteful": "Many shots on target, few goals",
    "low_volume": "Low shot volume",
}


def _minute(ev):
    m = re.match(r"\d+", (ev.get("clock") or {}).get("displayValue") or "")
    return int(m.group()) if m else None


def _stat(team_stats, name):
    for s in team_stats or []:
        if s.get("name") == name:
            try:
                return float(s.get("displayValue"))
            except (TypeError, ValueError):
                return None
    return None


def postmortem(data):
    """Unusual factors in a finished match, from ESPN's event summary: {"flags": [...], "details": [...], "stats": {...}}."""
    flags, details, delays = [], [], []
    goals = 0
    for ev in (data or {}).get("keyEvents") or []:
        kind, minute = (ev.get("type") or {}).get("type") or "", _minute(ev)
        team = (ev.get("team") or {}).get("displayName", "")
        at = f" {minute}'" if minute is not None else ""
        if kind.startswith("goal") or kind == "penalty---scored" or kind == "own-goal":
            goals += 1
            if minute is not None and minute >= 85 and (ev.get("period") or {}).get("number", 2) <= 2:
                flags.append("late_goal"); details.append(f"Goal{at}")
        if kind == "red-card":
            flags.append("red_card"); details.append(f"Red card: {team}{at}")
        elif kind.startswith("penalty---"):
            flags.append("penalty"); details.append(f"Penalty {kind.split('---')[1]}: {team}{at}")
        elif kind == "own-goal":
            flags.append("own_goal"); details.append(f"Own goal{at}")
        elif kind == "start-delay" and "injury" in (ev.get("text") or "").lower():
            delays.append(f"Injury delay{at}")
        elif kind == "substitution" and (ev.get("period") or {}).get("number") == 1:
            flags.append("first_half_sub"); details.append(f"First-half sub: {team}{at}")
    if len(delays) >= 2:  # a single treatment stoppage happens in almost every match, so it says little
        flags.append("injury_delay"); details.extend(delays)
    stats = {}
    teams = (data or {}).get("boxscore", {}).get("teams") or []
    if len(teams) == 2 and teams[0].get("statistics"):
        for key, name in (("shots", "totalShots"), ("sot", "shotsOnTarget"), ("saves", "saves")):
            vals = [_stat(t.get("statistics"), name) for t in teams]
            if None not in vals:
                stats[key] = [int(v) for v in vals]
        if "sot" in stats and sum(stats["sot"]) >= 8 and goals <= 2:
            flags.append("wasteful"); details.append(f"{sum(stats['sot'])} shots on target, {goals} goals")
        if "shots" in stats and sum(stats["shots"]) < 15:
            flags.append("low_volume"); details.append(f"Only {sum(stats['shots'])} total shots")
    return {"flags": sorted(set(flags)), "details": details, "stats": stats}


def flag_counts(log, line):
    """{flag: (in_misses, misses, in_hits, hits)} over settled picks, for one market."""
    key = "over15" if line == "1.5" else "over25"
    done = [r for r in log.values() if "result" in r and "notes" in r]
    miss = [r for r in done if not r["result"][key]]
    hit = [r for r in done if r["result"][key]]
    return {f: (sum(f in r["notes"]["flags"] for r in miss), len(miss), sum(f in r["notes"]["flags"] for r in hit), len(hit))
            for f in FLAG_LABELS if any(f in r["notes"]["flags"] for r in done)}


def settle_log(log):
    """Fetch final scores and post-mortem notes for logged matches that should be over. Returns newly settled count."""
    now = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() - 3 * 3600))  # allow ~3h for the match to finish
    settled = 0
    for rec in log.values():
        fresh = "result" not in rec
        if (fresh and rec["kickoff"] > now) or (not fresh and "notes" in rec):
            continue
        data = get_json(f"{ESPN}/{LEAGUES[rec['league_code']]['espn']}/summary", {"event": rec["event_id"]})
        comp = ((data or {}).get("header", {}).get("competitions") or [None])[0]
        if not comp or comp["status"]["type"]["state"] != "post":
            continue
        if fresh:
            sc = {c["homeAway"]: score(c) for c in comp["competitors"]}
            if sc.get("home") is None or sc.get("away") is None:
                continue
            rec["result"] = {**result_from_score(sc["home"], sc["away"]), "settled_on": date.today().isoformat()}
            settled += 1
        rec["notes"] = postmortem(data)
    return settled


def track_record(log, line):
    """(hits, total) of settled picks for a market: those flagged as qualifying, and all logged."""
    key = "over15" if line == "1.5" else "over25"
    done = [r for r in log.values() if "result" in r]
    qual = [r for r in done if r["markets"][line]["status"] == "qualifying"]
    return ((sum(r["result"][key] for r in qual), len(qual)),
            (sum(r["result"][key] for r in done), len(done)))


def bucket(r, line="1.5"):
    if not (r["c1"] and r["c3"]) or r["c2"] is False or r["mk"] is None:
        return "out"
    ok = r["mk"][line]["ok"]
    return "pending_odds" if ok is None else ("qualifying" if ok else "below")


def fmt_odds(n):
    return "–" if n is None else f"{n:+d}"


def fmt_time(iso):
    return iso.replace("T", " ")[:16] + " UTC"


CSS = """:root{--bg:#f4f6f5;--fg:#14201b;--card:#ffffff;--line:#dbe3df;--muted:#5d6f67;--accent:#0b7a4b;--accent-bg:#e1f3ea;--warn:#8a5a00;--warn-bg:#fdf0d5}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1512;--fg:#e6eee9;--card:#16201b;--line:#27352e;--muted:#93a69c;--accent:#3ccf8e;--accent-bg:#17352a;--warn:#f0c35e;--warn-bg:#3a2e12;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0e1512;--fg:#e6eee9;--card:#16201b;--line:#27352e;--muted:#93a69c;--accent:#3ccf8e;--accent-bg:#17352a;--warn:#f0c35e;--warn-bg:#3a2e12;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;padding-block:24px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:auto}h1{margin:0 0 4px;font-size:26px}h2{margin:32px 0 4px;font-size:18px}.n{background:var(--accent-bg);color:var(--accent);margin-left:8px;padding:1px 9px;border-radius:99px;font-size:13px}
.muted{color:var(--muted)}.empty{color:var(--muted);padding:16px;border:1px dashed var(--line);border-radius:10px;margin:12px 0}
article{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
article header{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:13px}.lg{font-weight:600}
h3{margin:6px 0 10px;font-size:18px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}h3 i{color:var(--muted);font-weight:400}
h3 img{width:22px;height:22px;object-fit:contain}dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0}
dt{font-size:12px;color:var(--muted)}dd{margin:0;font-weight:600;font-variant-numeric:tabular-nums;min-width:0}small{font-weight:400;color:var(--muted)}
.mk{margin-top:10px}main[data-market="1.5"] .m25,main[data-market="2.5"] .m15{display:none}
.sb{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.sb label{font-size:13px;color:var(--muted)}input[type=text]{width:96px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit;font-variant-numeric:tabular-nums}
input[type=text]:focus-visible,button:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input.bad{border-color:var(--warn)}.hint{font-size:12px;color:var(--warn)}
button{padding:7px 14px;border:1px solid var(--accent);border-radius:8px;background:var(--accent-bg);color:var(--accent);font:inherit;font-weight:600;cursor:pointer}
.seg{display:inline-flex;margin:14px 0 0}.seg button{border-radius:0;background:transparent}.seg button:first-child{border-radius:8px 0 0 8px}.seg button:last-child{border-radius:0 8px 8px 0;border-left:0}
.seg button[aria-pressed="true"]{background:var(--accent);color:var(--bg)}
.tbl{overflow-x:auto;margin-top:10px}table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:12px;color:var(--muted);font-weight:600}.notes{white-space:normal;min-width:220px;max-width:420px;font-weight:400}.hit{color:var(--accent);font-weight:600}.miss{color:var(--warn);font-weight:600}
.export{margin-top:32px;padding-top:16px;border-top:1px solid var(--line)}textarea{width:100%;min-height:110px;margin-top:8px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:13px ui-monospace,monospace}"""

JS = """(function(){
var CUT={'1.5':__C15__,'2.5':__C25__}, STORE='sbmt-manual-odds', BOOK='SportsBetMT', MK=['1.5','2.5'];
var saved={},market='1.5';
try{saved=JSON.parse(localStorage.getItem(STORE)||'{}')}catch(e){}
try{if(localStorage.getItem('gmf-market')==='2.5')market='2.5'}catch(e){}
var main=document.querySelector('main');
var cards=[].slice.call(document.querySelectorAll('article[data-key]'));
var secs={qual:document.getElementById('sec-qual'),pend:document.getElementById('sec-pend'),below:document.getElementById('sec-below')};
function q(m){return m.replace('.','')}
function sk(key,m){return m==='1.5'?key:key+' [2.5]'}
function fmt(n){return n===null?'\\u2013':(n>0?'+':'')+n}
function parse(v){v=String(v).trim().replace('\\u2212','-');if(!/^[+-]?\\d{3,4}$/.test(v))return null;var n=parseInt(v,10);return(n<=-100||n>=100)?n:null}
function persist(){try{localStorage.setItem(STORE,JSON.stringify(saved))}catch(e){}}
function hint(c,m){
  var p='.m'+q(m)+' ',inp=c.querySelector(p+'input'),raw=inp.value.trim(),done=inp.dataset.done||'';
  var bad=raw===done&&raw!==''&&parse(raw)===null;
  inp.classList.toggle('bad',bad);
  c.querySelector(p+'.hint').textContent=raw!==done?'Press Enter to apply':(bad?'Use American odds like -625':'');
}
function calc(c,m){
  var p='.m'+q(m)+' ',inp=c.querySelector(p+'input'),man=parse(inp.dataset.done||'');
  hint(c,m);
  var a=c.dataset['api'+q(m)],api=a===''?null:+a,price=api,src=c.dataset['book'+q(m)]||'';
  if(man!==null&&(api===null||man>api)){price=man;src=BOOK+' (manual)'}
  c.querySelector(p+'.best').textContent=fmt(price);
  c.querySelector(p+'.src').textContent=price===null?'no line yet':src;
  return price;
}
function update(c){
  var price=null;
  MK.forEach(function(m){var pr=calc(c,m);if(m===market)price=pr});
  secs[price===null?'pend':(price>=CUT[market]?'qual':'below')].appendChild(c);
}
function refresh(){
  Object.keys(secs).forEach(function(k){
    var s=secs[k],n=s.querySelectorAll('article').length;
    document.getElementById('cnt-'+k).textContent=n;
    s.querySelector('.empty').hidden=n>0;
  });
}
function setMarket(m){
  market=m;main.dataset.market=m;
  try{localStorage.setItem('gmf-market',m)}catch(e){}
  [].forEach.call(document.querySelectorAll('.seg button'),function(b){b.setAttribute('aria-pressed',b.dataset.m===m?'true':'false')});
  [].forEach.call(document.querySelectorAll('.mname'),function(s){s.textContent=m});
  [].forEach.call(document.querySelectorAll('.cut'),function(s){s.textContent=fmt(CUT[m])});
  cards.forEach(update);refresh();
}
cards.forEach(function(c){
  MK.forEach(function(m){
    var key=sk(c.dataset.key,m),inp=c.querySelector('.m'+q(m)+' input');
    inp.value=(key in saved)?saved[key]:c.dataset['man'+q(m)];
    inp.dataset.done=inp.value.trim();
    function commit(){
      var v=inp.value.trim();
      if(v===inp.dataset.done)return;
      inp.dataset.done=v;saved[key]=v;persist();update(c);refresh();
    }
    inp.addEventListener('input',function(){hint(c,m)});
    inp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();commit();hint(c,m)}});
    inp.addEventListener('change',commit);
  });
});
[].forEach.call(document.querySelectorAll('.seg button'),function(b){b.addEventListener('click',function(){setMarket(b.dataset.m)})});
setMarket(market);
var out=document.getElementById('export-out');
document.getElementById('export-btn').addEventListener('click',function(){
  var o={};cards.forEach(function(c){MK.forEach(function(m){var v=parse(c.querySelector('.m'+q(m)+' input').value);if(v!==null)o[sk(c.dataset.key,m)]=v})});
  out.value=JSON.stringify(o,null,2);out.hidden=false;out.select();
  var msg=document.getElementById('export-msg');
  try{navigator.clipboard.writeText(out.value).then(function(){msg.textContent='Copied. Paste into manual_odds.json.'},function(){msg.textContent='Select and copy the text, then paste into manual_odds.json.'})}
  catch(e){msg.textContent='Select and copy the text, then paste into manual_odds.json.'}
});
})();"""


def render_html(rows, args, log=None):
    def esc(x):
        return html.escape(str(x), quote=True)

    def market_block(r, key, line, iid):
        info, s = r["mk"][line], line.replace(".", "")
        api = (f"{fmt_odds(info['api'])} <small>{esc(info['api_book'])} · worst {fmt_odds(info['api_range'][0])} ({info['api_books']} books)</small>"
               if info["api"] is not None else "<span class=muted>no line</span>")
        return f"""<div class="mk m{s}">
   <dl>
    <div><dt>Over {line} best price</dt><dd><span class=best></span> <small class=src></small></dd></div>
    <div><dt>US books (API)</dt><dd>{api}</dd></div>
   </dl>
   <div class=sb><label for="{iid}{s}">SportsBetMT Over {line}</label><input type=text id="{iid}{s}" inputmode=numeric autocomplete=off placeholder="{'-625' if line == '1.5' else '-250'}"><span class=hint></span></div>
  </div>"""

    def card(r):
        key = f"{r['home']} v {r['away']}"
        logo = lambda u: f"<img src='{esc(u)}' alt=''>" if u else ""
        h2h = f"{r['h2h']} <small>({r['h2h_n']} games)</small>" if r["h2h"] is not None else "–"
        espn = (f"{esc(r['espn_book'] or 'ESPN')} O{r['espn_line']} {fmt_odds(int(r['espn_over'])) if r['espn_over'] is not None else ''}"
                if r["espn_line"] else "–")
        iid = "m" + re.sub(r"[^a-z0-9]", "", norm(key))[:40]
        attrs = " ".join(
            f'data-api{ln.replace(".", "")}="{"" if r["mk"][ln]["api"] is None else r["mk"][ln]["api"]}" '
            f'data-book{ln.replace(".", "")}="{esc(r["mk"][ln]["api_book"] or "")}" '
            f'data-man{ln.replace(".", "")}="{"" if r["mk"][ln]["manual"] is None else r["mk"][ln]["manual"]}"'
            for ln in ("1.5", "2.5"))
        return f"""<article data-key="{esc(key)}" {attrs}>
  <header><span class=lg>{esc(r['league'])}</span><time>{fmt_time(r['kickoff'])}</time></header>
  <h3>{logo(r['home_logo'])}{esc(r['home'])} <i>v</i> {logo(r['away_logo'])}{esc(r['away'])}</h3>
  <dl>
   <div><dt>Goals/gm (home / away)</dt><dd>{r['avg'][0]} / {r['avg'][1]}</dd></div>
   <div><dt>Scored in last 5</dt><dd>{r['last5'][0]} / {r['last5'][1]}</dd></div>
   <div><dt>H2H avg total</dt><dd>{h2h}</dd></div>
   <div><dt>ESPN main total</dt><dd>{espn}</dd></div>
  </dl>
  {market_block(r, key, "1.5", iid)}
  {market_block(r, key, "2.5", iid)}
</article>"""

    cands = [r for r in rows if r["c1"] and r["c3"] and r["c2"] is not False and r["mk"]]
    n_inc = sum(1 for r in rows if r.get("incomplete"))
    incomplete_note = f" ({n_inc} could not be fully checked because ESPN data was missing or failed to load; re-run)" if n_inc else ""
    crit = (f"avg goals ≥ {args.min_avg} · H2H avg ≥ {args.min_h2h} · scored in ≥4 of last 5 · "
            f"Over 1.5 at {args.odds} or better, or Over 2.5 at {args.odds_25} or better")

    def sec(k, title, note):
        return (f"<section id=sec-{k}><h2>{title} <span class=n id=cnt-{k}>0</span></h2><p class=muted>{note}</p>"
                f"<p class=empty hidden>None right now.</p></section>")

    body = (sec("qual", "Qualifying", "Passes filters 1–3 and the best Over <span class=mname></span> price is <span class=cut></span> or better.")
            + sec("pend", "Needs price", "Passes filters 1–3 but no book has an Over <span class=mname></span> line yet. Enter a SportsBetMT price on a card to check it.")
            + sec("below", "Price too short", "Passes filters 1–3, but the best Over <span class=mname></span> price is worse than <span class=cut></span>. A better SportsBetMT price moves it up."))
    cards = "\n".join(card(r) for r in cands)

    def results_section():
        done = sorted((r for r in (log or {}).values() if "result" in r), key=lambda r: r["kickoff"], reverse=True)
        if not done:
            return ("<section><h2>Track record</h2><p class=empty>No settled picks yet. Matches are logged when flagged "
                    "and scored once they finish.</p></section>")
        lines = []
        for ln in ("1.5", "2.5"):
            (qh, qn), (ah, an) = track_record(log, ln)
            lines.append(f"<li>Over {ln}: <b>{qh} of {qn}</b> qualifying picks hit · {ah} of {an} of all logged picks hit</li>")
        def mark(rec, ln):
            hit = rec["result"]["over15" if ln == "1.5" else "over25"]
            tag = {"qualifying": "Q", "pending_odds": "no price", "below": "too short"}[rec["markets"][ln]["status"]]
            return f"<span class={'hit' if hit else 'miss'}>{'Hit' if hit else 'Miss'}</span> <small>({tag})</small>"
        def notes(rec):
            n = rec.get("notes")
            if not n:
                return "<small>not available</small>"
            return esc("; ".join(n["details"])) or "<small>nothing unusual</small>"
        trs = "".join(
            f"<tr><td>{esc(r['kickoff'][:10])}</td><td>{esc(r['home'])} v {esc(r['away'])}</td>"
            f"<td>{r['result']['home']}-{r['result']['away']}</td><td>{mark(r, '1.5')}</td><td>{mark(r, '2.5')}</td>"
            f"<td class=notes>{notes(r)}</td></tr>"
            for r in done[:40])
        factor_html = ""
        for ln in ("1.5", "2.5"):
            fc = flag_counts(log, ln)
            if fc:
                rows_ = "".join(f"<tr><td>{FLAG_LABELS[f]}</td><td>{mi} of {mt}</td><td>{hi} of {ht}</td></tr>"
                                for f, (mi, mt, hi, ht) in fc.items())
                factor_html += (f"<h3>Over {ln}: what came with misses and hits</h3><div class=tbl><table><thead><tr><th>Factor</th>"
                                f"<th>Present in misses</th><th>Present in hits</th></tr></thead><tbody>{rows_}</tbody></table></div>")
        return (f"<section><h2>Track record <span class=n>{len(done)}</span></h2>"
                f"<p class=muted>Settled picks since {esc(min(r['flagged_on'] for r in done))}. Q = qualifying when last seen. "
                f"Small samples say little, so treat these as context.</p><ul>{''.join(lines)}</ul>"
                f"<div class=tbl><table><thead><tr><th>Date</th><th>Match</th><th>Score</th><th>Over 1.5</th><th>Over 2.5</th><th>Notes</th></tr></thead>"
                f"<tbody>{trs}</tbody></table></div>{factor_html}</section>")

    seg = """<div class=seg role=group aria-label="Market"><button type=button data-m="1.5" aria-pressed="true">Over 1.5</button><button type=button data-m="2.5" aria-pressed="false">Over 2.5</button></div>"""
    export = """<div class=export><button type=button id=export-btn>Export SportsBetMT prices</button>
<span class=muted id=export-msg></span><textarea id=export-out hidden readonly aria-label="manual_odds.json contents"></textarea></div>"""
    js = JS.replace("__C15__", str(args.odds)).replace("__C25__", str(args.odds_25))
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Goal Match Finder</title><style>{CSS}</style></head><body><main data-market="1.5">
<h1>Goal Match Finder</h1>
<p class=muted>Generated {time.strftime('%Y-%m-%d %H:%M')} · next {args.days} days · {crit}<br>
Scanned {len(rows)} fixtures{incomplete_note} · season goals and last 5 are competition-only (Champions League clubs also count their domestic league games) · H2H is ESPN's last 5 meetings.</p>
{seg}
{body}
<div hidden id=card-pool>{cards}</div>
{results_section()}
{export}
</main><script>{js}</script></body></html>"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--leagues", nargs="+", choices=LEAGUES, default=list(LEAGUES))
    p.add_argument("--days", type=int, default=3, help="look-ahead window (default 3)")
    p.add_argument("--season", type=int, help="override season start year")
    p.add_argument("--min-avg", type=float, default=1.5)
    p.add_argument("--min-h2h", type=float, default=1.5)
    p.add_argument("--odds", type=int, default=-650, help="American odds cutoff for Over 1.5, -650 or better (default -650)")
    p.add_argument("--odds-25", type=int, default=-300, help="American odds cutoff for Over 2.5, -300 or better (default -300)")
    p.add_argument("--allow-no-h2h", action="store_true", help="pass teams that have never met")
    p.add_argument("--log", action="store_true", help="record flagged matches in picks.json and settle finished ones (used by the daily build)")
    p.add_argument("--out", default=str(Path(__file__).parent / "site" / "index.html"))
    args = p.parse_args(argv)
    rows = evaluate(args)
    log = load_log()
    if args.log:
        log_picks(log, rows)
        newly = settle_log(log)
        save_log(log)
        print(f"pick log: {len(log)} picks, {sum('result' in r for r in log.values())} settled ({newly} new)")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(rows, args, log))
    (out.parent / "matches.json").write_text(json.dumps(rows, indent=1))
    for line in ("1.5", "2.5"):
        counts = {k: sum(1 for r in rows if bucket(r, line) == k) for k in ("qualifying", "pending_odds", "below")}
        print(f"Over {line}: {counts['qualifying']} qualifying, {counts['pending_odds']} need a price, {counts['below']} priced too short")
    print(f"{len(rows)} fixtures scanned")
    n_inc = sum(1 for r in rows if r.get("incomplete"))
    if n_inc:
        print(f"warning: {n_inc} fixtures had ESPN data fail to load and were not fully checked; re-run to retry")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
