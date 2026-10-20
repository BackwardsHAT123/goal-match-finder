#!/usr/bin/env python3
"""Find upcoming soccer matches (EPL, Championship, UEFA, MLS) that pass four goal-friendly filters.

  1. Both teams average >= 1.5 goals scored per game this season (in that competition)
  2. Past head-to-head meetings average >= 1.5 total goals
  3. Both teams scored in >= 4 of their last 5 matches (in that competition)
  4. Over 1.5 total goals pays -650 or better at the best US book (e.g. -600 passes, -700 fails)

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
    "ucl": {"name": "Champions League", "espn": "uefa.champions", "odds": "soccer_uefa_champs_league", "euro": True},
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

def get_json(url, params=None, retries=4):
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


def team_results(league, team_id, season):
    """Completed games for a team in this competition -> chronological [(gf, ga)]."""
    data = get_json(f"{ESPN}/{league['espn']}/teams/{team_id}/schedule", {"season": season})
    if data is None:
        return None  # fetch failed after retries; caller flags the match as incomplete
    games = []
    for ev in (data or {}).get("events", []):
        c = ev["competitions"][0]
        if c["status"]["type"]["state"] != "post":
            continue
        me = next((t for t in c["competitors"] if str(t["team"]["id"]) == str(team_id)), None)
        opp = next((t for t in c["competitors"] if t is not me), None)
        if me and opp and score(me) is not None and score(opp) is not None:
            games.append((ev["date"], score(me), score(opp)))
    return [(gf, ga) for _, gf, ga in sorted(games)]


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


def over15_prices(sport_key, home, away):
    """(American Over 1.5 price, bookmaker) pairs for one match; [] if no ODDS_API_KEY or no line.

    The 1.5 line lives in the alternate_totals market, which The Odds API only serves per event
    (1 credit per call), so this is called only for matches that already passed filters 1-3.
    """
    key = os.environ.get("ODDS_API_KEY")
    if not key:
        return []
    base = f"https://api.the-odds-api.com/v4/sports/{sport_key}"
    events = get_json(f"{base}/events", {"apiKey": key}) or []  # free, no credit cost
    ev = next((e for e in events if same_team(e["home_team"], home) and same_team(e["away_team"], away)), None)
    if not ev:
        return []
    data = get_json(f"{base}/events/{ev['id']}/odds",
                    {"apiKey": key, "regions": "us", "markets": "alternate_totals", "oddsFormat": "american"}) or {}
    return [(o["price"], bk["title"]) for bk in data.get("bookmakers", []) for m in bk.get("markets", [])
            for o in m["outcomes"] if o["name"] == "Over" and o.get("point") == 1.5]


# ---------------------------------------------------------------- pipeline

MANUAL_BOOK = "SportsBetMT (manual)"


def load_manual_odds():
    """manual_odds.json: {"Home v Away": american_price} typed in from a book the API does not cover."""
    f = Path(__file__).parent / "manual_odds.json"
    return json.loads(f.read_text()) if f.exists() else {}


def manual_price(manual, home, away):
    for key, price in manual.items():
        h, _, a = key.partition(" v ")
        if same_team(h.strip(), home) and same_team(a.strip(), away):
            return price
    return None

def avg(games):
    gf = goals_for(games)
    return round(sum(gf) / len(gf), 2) if gf else None


def evaluate(args):
    today = date.today()
    manual = load_manual_odds()
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
                   "avg": [avg(gh), avg(ga)], "games": [len(gh), len(ga)],
                   "last5": [sum(1 for g in goals_for(x)[-5:] if g >= 1) for x in (gh, ga)],
                   "espn_line": fx["espn_total"], "espn_over": fx["espn_over"], "espn_book": fx["espn_book"],
                   "incomplete": incomplete,
                   "c1": season_avg_ok(gh, args.min_avg) and season_avg_ok(ga, args.min_avg),
                   "c3": recent_scoring_ok(gh) and recent_scoring_ok(ga),
                   "h2h": None, "h2h_n": 0, "c2": None, "manual": None,
                   "o15_api": None, "o15_api_book": None, "o15_api_books": 0, "o15_api_range": None, "o15": None, "o15_range": None, "c4": None}
            if row["c1"] and row["c3"]:  # only spend extra calls on teams that pass the cheap checks
                tot = h2h_totals(lg, fx["id"])
                row["h2h_n"] = len(tot)
                row["h2h"] = round(sum(tot) / len(tot), 2) if tot else None
                row["c2"] = h2h_avg_ok(tot, args.min_h2h, args.allow_no_h2h)
                prices = over15_prices(lg["odds"], h["displayName"], a["displayName"])
                mp = manual_price(manual, h["displayName"], a["displayName"])
                row["manual"] = mp
                if prices:
                    api_best = max(prices)
                    row.update(o15_api=api_best[0], o15_api_book=api_best[1], o15_api_books=len(prices),
                               o15_api_range=[min(p for p, _ in prices), api_best[0]])
                if mp is not None:
                    prices.append((mp, MANUAL_BOOK))
                if prices:
                    best = max(prices)  # highest American price pays the most
                    row["o15"], row["o15_book"], row["o15_books"] = best[0], best[1], len(prices)
                    row["o15_range"] = [min(p for p, _ in prices), best[0]]
                    row["c4"] = odds_ok(best[0], args.odds)
            rows.append(row)
    rows.sort(key=lambda r: r["kickoff"])
    return rows


def bucket(r):
    if not (r["c1"] and r["c3"]) or r["c2"] is False or r["c4"] is False:
        return "out"
    return "qualifying" if r["c4"] else "pending_odds"  # c4 None -> no Over 1.5 line available


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
.sb{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.sb label{font-size:13px;color:var(--muted)}input[type=text]{width:96px;padding:6px 8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit;font-variant-numeric:tabular-nums}
input[type=text]:focus-visible,button:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input.bad{border-color:var(--warn)}.hint{font-size:12px;color:var(--warn)}
button{padding:7px 14px;border:1px solid var(--accent);border-radius:8px;background:var(--accent-bg);color:var(--accent);font:inherit;font-weight:600;cursor:pointer}
.export{margin-top:32px;padding-top:16px;border-top:1px solid var(--line)}textarea{width:100%;min-height:110px;margin-top:8px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);font:13px ui-monospace,monospace}"""

JS = """(function(){
var CUTOFF=__CUTOFF__, KEY='sbmt-manual-odds', BOOK='SportsBetMT';
var saved={};try{saved=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
var cards=[].slice.call(document.querySelectorAll('article[data-key]'));
var secs={qual:document.getElementById('sec-qual'),pend:document.getElementById('sec-pend'),below:document.getElementById('sec-below')};
function fmt(n){return n===null?'\\u2013':(n>0?'+':'')+n}
function parse(v){v=String(v).trim().replace('\\u2212','-');if(!/^[+-]?\\d{3,4}$/.test(v))return null;var n=parseInt(v,10);return(n<=-100||n>=100)?n:null}
function persist(){try{localStorage.setItem(KEY,JSON.stringify(saved))}catch(e){}}
function update(c){
  var inp=c.querySelector('input'),raw=inp.value.trim(),man=parse(raw);
  inp.classList.toggle('bad',raw!==''&&man===null);
  c.querySelector('.hint').textContent=(raw!==''&&man===null)?'Use American odds like -625':'';
  var api=c.dataset.api===''?null:+c.dataset.api,price=api,src=c.dataset.book||'';
  if(man!==null&&(api===null||man>api)){price=man;src=BOOK+' (manual)'}
  c.querySelector('.best').textContent=fmt(price);
  c.querySelector('.src').textContent=price===null?'no line yet':src;
  var k=price===null?'pend':(price>=CUTOFF?'qual':'below');
  secs[k].appendChild(c);
}
function refresh(){
  Object.keys(secs).forEach(function(k){
    var s=secs[k],n=s.querySelectorAll('article').length;
    document.getElementById('cnt-'+k).textContent=n;
    s.querySelector('.empty').hidden=n>0;
  });
}
cards.forEach(function(c){
  var key=c.dataset.key,inp=c.querySelector('input');
  inp.value=(key in saved)?saved[key]:c.dataset.manual;
  inp.addEventListener('input',function(){saved[key]=inp.value.trim();persist();update(c);refresh()});
  update(c);
});
refresh();
var out=document.getElementById('export-out');
document.getElementById('export-btn').addEventListener('click',function(){
  var o={};cards.forEach(function(c){var v=parse(c.querySelector('input').value);if(v!==null)o[c.dataset.key]=v});
  out.value=JSON.stringify(o,null,2);out.hidden=false;out.select();
  var msg=document.getElementById('export-msg');
  try{navigator.clipboard.writeText(out.value).then(function(){msg.textContent='Copied. Paste into manual_odds.json.'},function(){msg.textContent='Select and copy the text, then paste into manual_odds.json.'})}
  catch(e){msg.textContent='Select and copy the text, then paste into manual_odds.json.'}
});
})();"""


def render_html(rows, args):
    def esc(x):
        return html.escape(str(x), quote=True)

    def card(r):
        key = f"{r['home']} v {r['away']}"
        logo = lambda u: f"<img src='{esc(u)}' alt=''>" if u else ""
        h2h = f"{r['h2h']} <small>({r['h2h_n']} games)</small>" if r["h2h"] is not None else "–"
        api = (f"{fmt_odds(r['o15_api'])} <small>{esc(r['o15_api_book'])} · worst {fmt_odds(r['o15_api_range'][0])} ({r['o15_api_books']} books)</small>"
               if r["o15_api"] is not None else "<span class=muted>no line</span>")
        espn = (f"{esc(r['espn_book'] or 'ESPN')} O{r['espn_line']} {fmt_odds(int(r['espn_over'])) if r['espn_over'] is not None else ''}"
                if r["espn_line"] else "–")
        man = "" if r["manual"] is None else r["manual"]
        api_v = "" if r["o15_api"] is None else r["o15_api"]
        iid = "m" + re.sub(r"[^a-z0-9]", "", norm(key))[:40]
        return f"""<article data-key="{esc(key)}" data-api="{api_v}" data-book="{esc(r['o15_api_book'] or '')}" data-manual="{man}">
  <header><span class=lg>{esc(r['league'])}</span><time>{fmt_time(r['kickoff'])}</time></header>
  <h3>{logo(r['home_logo'])}{esc(r['home'])} <i>v</i> {logo(r['away_logo'])}{esc(r['away'])}</h3>
  <dl>
   <div><dt>Goals/gm (home / away)</dt><dd>{r['avg'][0]} / {r['avg'][1]}</dd></div>
   <div><dt>Scored in last 5</dt><dd>{r['last5'][0]} / {r['last5'][1]}</dd></div>
   <div><dt>H2H avg total</dt><dd>{h2h}</dd></div>
   <div><dt>Over 1.5 best price</dt><dd><span class=best></span> <small class=src></small></dd></div>
   <div><dt>US books (API)</dt><dd>{api}</dd></div>
   <div><dt>ESPN main total</dt><dd>{espn}</dd></div>
  </dl>
  <div class=sb><label for="{iid}">SportsBetMT Over 1.5</label><input type=text id="{iid}" inputmode=numeric autocomplete=off placeholder="-625"><span class=hint></span></div>
</article>"""

    cands = [r for r in rows if r["c1"] and r["c3"] and r["c2"] is not False]
    n_inc = sum(1 for r in rows if r.get("incomplete"))
    incomplete_note = f" ({n_inc} could not be checked because ESPN data failed to load; re-run)" if n_inc else ""
    crit = f"avg goals ≥ {args.min_avg} · H2H avg ≥ {args.min_h2h} · scored in ≥4 of last 5 · Over 1.5 at {args.odds} or better"

    def sec(k, title, note):
        return (f"<section id=sec-{k}><h2>{title} <span class=n id=cnt-{k}>0</span></h2><p class=muted>{note}</p>"
                f"<p class=empty hidden>None right now.</p></section>")

    body = (sec("qual", "Qualifying", "Passes all four filters at the best available Over 1.5 price.")
            + sec("pend", "Needs Over 1.5 price", "Passes filters 1–3 but no book has a line yet. Enter the SportsBetMT price on a card to check it.")
            + sec("below", "Price too short", "Passes filters 1–3, but the best price is worse than the cutoff. A better SportsBetMT price moves it up."))
    cards = "\n".join(card(r) for r in cands)
    export = """<div class=export><button type=button id=export-btn>Export SportsBetMT prices</button>
<span class=muted id=export-msg></span><textarea id=export-out hidden readonly aria-label="manual_odds.json contents"></textarea></div>"""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Goal Match Finder</title><style>{CSS}</style></head><body><main>
<h1>Goal Match Finder</h1>
<p class=muted>Generated {time.strftime('%Y-%m-%d %H:%M')} · next {args.days} days · {crit}<br>
Scanned {len(rows)} fixtures{incomplete_note} · season goals and last 5 are competition-only · H2H is ESPN's last 5 meetings.</p>
{body}
<div hidden id=card-pool>{cards}</div>
{export}
</main><script>{JS.replace('__CUTOFF__', str(args.odds))}</script></body></html>"""


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--leagues", nargs="+", choices=LEAGUES, default=list(LEAGUES))
    p.add_argument("--days", type=int, default=3, help="look-ahead window (default 3)")
    p.add_argument("--season", type=int, help="override season start year")
    p.add_argument("--min-avg", type=float, default=1.5)
    p.add_argument("--min-h2h", type=float, default=1.5)
    p.add_argument("--odds", type=int, default=-650, help="American odds cutoff for Over 1.5, -650 or better (default -650)")
    p.add_argument("--allow-no-h2h", action="store_true", help="pass teams that have never met")
    p.add_argument("--out", default=str(Path(__file__).parent / "site" / "index.html"))
    args = p.parse_args(argv)
    rows = evaluate(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(rows, args))
    (out.parent / "matches.json").write_text(json.dumps(rows, indent=1))
    counts = {k: sum(1 for r in rows if bucket(r) == k) for k in ("qualifying", "pending_odds", "out")}
    print(f"{len(rows)} fixtures scanned: {counts['qualifying']} qualifying, {counts['pending_odds']} need an Over 1.5 price")
    n_inc = sum(1 for r in rows if r.get("incomplete"))
    if n_inc:
        print(f"warning: {n_inc} fixtures had ESPN data fail to load and were not fully checked; re-run to retry")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
