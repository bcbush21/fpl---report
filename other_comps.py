"""
Cup and European games for Premier League players, from API-Football (api-sports.io).

FPL only covers league games. This adds the Champions League, Europa League, Conference League,
FA Cup and League Cup so the report can see midweek minutes: who is being rotated, who starts the
cups because he's a backup, who is getting minutes back after an injury.

Needs a free API-Football key in the API_FOOTBALL_KEY secret. The free plan allows 100 requests a
day and about 10 a minute, so everything is cached in other_comps.json: finished games are fetched
once and never again, and a run stops after MAX_CALLS requests and picks up the rest tomorrow.
Without a key the report still builds; the tab explains how to switch it on.
"""
import datetime as dt
import json
import os
import re
import time
import unicodedata
from pathlib import Path

import requests

API = "https://v3.football.api-sports.io"
KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
CACHE = Path(os.environ.get("OC_CACHE") or Path(__file__).with_name("other_comps.json"))
PL_LEAGUE = 39
LOOKBACK_DAYS = 21          # how far back to look for cup and European games
MAX_CALLS = 45              # per run; leaves room under the 100-a-day free limit for a second run
CALL_GAP = 6.5              # seconds between calls, to stay under ~10 a minute
UPCOMING_TTL_H = 12         # re-check upcoming days' fixtures after this many hours
FINISHED = {"FT", "AET", "PEN", "AWD", "WO"}
NOT_PLAYED = {"PST", "CANC", "ABD"}

COMP_SHORT = {2: "UCL", 3: "UEL", 848: "UECL", 45: "FA Cup", 48: "League Cup", 528: "Shield", 531: "Super Cup"}
DOMESTIC_CUPS = {45, 48}
EUROPE = {2, 3, 848}

# FPL short club names -> the names API-Football uses
TEAM_ALIASES = {
    "man city": "manchester city", "man utd": "manchester united", "spurs": "tottenham",
    "nott'm forest": "nottingham forest", "wolves": "wolves", "sheffield utd": "sheffield utd",
    "west ham": "west ham", "newcastle": "newcastle", "brighton": "brighton", "leicester": "leicester",
}


class ApiError(Exception):
    pass


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", "and").replace("'", "").replace(".", " ").replace("-", " ")
    return re.sub(r"\s+", " ", re.sub(r"\b(fc|afc|cf)\b", "", s)).strip()


class Client:
    def __init__(self, key):
        self.key, self.calls, self.last = key, 0, 0.0
        self.s = requests.Session()
        self.s.headers.update({"x-apisports-key": key})

    def get(self, path, **params):
        if self.calls >= MAX_CALLS:
            raise ApiError("daily budget reached")
        wait = CALL_GAP - (time.time() - self.last)
        if wait > 0 and self.calls:
            time.sleep(wait)
        self.last = time.time()
        self.calls += 1
        r = self.s.get(f"{API}/{path}", params=params, timeout=40)
        if r.status_code == 429:
            raise ApiError("rate limited by API-Football")
        r.raise_for_status()
        j = r.json()
        errs = j.get("errors")
        if errs:
            msg = "; ".join(f"{v}" for v in (errs.values() if isinstance(errs, dict) else errs))
            raise ApiError(msg)
        return j.get("response", [])


def _load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(c):
    CACHE.write_text(json.dumps(c, separators=(",", ":"), sort_keys=True))


def map_teams(fpl_teams, api_teams):
    """{fpl team id: api team id}, matching club names."""
    api = {_norm(t["name"]): t["id"] for t in api_teams}
    out = {}
    for t in fpl_teams.values():
        n = _norm(TEAM_ALIASES.get(t["full"].lower(), t["full"]))
        if n in api:
            out[t["id"]] = api[n]
            continue
        # fall back to the closest name: shared words, preferring a match on the first word
        best, score = None, 0
        for an, aid in api.items():
            a, b = set(n.split()), set(an.split())
            sc = len(a & b) + (0.5 if n.split()[0] == an.split()[0] else 0)
            if sc > score:
                best, score = aid, sc
        if best and score >= 1:
            out[t["id"]] = best
    return out


def fetch(fpl_teams, season, today=None):
    """Bring the cache up to date. Returns (cache, status) where status says what happened."""
    cache = _load_cache()
    if not KEY:
        return cache, ("nokey" if not cache.get("fixtures") else "cached")
    today = today or dt.datetime.now(dt.timezone.utc).date()
    cl = Client(KEY)
    status = "ok"
    try:
        tk = f"teams_{season}"
        if not cache.get(tk):
            cache[tk] = [dict(id=x["team"]["id"], name=x["team"]["name"]) for x in cl.get("teams", league=PL_LEAGUE, season=season)]
            if not cache[tk]:
                raise ApiError(f"API-Football has no Premier League teams for the {season} season on this plan")
        tmap = map_teams(fpl_teams, cache[tk])
        cache["team_map"] = {str(k): v for k, v in tmap.items()}
        pl_ids = set(tmap.values())

        fx = cache.setdefault("fixtures", {})      # fixture id -> compact fixture
        days = cache.setdefault("days", {})         # date -> {"at": iso time fetched, "done": bool}
        now = dt.datetime.now(dt.timezone.utc)
        for off in range(-LOOKBACK_DAYS, 8):
            day = today + dt.timedelta(days=off)
            key = day.isoformat()
            info = days.get(key)
            if info:
                if info.get("done"):
                    continue
                age = (now - dt.datetime.fromisoformat(info["at"])).total_seconds() / 3600
                if off > 0 and age < UPCOMING_TTL_H:
                    continue
                if off <= 0 and age < 3:
                    continue
            rows = cl.get("fixtures", date=key, timezone="UTC")
            done = off < -1
            for r in rows:
                h, a = r["teams"]["home"]["id"], r["teams"]["away"]["id"]
                if h not in pl_ids and a not in pl_ids:
                    continue
                lg = r["league"]
                st = r["fixture"]["status"]["short"]
                if off <= -1 and st not in FINISHED | NOT_PLAYED:
                    done = False
                fx[str(r["fixture"]["id"])] = dict(
                    id=r["fixture"]["id"], t=r["fixture"]["date"], st=st, lg=lg["id"], lgn=lg["name"],
                    rd=lg.get("round") or "", h=h, a=a, hn=r["teams"]["home"]["name"], an=r["teams"]["away"]["name"],
                    gh=r["goals"]["home"], ga=r["goals"]["away"])
            days[key] = dict(at=now.isoformat(), done=done)

        # player stats for finished cup and European games (once each, never again)
        stats = cache.setdefault("stats", {})
        todo = [f for f in fx.values() if f["lg"] != PL_LEAGUE and f["st"] in FINISHED and str(f["id"]) not in stats]
        for f in sorted(todo, key=lambda f: f["t"], reverse=True):
            resp = cl.get("fixtures/players", fixture=f["id"])
            out = {}
            for side in resp:
                tid = side["team"]["id"]
                if tid not in pl_ids:
                    continue
                rows = []
                for pl in side.get("players", []):
                    st = (pl.get("statistics") or [{}])[0]
                    g = st.get("games") or {}
                    gl = st.get("goals") or {}
                    rows.append(dict(id=pl["player"]["id"], n=pl["player"]["name"], m=g.get("minutes") or 0,
                                     sub=bool(g.get("substitute")), r=g.get("rating"),
                                     g=gl.get("total") or 0, a=gl.get("assists") or 0))
                out[str(tid)] = rows
            stats[str(f["id"])] = out
    except ApiError as err:
        status = "budget" if "budget" in str(err) else f"error: {err}"
    except requests.RequestException as err:
        status = f"error: {err}"

    # keep the cache small: drop anything older than 60 days
    cut = (today - dt.timedelta(days=60)).isoformat()
    for k in [k for k, f in cache.get("fixtures", {}).items() if f["t"][:10] < cut]:
        cache["fixtures"].pop(k, None)
        cache.get("stats", {}).pop(k, None)
    for k in [k for k in cache.get("days", {}) if k < cut]:
        cache["days"].pop(k)
    cache["updated"] = dt.datetime.now(dt.timezone.utc).isoformat()
    cache["calls"] = cl.calls
    _save_cache(cache)
    print(f"Other competitions: {cl.calls} API-Football calls, status {status}")
    return cache, status


# ---------------------------------------------------------------- matching players

def _name_keys(el):
    first, second, web = _norm(el.get("first_name")), _norm(el.get("second_name")), _norm(el.get("web_name"))
    return first, second, web


def match_player(api_name, candidates):
    """Pick the FPL element (from one club) an API-Football name refers to, or None."""
    an = _norm(api_name)
    toks = an.split()
    if not toks:
        return None
    last = toks[-1]
    initial = toks[0][0] if len(toks) > 1 else ""
    best, score = None, 0
    for el in candidates:
        first, second, web = _name_keys(el)
        full = f"{first} {second}".strip()
        sc = 0
        if an == full or an == web:
            sc = 10
        elif last in second.split() or last == web or web in an.split():
            sc = 5 + (2 if initial and first.startswith(initial) else 0)
        elif an in full or (second and second in an):
            sc = 4
        if sc > score:
            best, score = el, sc
    return best if score >= 5 else None


# ---------------------------------------------------------------- signals

def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def comp_short(f):
    return COMP_SHORT.get(f["lg"], f["lgn"])


def build(cache, d, players, teams, now=None):
    """Per player: his cup/European games and flags. Per club: games played and coming up."""
    now = now or dt.datetime.now(dt.timezone.utc)
    tmap = {int(k): v for k, v in (cache.get("team_map") or {}).items()}
    inv = {v: k for k, v in tmap.items()}
    fx = cache.get("fixtures") or {}
    stats = cache.get("stats") or {}
    els = {e["id"]: e for e in d["boot"]["elements"]}
    by_team = {}
    for e in d["boot"]["elements"]:
        by_team.setdefault(e["team"], []).append(e)

    # each FPL player's cup and European games
    games = {}
    for fid, f in fx.items():
        if f["lg"] == PL_LEAGUE or fid not in stats:
            continue
        for api_tid, rows in stats[fid].items():
            tid = inv.get(int(api_tid))
            if not tid:
                continue
            opp = f["an"] if f["h"] == int(api_tid) else f["hn"]
            home = f["h"] == int(api_tid)
            for r in rows:
                el = match_player(r["n"], by_team.get(tid, []))
                if not el:
                    continue
                games.setdefault(el["id"], []).append(dict(
                    t=f["t"], comp=comp_short(f), lg=f["lg"], opp=opp, home=home, m=r["m"], sub=r["sub"],
                    g=r["g"], a=r["a"], r=r["r"], fid=f["id"]))

    # every club's games around the next league gameweek, all competitions
    next_pl = {}
    for fx_pl in d["fixtures"]:
        if fx_pl.get("event") != d["next"]:
            continue
        for side in (fx_pl["team_h"], fx_pl["team_a"]):
            t = _ts(fx_pl.get("kickoff_time"))
            if t and (side not in next_pl or t < next_pl[side]):
                next_pl[side] = t
    clubs = {}
    for t in teams.values():
        api = tmap.get(t["id"])
        mine_fx = [f for f in fx.values() if api and api in (f["h"], f["a"]) and f["lg"] != PL_LEAGUE
                   and f["st"] not in NOT_PLAYED]
        pl_ko = [_ts(r["kickoff"]) for r in t["results"] if r.get("kickoff")]
        recent = [x for x in pl_ko if x and now - dt.timedelta(days=14) <= x <= now]
        recent += [_ts(f["t"]) for f in mine_fx if _ts(f["t"]) and now - dt.timedelta(days=14) <= _ts(f["t"]) <= now]
        nxt = next_pl.get(t["id"])
        before = sorted([f for f in mine_fx if nxt and now - dt.timedelta(days=4) <= _ts(f["t"]) < nxt],
                        key=lambda f: f["t"])
        after = sorted([f for f in mine_fx if _ts(f["t"]) and _ts(f["t"]) > now and (not nxt or _ts(f["t"]) >= nxt)],
                       key=lambda f: f["t"])[:2]
        last_any = max([x for x in pl_ko if x and x <= now] + [_ts(f["t"]) for f in mine_fx
                       if _ts(f["t"]) and _ts(f["t"]) <= now], default=None)
        prev = before[-1] if before else None
        prev_t = _ts(prev["t"]) if prev else last_any
        rest = (nxt - prev_t).days if nxt and prev_t and prev_t < nxt else None
        clubs[t["id"]] = dict(api=bool(api), n14=len(recent), next_pl=nxt, before=before, after=after, rest=rest,
                              europe=any(f["lg"] in EUROPE for f in mine_fx))

    out = {}
    for p in players.values():
        gs = sorted(games.get(p["id"], []), key=lambda g: g["t"])
        c = clubs.get(p["tid"], {})
        flags = []
        a = p.get("as") or {}
        nailed = a.get("nailed")
        nxt = c.get("next_pl")
        for g in gs:
            gt = _ts(g["t"])
            if not gt:
                continue
            days_before = (nxt - gt).total_seconds() / 86400 if nxt else None
            wd = gt.strftime("%a")
            if days_before is not None and 0 < days_before <= 4.5 and g["m"] >= 75:
                flags.append((f"Played {g['m']}' in the {g['comp']} {wd}, {int(days_before)} days before GW{d['next']}: rotation risk", "warn"))
            elif g["lg"] in DOMESTIC_CUPS and now - gt <= dt.timedelta(days=LOOKBACK_DAYS):
                if nailed and (g["sub"] or g["m"] < 30):
                    flags.append((f"Rested for the {g['comp']} {gt:%b %-d}: treated as first choice", "good"))
                elif not nailed and not g["sub"] and g["m"] >= 60:
                    flags.append((f"Started the {g['comp']} {gt:%b %-d}: likely a backup in the league", "warn"))
        # nailed league starters left out of a domestic cup squad entirely also count as rested
        for f in fx.values():
            if f["lg"] in DOMESTIC_CUPS and f["st"] in FINISHED and tmap.get(p["tid"]) in (f["h"], f["a"]) \
                    and str(f["id"]) in stats and not any(g["fid"] == f["id"] for g in gs) and nailed:
                gt = _ts(f["t"])
                if gt and now - gt <= dt.timedelta(days=LOOKBACK_DAYS):
                    flags.append((f"Left out of the {comp_short(f)} squad {gt:%b %-d}: rested as a first choice", "good"))
        # coming back: flagged, or no league minutes lately, but playing elsewhere
        recent_g = [g for g in gs if _ts(g["t"]) and now - _ts(g["t"]) <= dt.timedelta(days=14) and g["m"] > 0]
        pl_last = (a.get("rows") or [{}])[-1].get("mins", 0) if a.get("rows") else 0
        if recent_g and (p["status"] != "a" or pl_last == 0):
            g = recent_g[-1]
            flags.append((f"Got {g['m']}' in the {g['comp']} {_ts(g['t']):%b %-d}" +
                          (": back up to speed" if g["m"] >= 60 else ": minutes building"), "good"))
        # upcoming midweek before the next league game
        for f in c.get("before", []):
            ft = _ts(f["t"])
            if ft and ft > now and nxt:
                dd = (nxt - ft).total_seconds() / 86400
                if dd <= 4.5:
                    flags.append((f"{comp_short(f)} {ft:%a} then GW{d['next']} {nxt:%a}: {int(dd)} days apart", "info"))
                    break
        seen, uniq = set(), []
        for txt, k in flags:
            if txt not in seen:
                seen.add(txt)
                uniq.append((txt, k))
        last14 = [g for g in gs if _ts(g["t"]) and now - _ts(g["t"]) <= dt.timedelta(days=14)]
        out[p["id"]] = dict(games=gs[-6:], flags=uniq[:3],
                            n14=len(last14), s14=sum(1 for g in last14 if not g["sub"] and g["m"] > 0),
                            m14=sum(g["m"] for g in last14), club=c)
    return out, clubs
