#!/usr/bin/env python3
"""
FPL Draft report for Boofla (Hope Bros league), v2.

Focus:
  1. Fixture outlook for the next 5 GWs, split by attack and defense, with the reason
  2. Finishing: xG / xA vs actual goals / assists (who is due, who is running hot)
  3. Defensive contribution: which defenders (and mids) bank reliable points
  4. Projected points from those pieces, used for lineup, waiver and trade advice

Data: unofficial FPL Draft + FPL APIs. If the Premier League changes them,
the fix is almost always in get() or load_data().
"""
import datetime
import html
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ENTRY_ID = 275109  # your team ID
HORIZON = 5        # how many upcoming gameweeks to look at

DRAFT = "https://draft.premierleague.com/api"
CLASSIC = "https://fantasy.premierleague.com/api"
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
STATUS = {"a": "Available", "d": "Doubtful", "i": "Injured",
          "s": "Suspended", "u": "Unavailable", "n": "Not in squad"}

# FPL scoring
GOAL_PTS = {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_PTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
DC_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}  # actions needed for +2
HOME_BOOST = 1.12  # home teams create ~12% more xG on average

# ---------------------------------------------------------------- your settings
# Players the report should never tell you to drop or trade, whatever the numbers say.
# Use the name exactly as FPL shows it, e.g. "Palmer", "Cunha". Edit on GitHub any time.
KEEP = []

# How much of a player's over/underperformance vs xG counts as real finishing skill.
# 0 = pure xG (ignores finishing), 1 = pure actual returns. 0.3 is a sensible middle.
FINISHING_SKILL = 0.3

# Core = top this share of players at their position by underlying output,
# or one of your top CORE_TOP_SCORERS by total points. Core players are never drop/sell candidates.
CORE_PERCENTILE = 0.25
CORE_TOP_SCORERS = 4

# ---------------------------------------------------------------- data

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "Mozilla/5.0"


def get(url):
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def load_data():
    boot = get(f"{DRAFT}/bootstrap-static")
    entry = get(f"{DRAFT}/entry/{ENTRY_ID}/public")["entry"]
    league_id = entry["league_set"][0]

    events = boot["events"]
    current = events.get("current") if isinstance(events, dict) else None
    if current is None:
        current = get(f"{DRAFT}/game")["current_event"]
    nxt = (events.get("next") if isinstance(events, dict) else None) or current + 1

    d = dict(
        boot=boot, entry=entry, league_id=league_id, current=current, next=nxt,
        picks=get(f"{DRAFT}/entry/{ENTRY_ID}/event/{current}")["picks"],
        status=get(f"{DRAFT}/league/{league_id}/element-status")["element_status"],
        details=get(f"{DRAFT}/league/{league_id}/details"),
        fixtures=get(f"{CLASSIC}/fixtures/"),  # team ids match the Draft game
    )
    # the main FPL game publishes each club's set-piece order (penalties, direct free kicks, corners);
    # player codes are shared between the two games, so we can join on them
    try:
        d["classic_boot"] = get(f"{CLASSIC}/bootstrap-static/")
    except requests.RequestException:
        d["classic_boot"] = None

    # Per-match history for the players we care about: my squad plus every
    # DEF/MID with real minutes (needed for defensive contribution hit rates).
    # live ownership is the source of truth, so waiver pickups show up before the next deadline
    owned = [s["element"] for s in d["status"] if s.get("owner") == ENTRY_ID]
    if owned:
        kept = [p["element"] for p in sorted(d["picks"], key=lambda x: x["position"]) if p["element"] in owned]
        order = kept + [e for e in owned if e not in kept]
        d["picks"] = [{"element": e, "position": i + 1} for i, e in enumerate(order)]
    mine = {p["element"] for p in d["picks"]}
    my_slots = {(e["team"], e["element_type"]) for e in boot["elements"] if e["id"] in mine}
    wanted = [e["id"] for e in boot["elements"]
              if e["id"] in mine or (e["team"], e["element_type"]) in my_slots
              or (e["element_type"] in (2, 3) and e.get("minutes", 0) >= 180)]

    def hist(pid):
        try:
            return pid, get(f"{DRAFT}/element-summary/{pid}").get("history", [])
        except requests.RequestException:
            return pid, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        d["history"] = dict(pool.map(hist, wanted))

    # every manager's points by gameweek, for the season charts
    def entry_hist(e):
        try:
            rows = get(f"{DRAFT}/entry/{e['entry_id']}/history").get("history", [])
            return e["entry_id"], {r["event"]: r.get("points", 0) for r in rows}
        except requests.RequestException:
            return e["entry_id"], {}

    entries = d["details"].get("league_entries", [])
    with ThreadPoolExecutor(max_workers=8) as pool:
        d["league_hist"] = dict(pool.map(entry_hist, entries))

    # every manager's counted lineup and every player's points, for each gameweek played so far
    gws = list(range(1, current + 1))

    def lineup(job):
        eid, gw = job
        try:
            r = get(f"{DRAFT}/entry/{eid}/event/{gw}")
        except requests.RequestException:
            return job, None
        xi = {p["element"] for p in r.get("picks", []) if p.get("position", 99) <= 11}
        for sub in r.get("subs", []) or []:  # automatic subs: make sure only counted players remain
            xi.discard(sub.get("element_out"))
            if sub.get("element_in"):
                xi.add(sub["element_in"])
        return job, xi

    def live(gw):
        try:
            els = get(f"{DRAFT}/event/{gw}/live").get("elements", {})
            return gw, {int(k): (v.get("stats", {}).get("total_points", 0), v.get("stats", {}).get("minutes", 0),
                                 v.get("stats", {}).get("bps", 0)) for k, v in els.items()}
        except requests.RequestException:
            return gw, {}

    jobs = [(e["entry_id"], gw) for e in entries if e.get("entry_id") for gw in gws]
    with ThreadPoolExecutor(max_workers=8) as pool:
        d["lineups"] = {k: v for k, v in pool.map(lineup, jobs) if v is not None}
        raw = dict(pool.map(live, gws))
    d["live"] = {gw: {k: v[0] for k, v in m.items()} for gw, m in raw.items()}
    d["live_min"] = {gw: {k: v[1] for k, v in m.items()} for gw, m in raw.items()}
    d["live_bps"] = {gw: {k: v[2] for k, v in m.items()} for gw, m in raw.items()}

    # full per-player match stats for the latest gameweek, tagged with the fixture(s) each player played in
    try:
        els = get(f"{DRAFT}/event/{current}/live").get("elements", {})
        d["recap_live"] = {int(k): (v.get("stats", {}), [x[1] for x in v.get("explain", []) if len(x) > 1])
                           for k, v in els.items()}
    except requests.RequestException:
        d["recap_live"] = {}
    return d

# ---------------------------------------------------------------- helpers

def has_ended(f):
    """A match counts once it has ended. FPL's 'finished' flag waits for bonus points to be confirmed,
    often a day later, so 'finished_provisional' is the earlier signal that the game is over."""
    return bool(f.get("finished") or f.get("finished_provisional"))


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def per90(x, mins):
    return x / mins * 90 if mins >= 90 else 0.0


def esc(x):
    return html.escape(str(x))

# ---------------------------------------------------------------- teams

def build_teams(d):
    teams = {t["id"]: {"id": t["id"], "name": t.get("short_name", t["name"]),
                       "full": t["name"], "code": t.get("code", t["id"]),
                       "gp": 0, "gf": 0, "ga": 0, "xg": 0.0, "xgc90": None, "results": []}
             for t in d["boot"]["teams"]}

    done = [f for f in d["fixtures"] if has_ended(f) and f.get("team_h_score") is not None]
    for f in sorted(done, key=lambda f: f.get("kickoff_time") or ""):
        h, a = teams[f["team_h"]], teams[f["team_a"]]
        hs, as_ = f["team_h_score"], f["team_a_score"]
        h["gp"] += 1; a["gp"] += 1
        h["gf"] += hs; h["ga"] += as_
        a["gf"] += as_; a["ga"] += hs
        ko = f.get("kickoff_time") or ""
        h["results"].append(dict(gf=hs, ga=as_, opp=f["team_a"], home=True, kickoff=ko, gw=f.get("event")))
        a["results"].append(dict(gf=as_, ga=hs, opp=f["team_h"], home=False, kickoff=ko, gw=f.get("event")))

    # team xG = sum of its players' xG; team xGC/90 = its busiest keeper's xGC/90
    keeper_mins = {}
    for el in d["boot"]["elements"]:
        t = teams.get(el["team"])
        if not t:
            continue
        t["xg"] += num(el.get("expected_goals"))
        if el["element_type"] == 1 and el.get("minutes", 0) > keeper_mins.get(el["team"], 0):
            keeper_mins[el["team"]] = el["minutes"]
            t["xgc90"] = per90(num(el.get("expected_goals_conceded")), el["minutes"])

    for t in teams.values():
        gp = max(t["gp"], 1)
        t["xg_pg"] = t["xg"] / gp if t["gp"] else 1.3
        t["gf_pg"], t["ga_pg"] = t["gf"] / gp, t["ga"] / gp
        if not t["xgc90"]:
            t["xgc90"] = t["ga_pg"] or 1.3

    # strength ratings: 60% season (goals and xG blended), 40% last 5 matches
    for t in teams.values():
        last = t["results"][-5:]
        t["n5"] = len(last)
        t["gf5"] = sum(r["gf"] for r in last)
        t["ga5"] = sum(r["ga"] for r in last)
        t["form"] = ["W" if r["gf"] > r["ga"] else "D" if r["gf"] == r["ga"] else "L" for r in last]
        t["gd"] = t["gf"] - t["ga"]
        t["cs"] = sum(1 for r in t["results"] if r["ga"] == 0)
        t["w"] = sum(1 for r in t["results"] if r["gf"] > r["ga"])
        t["dr"] = sum(1 for r in t["results"] if r["gf"] == r["ga"])
        t["l"] = sum(1 for r in t["results"] if r["gf"] < r["ga"])
        t["pts"] = 3 * t["w"] + t["dr"]
        t["cs5"] = sum(1 for r in last if r["ga"] == 0)
        season_att = (t["xg_pg"] + t["gf_pg"]) / 2
        season_def = (t["xgc90"] + t["ga_pg"]) / 2
        if t["n5"]:
            t["att_rating"] = 0.6 * season_att + 0.4 * t["gf5"] / t["n5"]
            t["def_rating"] = 0.6 * season_def + 0.4 * t["ga5"] / t["n5"]
        else:
            t["att_rating"], t["def_rating"] = season_att, season_def

    # league position, same order as the table: points, goal difference, goals scored, name
    for i, t in enumerate(sorted(teams.values(), key=lambda t: (-t["pts"], -t["gd"], -t["gf"], t["full"])), 1):
        t["table_pos"] = i
    t_count = len(teams)
    for t in teams.values():
        t["table_n"] = t_count

    played = [t for t in teams.values() if t["gp"]]
    lg_xg = sum(t["xg_pg"] for t in played) / len(played) if played else 1.3
    lg_xgc = sum(t["xgc90"] for t in played) / len(played) if played else 1.3

    # next HORIZON gameweeks of fixtures for each team
    last_gw = d["next"] + HORIZON - 1
    for t in teams.values():
        t["run"] = []
    for f in sorted(d["fixtures"], key=lambda f: (f.get("event") or 99, f.get("kickoff_time") or "")):
        ev = f.get("event")
        if not ev or not (d["next"] <= ev <= last_gw) or has_ended(f):
            continue
        for side, opp, home in ((f["team_h"], f["team_a"], True), (f["team_a"], f["team_h"], False)):
            o = teams[opp]
            mult = HOME_BOOST if home else 1 / HOME_BOOST
            teams[side]["run"].append(dict(
                gw=ev, opp=o["name"], opp_id=opp, home=home,
                fdr=f["team_h_difficulty"] if home else f["team_a_difficulty"],
                # attack: how leaky is the opponent (vs league average), adjusted for venue
                att=(o["xgc90"] / lg_xgc) * mult,
                # defense: how dangerous is the opponent, adjusted for venue
                dfn=(o["xg_pg"] / lg_xg) / mult,
                o_xg=o["xg_pg"], o_xgc=o["xgc90"], o_gf=o["gf_pg"], o_ga=o["ga_pg"],
            ))

    for t in teams.values():
        r = t["run"]
        n = len(r)
        # ease scores: 1.0 = average run, >1 = easier
        t["att_ease"] = sum(x["att"] for x in r) / n if n else 0
        t["def_ease"] = sum(1 / x["dfn"] for x in r) / n if n else 0
        t["n_fix"] = n
    return teams, lg_xg, lg_xgc


def run_str(t, side):
    parts = []
    for x in t["run"]:
        ha = "H" if x["home"] else "A"
        if side == "att":
            parts.append(f"{x['opp']}({ha}) concede {x['o_xgc']:.1f} xG/g")
        else:
            parts.append(f"{x['opp']}({ha}) create {x['o_xg']:.1f} xG/g")
    return " · ".join(parts) or "No fixtures"


def ease_label(v):
    if v == 0:
        return "Blank"
    if v >= 1.15:
        return "Easy"
    if v >= 1.03:
        return "Good"
    if v > 0.92:
        return "Average"
    if v > 0.82:
        return "Tough"
    return "Very tough"

# ---------------------------------------------------------------- players

SET_PIECES = (("pen", "penalties_order"), ("fk", "direct_freekicks_order"),
              ("ck", "corners_and_indirect_freekicks_order"))


def set_piece_orders(d):
    """{code: {"pen": order, ...}} and {(team, kind): [(order, name), ...]} from FPL's published order.
    Falls back to the Draft feed's own fields if the main game is unreachable."""
    src = (d.get("classic_boot") or {}).get("elements") or d["boot"]["elements"]
    by_code, by_team = {}, {}
    for e in src:
        for kind, field in SET_PIECES:
            o = e.get(field)
            if o:
                by_code.setdefault(e.get("code"), {})[kind] = o
                by_team.setdefault((e["team"], kind), []).append((o, e["web_name"]))
    for v in by_team.values():
        v.sort()
    return by_code, by_team


def set_piece_info(p, by_code, by_team):
    """Per kind: [yes(1/0, None if unknown), note]. 'Yes' only when he's first choice."""
    out = {}
    mine = by_code.get(p.get("code"), {})
    for kind, _ in SET_PIECES:
        takers = by_team.get((p["tid"], kind), [])
        o = mine.get(kind)
        if not takers:
            out[kind] = [None, "No order published"]
        elif o == 1:
            others = [n for oo, n in takers if oo == 1 and n != p["name"]]
            out[kind] = [1, "Shares it with " + ", ".join(others) if others else "First choice"]
        else:
            ahead = [n for oo, n in takers if o is None or oo < o]
            if o is None:
                ahead = [n for oo, n in takers if oo == 1] or ahead[:1]
                out[kind] = [0, "Taker: " + ", ".join(ahead)]
            else:
                out[kind] = [0, f"#{o}, behind " + ", ".join(ahead)]
    return out


OUT_STATUSES = ("i", "s", "u", "n")  # injured, suspended, unavailable, not in squad


def minutes_security(p, team, live_min):
    """Minutes played as a share of the minutes he was available for.
    Games after he was flagged injured/suspended don't count against him. FPL doesn't keep a
    history of past flags, so an injury he has already recovered from still counts as missed games."""
    flagged = parse_ts(p.get("news_added")) if p["status"] in OUT_STATUSES else None
    games = {}
    for r in team["results"]:
        if not r.get("gw"):
            continue
        ko = parse_ts(r["kickoff"])
        if flagged and ko and ko > flagged:
            continue
        games[r["gw"]] = games.get(r["gw"], 0) + 1
    if not games:
        return None
    mins = {gw: min(live_min.get(gw, {}).get(p["id"], 0), 90 * n) for gw, n in games.items()}
    n_games = sum(games.values())
    played = sum(mins.values())
    pct = played / (90 * n_games)
    recent = sorted(games)[-3:]
    r_pct = sum(mins[g] for g in recent) / (90 * sum(games[g] for g in recent))
    label = ("Nailed" if pct >= 0.85 and r_pct >= 0.75 else "Secure" if pct >= 0.7 and r_pct >= 0.6
             else "Rotation risk" if pct >= 0.4 or r_pct >= 0.5 else "Fringe")
    return dict(pct=round(pct, 3), r3=round(r_pct, 3), mins=played, poss=90 * n_games, games=n_games,
                starts=min(p["starts"], n_games), out=len([1 for r in team["results"] if r.get("gw")]) - n_games,
                label=label)


def build_players(d, teams):
    names = {}
    for e in d["details"].get("league_entries", []):
        label = e.get("entry_name") or e.get("player_first_name", "?")
        names[e.get("entry_id")] = label
        names[e.get("id")] = label
    owner = {s["element"]: names.get(s.get("owner")) for s in d["status"] if s.get("owner")}
    my_name = d["entry"]["name"]
    mine = {p["element"] for p in d["picks"]}

    fx_by_id = {f["id"]: f for f in d["fixtures"]}
    players = {}
    for el in d["boot"]["elements"]:
        pos = POS[el["element_type"]]
        t = teams.get(el["team"])
        if not t:
            continue
        mins = el.get("minutes", 0)
        gp = max(t["gp"], 1)
        hist = d["history"].get(el["id"])

        xg, xa = num(el.get("expected_goals")), num(el.get("expected_assists"))
        g, a = el.get("goals_scored", 0), el.get("assists", 0)
        dc_total = el.get("defensive_contribution", 0) or 0

        p = dict(
            id=el["id"], name=el["web_name"], pos=pos, team=t["name"], tid=el["team"],
            draft_rank=el.get("draft_rank") or 999, news_added=el.get("news_added"),
            news_return=el.get("news_return"),
            code=el.get("code"),
            status=el.get("status", "a"), news=el.get("news", ""),
            chance=el.get("chance_of_playing_next_round"),
            total=el.get("total_points", 0), form=num(el.get("form")),
            mins=mins, starts=el.get("starts", 0), gp=t["gp"],
            start_rate=min(el.get("starts", 0) / gp, 1.0),
            avg_mins=mins / max(el.get("starts", 0), 1) if el.get("starts") else 0,
            g=g, a=a, xg=xg, xa=xa, xgi=xg + xa,
            g_diff=g - xg, a_diff=a - xa, gi_diff=(g + a) - (xg + xa),
            # scoring rates credit part of real finishing, so clinical players aren't underrated
            xg90=per90(max(xg + FINISHING_SKILL * (g - xg), 0), mins),
            xa90=per90(max(xa + FINISHING_SKILL * (a - xa), 0), mins),
            raw_xgi90=per90(xg + xa, mins),
            dc_total=dc_total, dc90=per90(dc_total, mins),
            cs=el.get("clean_sheets", 0), bonus90=per90(el.get("bonus", 0), mins),
            saves90=per90(el.get("saves", 0), mins),
            owner=owner.get(el["id"]), mine=el["id"] in mine,
        )
        p["owned_by_other"] = bool(p["owner"]) and not p["mine"] and p["owner"] != my_name

        # how often they actually hit the defensive contribution bonus, and play 90
        thr = DC_THRESHOLD.get(pos)
        apps = [h for h in (hist or []) if h.get("minutes", 0) >= 45]
        if apps:
            p["full90_rate"] = sum(h["minutes"] >= 89 for h in apps) / len(apps)
            p["dc_hit"] = (sum(h.get("defensive_contribution", 0) >= thr for h in apps) / len(apps)
                           if thr else 0.0)
            p["dc_source"] = "matches"
        else:
            p["full90_rate"] = p["avg_mins"] / 90 if p["avg_mins"] else 0
            # rough estimate from the season rate when we have no match log
            p["dc_hit"] = max(0.0, min(1.0, (p["dc90"] - 0.6 * thr) / (0.8 * thr))) if thr and p["dc90"] else 0.0
            p["dc_source"] = "estimate"
        p["apps"] = len(apps)
        p["ppg"] = p["total"] / max(t["gp"], 1)
        # game-by-game log for the squad review
        log = []
        for h in hist or []:
            fx = fx_by_id.get(h.get("fixture"))
            if not fx or not has_ended(fx):
                continue
            home = fx["team_h"] == el["team"]
            log.append(dict(gw=fx.get("event"), mins=h.get("minutes", 0), pts=h.get("total_points", 0),
                            g=h.get("goals_scored", 0), a=h.get("assists", 0), cs=h.get("clean_sheets", 0),
                            bonus=h.get("bonus", 0), dc=h.get("defensive_contribution", 0), bps=h.get("bps", 0),
                            opp=h.get("opponent_team") or (fx["team_a"] if home else fx["team_h"]), home=home,
                            kickoff=fx.get("kickoff_time") or ""))
        p["log"] = sorted(log, key=lambda x: (x["gw"] or 0, x["kickoff"]))
        players[el["id"]] = p
    return players


def rank_and_tag(players, squad_ids):
    """Rank everyone within their position by actual points per game, then tag Core players."""
    for pos in POS.values():
        group = sorted((p for p in players.values() if p["pos"] == pos and p["mins"] >= 270),
                       key=lambda p: p["ppg"], reverse=True)
        n = len(group) or 1
        for i, p in enumerate(group, 1):
            p["pos_rank"], p["pos_pct"] = i, i / n
    keep = {k.lower() for k in KEEP}
    mine = [players[i] for i in squad_ids]
    top_scorers = {p["id"] for p in sorted(mine, key=lambda p: p["total"], reverse=True)[:CORE_TOP_SCORERS]}
    for p in players.values():
        p.setdefault("pos_rank", None)
        p.setdefault("pos_pct", 1.0)
        p["elite"] = p["pos_pct"] <= CORE_PERCENTILE
        p["core"] = p["mine"] and (p["elite"] or p["id"] in top_scorers or p["name"].lower() in keep)
        p["kept"] = p["name"].lower() in keep


def rank_str(p):
    return f"{p['pos']} #{p['pos_rank']}" if p.get("pos_rank") else "-"


def finishing_verdict(p):
    if p["xgi"] < 0.8 and abs(p["gi_diff"]) < 1:
        return "Low volume"
    d = p["gi_diff"]
    if d <= -1.5:
        return "Due: underperforming"
    if d <= -0.75:
        return "Slightly unlucky"
    if d >= 1.5:
        # an elite player scoring above xG is still elite after regression
        return "Elite and clinical" if p.get("elite") else "Running hot: expect cooling"
    if d >= 0.75:
        return "Slightly clinical"
    return "In line"


# ---------------------------------------------------------------- season charts

def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def season_data(d):
    me = ENTRY_ID
    hist = d.get("league_hist", {})
    mine = hist.get(me, {})
    gws = sorted(g for g in mine if g <= d["current"])
    n_teams = len(hist) or 1
    rows, totals = [], {e: 0 for e in hist}
    for gw in gws:
        for e, h in hist.items():
            totals[e] += h.get(gw, 0)
        place = 1 + sum(1 for e, t in totals.items() if e != me and t > totals[me])
        week = [h.get(gw, 0) for h in hist.values()]
        week_rank = 1 + sum(1 for e, h in hist.items() if e != me and h.get(gw, 0) > mine.get(gw, 0))
        rows.append(dict(gw=gw, pts=mine.get(gw, 0), avg=sum(week) / len(week), place=place,
                         total=totals[me], week_rank=week_rank))
    return rows, n_teams


def svg_line(xlabels, series, y_min, y_max, y_ticks, y_fmt, invert=False, height=260, tips=None):
    """Small static line chart. series = [(name, values, color, dashed)]. Hover via native tooltips."""
    W, H = 640, height
    L, R, T, B = 54, 24, 18, 48
    pw, ph = W - L - R, H - T - B
    n = len(xlabels)

    def X(i):
        return L + (pw * (i / (n - 1)) if n > 1 else pw / 2)

    def Y(v):
        f = (v - y_min) / ((y_max - y_min) or 1)
        return T + (f * ph if invert else (1 - f) * ph)

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img">']
    for t in y_ticks:
        y = Y(t)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L - 10}" y="{y + 4:.1f}" class="ytick">{y_fmt(t)}</text>')
    every = max(1, math.ceil(n / 10))  # keep labels readable as the season grows
    for i, lab in enumerate(xlabels):
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{X(i):.1f}" y="{H - 10}" class="xtick">{lab}</text>')
    for name, vals, color, dashed in series:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.5" '
                   f'stroke-linejoin="round" stroke-linecap="round"{dash}/>')
        if not dashed:
            for i, v in enumerate(vals):
                tip = esc(tips[i]) if tips else f"{xlabels[i]}: {v}"
                out.append(f'<g class="pt"><title>{tip}</title>'
                           f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="14" fill="transparent"/>'
                           f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="5" fill="{color}" stroke="#fff" stroke-width="2"/></g>')
    out.append("</svg>")
    return "".join(out)


TEAM_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]


def league_lines(d):
    """Every Hope Bros team's points per gameweek, one colored line each, plus a table view."""
    hist = d.get("league_hist", {})
    entries = [e for e in d["details"].get("league_entries", []) if e.get("entry_id") in hist]
    if not entries:
        return ""
    # you are always blue; everyone else keeps a fixed color in league order
    entries.sort(key=lambda e: e["entry_id"] != ENTRY_ID)
    teams = [dict(k=f"t{i}", id=e["entry_id"], name=e.get("entry_name") or "?",
                  color=TEAM_COLORS[i % len(TEAM_COLORS)], me=e["entry_id"] == ENTRY_ID)
             for i, e in enumerate(entries)]
    gws = sorted({g for h in hist.values() for g in h if g <= d["current"]})
    if len(gws) < 2:
        return ""
    for t in teams:
        t["pts"] = [hist[t["id"]].get(g, 0) for g in gws]
        t["total"] = sum(t["pts"])

    W, H = 1000, 380
    L, R, T, B = 50, 190, 20, 44
    pw, ph = W - L - R, H - T - B
    top = max(max(t["pts"]) for t in teams)
    y_max = 10 * (int(top // 10) + 1)
    n = len(gws)
    X = lambda i: L + pw * i / (n - 1)
    Y = lambda v: T + (1 - v / y_max) * ph

    out = [f'<svg viewBox="0 0 {W} {H}" class="chart big" id="lgc" role="img">']
    for v in range(0, y_max + 1, 10):
        out.append(f'<line x1="{L}" x2="{L + pw}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" class="grid"/>'
                   f'<text x="{L - 10}" y="{Y(v) + 4:.1f}" class="ytick">{v}</text>')
    every = max(1, -(-n // 12))
    for i, g in enumerate(gws):
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{X(i):.1f}" y="{H - 14}" class="xtick">GW{g}</text>')
    out.append(f'<text x="{L + pw / 2:.0f}" y="{H}" class="xtick axt">Gameweek</text>')

    # draw everyone else first so your line sits on top
    for t in sorted(teams, key=lambda t: t["me"]):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(t["pts"]))
        w = 4 if t["me"] else 2.25
        out.append(f'<g data-k="{t["k"]}" class="series"><polyline points="{pts}" fill="none" stroke="{t["color"]}" '
                   f'stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"/>')
        for i, v in enumerate(t["pts"]):
            out.append(f'<g class="pt"><title>{esc(t["name"])} · GW{gws[i]}: {v} pts</title>'
                       f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="12" fill="transparent"/>'
                       f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="{5 if t["me"] else 4}" fill="{t["color"]}" '
                       f'stroke="#fff" stroke-width="2"/></g>')
        out.append("</g>")

    # name labels at the end of each line, nudged apart so they never overlap
    ends = sorted(teams, key=lambda t: Y(t["pts"][-1]))
    gap, prev = 17, -1e9
    for t in ends:
        y = max(Y(t["pts"][-1]), prev + gap)
        t["ly"], prev = y, y
    overflow = prev - (T + ph)
    if overflow > 0:
        for t in ends:
            t["ly"] -= overflow
    for t in teams:
        name = t["name"] if len(t["name"]) <= 20 else t["name"][:19] + "…"
        out.append(f'<g data-k="{t["k"]}" class="endlab{" me" if t["me"] else ""}">'
                   f'<circle cx="{L + pw + 16}" cy="{t["ly"]:.1f}" r="5" fill="{t["color"]}"/>'
                   f'<text x="{L + pw + 27}" y="{t["ly"] + 4:.1f}" fill="#0f1b2d">{esc(name)} · {t["pts"][-1]}</text></g>')
    out.append("</svg>")

    legend = "".join(f'<button class="lg-btn" data-k="{t["k"]}"><i class="sw" style="background:{t["color"]}"></i>'
                     f'{esc(t["name"])}{" (you)" if t["me"] else ""}</button>' for t in teams)

    # table view: weekly winner in bold
    best = {i: max(t["pts"][i] for t in teams) for i in range(n)}
    head = "".join(f"<th>GW{g}</th>" for g in gws)
    body = ""
    for t in sorted(teams, key=lambda t: t["total"], reverse=True):
        cells = "".join(f'<td class="{"wk" if v == best[i] else ""}">{v}</td>' for i, v in enumerate(t["pts"]))
        body += (f'<tr class="{"mine" if t["me"] else ""}"><td><span class="lgn"><i class="sw" '
                 f'style="background:{t["color"]}"></i>{esc(t["name"])}</span></td>{cells}<td><b>{t["total"]}</b></td></tr>')

    return f"""<div class="card"><h3>Hope Bros: points by team, every gameweek</h3>
<p class="note">One line per team. Tap a name to spotlight that team; tap it again to show everyone. Hover or tap a point for the exact score. On a phone, swipe the chart sideways.</p>
<div class="lg-row">{legend}</div>
<div class="chartwrap">{"".join(out)}</div>
<h4>Table view</h4><p class="note" style="margin-top:-4px">Bold = highest score in the league that week.</p>
<div class="tw"><table class="lgt"><thead><tr><th>Team</th>{head}<th>Total</th></tr></thead><tbody>{body}</tbody></table></div>
</div>
<script>
(function(){{var c=document.getElementById('lgc');if(!c)return;var cur='';
document.querySelectorAll('.lg-btn').forEach(function(b){{b.addEventListener('click',function(){{
cur=(cur===b.dataset.k)?'':b.dataset.k;
document.querySelectorAll('.lg-btn').forEach(function(x){{x.classList.toggle('on',x.dataset.k===cur);}});
c.querySelectorAll('[data-k]').forEach(function(el){{el.classList.toggle('dim',cur!==''&&el.dataset.k!==cur);}});
}});}});}})();
</script>"""


# ---------------------------------------------------------------- league table and matchups

ATTACK_FLAG = 2.0   # projected goals at or above this = "should score"
CS_FLAG = 0.50      # clean sheet chance at or above this = "clean sheet spot"


def form_chips(t):
    return "".join(f'<span class="fm {r}">{r}</span>' for r in t["form"]) or '<span class="mut">-</span>'


def played_note(t, max_gp):
    behind = max_gp - t["gp"]
    if behind > 0:
        return f'<span class="sub2 gih">{behind} game{"s" if behind > 1 else ""} in hand</span>'
    return ""


def team_table_section(teams):
    rows = [t for t in teams.values()]
    max_gp = max((t["gp"] for t in rows), default=0)
    body = []
    for t in rows:
        gd_cls = "pos" if t["gd"] > 0 else "neg" if t["gd"] < 0 else "mut"
        body.append(
            f'<tr data-pts="{t["pts"]}" data-gd="{t["gd"]}" data-gf="{t["gf"]}" data-ga="{t["ga"]}" data-cs="{t["cs"]}" '
            f'data-name="{esc(t["full"])}">'
            f'<td class="rk"></td><td>{team_cell(t)}</td>'
            f'<td><span class="bignum">{t["gp"]}</span>{played_note(t, max_gp)}</td>'
            f'<td><span class="bignum">{t["pts"]}</span><span class="sub2">{t["pts"] / max(t["gp"], 1):.2f} per game</span></td>'
            f'<td class="wdl">{t["w"]}<i>-</i>{t["dr"]}<i>-</i>{t["l"]}</td>'
            f'<td><span class="bignum">{t["gf"]}</span><span class="sub2">xG {t["xg_pg"]:.1f}/g</span></td>'
            f'<td><span class="bignum">{t["ga"]}</span><span class="sub2">xGA {t["xgc90"]:.1f}/g</span></td>'
            f'<td><span class="bignum {gd_cls}">{t["gd"]:+d}</span></td>'
            f'<td><span class="bignum">{t["cs"]}</span><span class="sub2">{t["cs"] / max(t["gp"], 1):.0%} of games · '
            f'{t["cs5"]} in last {t["n5"]}</span></td>'
            f'<td><div class="fms">{form_chips(t)}</div>'
            f'<span class="sub2">{t["gf5"]} scored, {t["ga5"]} conceded</span></td></tr>')
    return f"""<div class="card"><h3>Premier League table</h3>
<p class="note">Tap a button to re-rank. Teams with a game in hand are marked under Played, so compare points per game
as well as the total. Small grey numbers under goals are expected goals per game. Last 5 reads oldest to newest.</p>
<div class="sortbar"><span class="note" style="margin:0">Rank by:</span>
<button class="sb on" data-k="pts" data-dir="desc">League table (points)</button>
<button class="sb" data-k="gd" data-dir="desc">Goal difference</button>
<button class="sb" data-k="gf" data-dir="desc">Most goals scored</button>
<button class="sb" data-k="ga" data-dir="asc">Fewest goals conceded</button>
<button class="sb" data-k="ga" data-dir="desc">Most goals conceded</button>
<button class="sb" data-k="cs" data-dir="desc">Most clean sheets</button></div>
<div class="tw"><table class="tt" id="tt"><thead><tr><th>#</th><th>Team</th><th>Played</th><th>Points</th><th>W-D-L</th>
<th>Scored</th><th>Conceded</th>
<th>GD</th><th>Clean sheets</th><th>Last 5</th></tr></thead><tbody>{''.join(body)}</tbody></table></div></div>
<script>
(function(){{var tb=document.querySelector('#tt tbody');if(!tb)return;
function sort(k,dir){{var rows=[].slice.call(tb.rows);
rows.sort(function(a,b){{var d=(+a.dataset[k])-(+b.dataset[k]);if(dir==='desc')d=-d;
if(d===0&&k==='pts')d=(+b.dataset.gd)-(+a.dataset.gd);if(d===0&&k==='pts')d=(+b.dataset.gf)-(+a.dataset.gf);
if(d===0)d=(+b.dataset.pts)-(+a.dataset.pts);if(d===0)d=(+b.dataset.gd)-(+a.dataset.gd);
if(d===0)d=a.dataset.name.localeCompare(b.dataset.name);return d;}});
rows.forEach(function(r,i){{r.cells[0].textContent=i+1;tb.appendChild(r);}});}}
document.querySelectorAll('.sb').forEach(function(b){{b.addEventListener('click',function(){{
document.querySelectorAll('.sb').forEach(function(x){{x.classList.toggle('on',x===b);}});sort(b.dataset.k,b.dataset.dir);}});}});
sort('pts','desc');}})();
</script>"""


def matchups(d, teams, log=None):
    """Next gameweek's games. When bookmaker odds are logged for a fixture, its goals, result chances,
    most likely score and clean sheet odds come from the bookmakers; otherwise from the stats model."""
    logged = (log or {}).get("gws", {}).get(str(d["next"]), {}).get("matches", {})
    played = [t for t in teams.values() if t["gp"]]
    if not played:
        return []
    lg_att = sum(t["att_rating"] for t in played) / len(played)
    lg_def = sum(t["def_rating"] for t in played) / len(played)
    games = []
    for f in sorted((f for f in d["fixtures"] if f.get("event") == d["next"] and not has_ended(f)),
                    key=lambda f: f.get("kickoff_time") or ""):
        h, a = teams[f["team_h"]], teams[f["team_a"]]
        lam_h = lg_att * (h["att_rating"] / lg_att) * (a["def_rating"] / lg_def) * HOME_BOOST
        lam_a = lg_att * (a["att_rating"] / lg_att) * (h["def_rating"] / lg_def) / HOME_BOOST
        mk = (logged.get(str(f["id"])) or {}).get("market")
        pick = mk or pick_from_lambdas(lam_h, lam_a)
        games.append(dict(h=h, a=a, lh=pick["lh"], la=pick["la"], cs_h=pick["cs_h"], cs_a=pick["cs_a"],
                          pick=pick, src="odds" if mk else "model", fid=f["id"],
                          kickoff=f.get("kickoff_time")))

    # flag the standouts: clear thresholds, plus the top 3 of the week if they're close
    sides = [(g["lh"], g, "h") for g in games] + [(g["la"], g, "a") for g in games]
    top_att = {(id(g), s) for v, g, s in sorted(sides, key=lambda x: -x[0])[:2] if v >= 1.6}
    cs_sides = [(g["cs_h"], g, "h") for g in games] + [(g["cs_a"], g, "a") for g in games]
    top_cs = {(id(g), s) for v, g, s in sorted(cs_sides, key=lambda x: -x[0])[:2] if v >= 0.40}
    for g in games:
        g["flags"] = []
        for s, opp in (("h", "a"), ("a", "h")):
            me, them = g[s], g[opp]
            lam = g["l" + s]
            cs = g["cs_" + s]
            # a real mismatch needs both halves: a good attack AND a leaky defense (and vice versa)
            strong_att = me["att_rating"] > lg_att and them["def_rating"] > lg_def
            strong_def = me["def_rating"] < lg_def and them["att_rating"] < lg_att
            if strong_att and (lam >= ATTACK_FLAG or (id(g), s) in top_att):
                g["flags"].append(dict(kind="att", side=s, team=me, text=(
                    f"{me['full']} should score: they average {me['gf_pg']:.1f} goals a game "
                    f"({me['gf5']} in their last {me['n5']}), and {them['full']} concede {them['ga_pg']:.1f} "
                    f"({them['ga5']} in their last {them['n5']}). Projected {lam:.1f} goals.")))
            if strong_def and (cs >= CS_FLAG or (id(g), s) in top_cs):
                g["flags"].append(dict(kind="cs", side=s, team=me, text=(
                    f"{me['full']} clean sheet spot: they concede {me['ga_pg']:.1f} a game "
                    f"({me['ga5']} in their last {me['n5']}), and {them['full']} score only {them['gf_pg']:.1f} "
                    f"({them['gf5']} in their last {them['n5']}). {cs:.0%} clean sheet chance.")))
    return games


def matchup_section(d, teams, players, log=None):
    games = matchups(d, teams, log)
    if not games:
        return ""
    mine_by_team = {}
    for p in players.values():
        if p["mine"]:
            mine_by_team.setdefault(p["tid"], []).append(p)

    cards = []
    for g in games:
        h, a = g["h"], g["a"]
        ko, day, sort_key = "", "Date to be confirmed", "9999"
        if g["kickoff"]:
            sort_key = g["kickoff"]
            try:
                dt = datetime.datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00"))
                et = dt.astimezone(ZoneInfo("America/New_York"))
                ko = et.strftime("%-I:%M %p ET")
                day = et.strftime("%A, %b %-d")
            except ValueError:
                pass
        flags = "".join(
            f'<div class="mflag {fl["kind"]}"><span class="mft">{"Goals likely" if fl["kind"] == "att" else "Clean sheet spot"}</span>'
            f'{badge(fl["team"]["id"], 18)}<span>{esc(fl["text"])}</span></div>' for fl in g["flags"])
        mine = mine_by_team.get(h["id"], []) + mine_by_team.get(a["id"], [])
        mine_html = ("" if not mine else
                     '<div class="mmine"><span class="note" style="margin:0">Your players:</span>'
                     + "".join(f'<span class="mp">{photo(p, 30)}{esc(p["name"])}</span>' for p in mine) + "</div>")
        hot = " hot" if g["flags"] else ""

        def side(t, lam, cs, align):
            pos = t.get("table_pos")
            tier = "top" if pos and pos <= 4 else "bot" if pos and pos > t.get("table_n", 20) - 3 else ""
            return (f'<div class="ms {align}">{badge(t["id"], 48)}<div class="mn">{esc(t["full"])}</div>'
                    f'<div class="mpos {tier}" title="League position">{ordinal(pos)} of {t.get("table_n", 20)}'
                    f' · {t["pts"]} pts</div>'
                    f'<div class="fms">{form_chips(t)}</div>'
                    f'<div class="sub2">{cs:.0%} clean sheet</div></div>')

        cards.append((sort_key, day, f'''<div class="mc{hot}">
<div class="mko">{ko}</div>
<div class="mrow">{side(h, g["lh"], g["cs_h"], "l")}
<div class="mscore"><span>{card_pick(g)[2]}</span><i>–</i><span>{card_pick(g)[3]}</span>
<small>{"predicted score (odds)" if g["src"] == "odds" else "predicted score (model)"}</small></div>
{side(a, g["la"], g["cs_a"], "r")}</div>
{wdl_bar(g, h, a)}{flags}{mine_html}</div>'''))

    # earliest kickoff at the top, grouped under a heading for each day
    days_html, current = "", None
    for key, day, card in sorted(cards, key=lambda x: x[0]):
        if day != current:
            if current is not None:
                days_html += "</div>"
            days_html += f'<h4 class="mday">{esc(day)}</h4><div class="mgrid">'
            current = day
        days_html += card
    if current is not None:
        days_html += "</div>"
    flagged = sum(1 for g in games if g["flags"])
    return f"""<div class="card"><h3>Gameweek {d['next']} matchups</h3>
<p class="note">Each game shows the bookmakers' chances of a home win, draw or away win, the favourite, and the
predicted score (the likeliest score for the favourite result, so draws show when a draw is most likely), plus the
three likeliest exact scorelines,
worked out from the match result and over/under 2.5 goals odds averaged across firms. Where no odds are available yet,
the card says so and uses the stats model instead. Clean sheet chances use the same numbers.
{flagged} of {len(games)} games are flagged: <b class="g">Goals likely</b> = strong attack vs leaky defense,
<b class="b">Clean sheet spot</b> = tight defense vs weak attack. Games are in kickoff order (Eastern time).</p>
<p class="note" style="margin-bottom:6px">Projections can't see this week's lineup news. Check before the deadline:</p>
{links_html()}
{days_html}</div>"""


def card_pick(g):
    """Result chances straight from the bookmakers when we have them (fitted model only turns them into
    scorelines), the favourite result, and the likeliest score for that result."""
    pk = g["pick"]
    if g["src"] == "odds" and pk.get("oH") is not None:
        probs = {"H": pk["oH"], "D": pk["oD"], "A": pk["oA"]}
    else:
        probs = {"H": pk["pH"], "D": pk["pD"], "A": pk["pA"]}
    fav = max(probs, key=probs.get)
    grid = score_grid(pk["lh"], pk["la"])
    (h, a), _ = max(((k, v) for k, v in grid.items() if outcome_of(*k) == fav), key=lambda kv: kv[1])
    return probs, fav, h, a


def top_scores(g):
    pk = g["pick"]
    if pk.get("top"):
        return pk["top"]
    grid = score_grid(pk["lh"], pk["la"])
    return [[i, j, v] for (i, j), v in sorted(grid.items(), key=lambda kv: -kv[1])[:3]]


def wdl_bar(g, h, a):
    pk = g["pick"]
    probs, fav_k, _, _ = card_pick(g)
    fav = (fav_k, probs[fav_k])
    fav_lab = {"H": f"{h['name']} win", "D": "Draw", "A": f"{a['name']} win"}[fav[0]]
    tops = " · ".join(f"{i}–{j} <i>{v:.0%}</i>" for i, j, v in top_scores(g))
    src = (f"Bookmakers, {pk.get('books', 0)} firms averaged" if g["src"] == "odds"
           else "Stats model: no bookmaker odds for this game yet")
    return (f'<div class="mwdl"><div class="pbar"><span class="pb-h" style="width:{probs["H"]*100:.0f}%">{probs["H"]:.0%}</span>'
            f'<span class="pb-d" style="width:{probs["D"]*100:.0f}%">{probs["D"]:.0%}</span>'
            f'<span class="pb-a" style="width:{probs["A"]*100:.0f}%">{probs["A"]:.0%}</span></div>'
            f'<div class="mwdl-l"><span>{esc(h["name"])} win</span><span>Draw</span><span>{esc(a["name"])} win</span></div>'
            f'<div class="mwdl-s">Favourite: <b>{esc(fav_lab)} ({fav[1]:.0%})</b> · Likeliest scores: {tops}</div>'
            f'<div class="mwdl-s">{esc(src)}</div></div>')


def teams_section(d, teams, players, log=None):
    return (f'<section id="teams"><h2>Premier League teams and this week\'s matchups</h2>'
            f'{team_table_section(teams)}{matchup_section(d, teams, players, log)}</section>')


def season_section(d):
    rows, n_teams = season_data(d)
    if len(rows) < 2:
        return ""
    labels = [f"GW{r['gw']}" for r in rows]
    pts = [r["pts"] for r in rows]
    avg = [round(r["avg"], 1) for r in rows]
    top = max(pts + avg)
    step = 20 if top > 80 else 10
    y_max = step * (int(top // step) + 1)

    last, prev = rows[-1], rows[-2]
    delta = last["pts"] - prev["pts"]
    best = max(rows, key=lambda r: r["pts"])
    vs_avg = sum(pts) / len(pts) - sum(avg) / len(avg)

    pts_chart = svg_line(
        labels, [("League average", avg, "#d97706", True), ("You", pts, "#2563eb", False)],
        0, y_max, list(range(0, y_max + 1, step)), str,
        tips=[f"GW{r['gw']}: {r['pts']} pts (league avg {r['avg']:.0f}, {ordinal(r['week_rank'])} of {n_teams} that week)"
              for r in rows])

    places = [r["place"] for r in rows]
    place_chart = svg_line(
        labels, [("Place", places, "#2563eb", False)],
        1, n_teams, list(range(1, n_teams + 1)), ordinal, invert=True,
        tips=[f"GW{r['gw']}: {ordinal(r['place'])} of {n_teams} ({r['total']} total pts)" for r in rows])

    move = prev["place"] - last["place"]
    move_txt = ("up " + str(move) if move > 0 else "down " + str(-move) if move < 0 else "no change") + " from last week"
    arrow = "▲" if move > 0 else "▼" if move < 0 else "●"
    move_cls = "pos" if move > 0 else "neg" if move < 0 else "mut"
    d_cls = "pos" if delta > 0 else "neg" if delta < 0 else "mut"

    return f"""<section id="season"><h2>Season so far</h2>
<div class="two">
<div class="card"><div class="ch-head"><div><h3>Points per gameweek</h3>
<p class="note">How your weekly score moves against the Hope Bros average. Hover a point for details.</p></div>
<div class="kpi"><b>{last['pts']}</b><span class="{d_cls}">{delta:+d} vs GW{prev['gw']}</span></div></div>
<div class="legend"><span><i class="sw" style="background:#2563eb"></i>You</span>
<span><i class="sw dash"></i>League average</span></div>
{pts_chart}
<p class="note" style="margin:12px 0 0">Best week: GW{best['gw']} with {best['pts']} pts.
On average you score {abs(vs_avg):.1f} pts per week {'more' if vs_avg >= 0 else 'less'} than the league.</p></div>

<div class="card"><div class="ch-head"><div><h3>League position</h3>
<p class="note">Where you stood in the {n_teams}-team table after each gameweek. Higher is better.</p></div>
<div class="kpi"><b>{ordinal(last['place'])}</b><span class="{move_cls}">{arrow} {move_txt}</span></div></div>
{place_chart}
<div class="placestrip">{''.join(f'<div class="ps{" now" if r is last else ""}"><small>GW{r["gw"]}</small><b>{ordinal(r["place"])}</b></div>' for r in rows[-8:])}</div>
</div></div>
{league_lines(d)}</section>"""


# ---------------------------------------------------------------- lineup links and position battles

LINKS = [
    ("Predicted lineups (RotoWire)", "https://www.rotowire.com/soccer/lineups.php"),
    ("Injury table (Premier Injuries)", "https://www.premierinjuries.com/injury-table.php"),
    ("Team news (Fantasy Football Scout)", "https://www.fantasyfootballscout.co.uk/team-news/"),
]
def links_html():
    return '<div class="links">' + "".join(
        f'<a href="{u}" target="_blank" rel="noopener">{esc(n)} ↗</a>' for n, u in LINKS) + "</div>"


def position_battle(p, players):
    """Did this player start because a teammate was out, and is that teammate now back?"""
    if not p["log"]:
        return None
    mine = {g["gw"]: g["mins"] for g in p["log"]}
    recent = p["log"][-3:]
    best = None
    for q in players.values():
        if q["id"] == p["id"] or q["tid"] != p["tid"] or q["pos"] != p["pos"] or not q["log"]:
            continue
        if not (q["status"] == "a" or (q["chance"] or 0) >= 75):
            continue
        theirs = {g["gw"]: g["mins"] for g in q["log"]}
        covered = [g["gw"] for g in recent if g["mins"] >= 60 and theirs.get(g["gw"], 0) == 0]
        last_gw = p["log"][-1]["gw"]
        replaced = theirs.get(last_gw, 0) >= 60 and mine.get(last_gw, 0) < 30
        established = q["starts"] >= p["starts"] or q["draft_rank"] < p["draft_rank"]
        if established and (covered and (replaced or theirs.get(last_gw, 0) >= 60)):
            score = len(covered) + (2 if replaced else 0)
            if not best or score > best["score"]:
                best = dict(q=q, covered=covered, replaced=replaced, last_gw=last_gw, score=score)
    return best


def battle_note(p, b):
    q = b["q"]
    gws = ", ".join(f"GW{g}" for g in b["covered"])
    txt = f"Position battle: {q['name']} is fit again. {p['name']}'s start{'s' if len(b['covered']) > 1 else ''} in {gws} came while {q['name']} was out"
    if b["replaced"]:
        txt += f", and {q['name']} started GW{b['last_gw']} ahead of him"
    txt += ". Expect his minutes to drop."
    if q["owner"] and not q["mine"]:
        txt += f" {q['name']} is owned by {q['owner']}, so a trade is the way to get the starter."
    elif not q["owner"]:
        txt += f" {q['name']} is a free agent right now."
    return txt




# ---------------------------------------------------------------- squad review

VERDICTS = {  # label, css color, sort order
    "start": ("Start him", "g", 0), "form": ("Coming into form", "g", 1), "core": ("Keep: core", "b", 2),
    "keep": ("Keep", "b", 3), "monitor": ("Monitor", "a", 4), "hold": ("Injured: hold", "a", 5),
    "bench": ("Bench for now", "a", 6), "replace": ("Replace", "r", 7), "drop": ("Drop", "r", 8),
}


def team_ranks(teams):
    played = [t for t in teams.values() if t["gp"]]
    by_def = sorted(played, key=lambda t: t["def_rating"])            # 1 = tightest defense
    by_att = sorted(played, key=lambda t: t["att_rating"], reverse=True)  # 1 = best attack
    return ({t["id"]: i for i, t in enumerate(by_def, 1)}, {t["id"]: i for i, t in enumerate(by_att, 1)})


def review(p, teams, players, ranks):
    t = teams[p["tid"]]
    def_rank, att_rank = ranks
    games = p["log"]
    n = len(games) or max(t["gp"], 1)
    mins = [g["mins"] for g in games]
    played = [g for g in games if g["mins"] > 0]
    total_pts = sum(g["pts"] for g in games) if games else p["total"]
    total_mins = sum(mins) if games else p["mins"]
    full = sum(1 for m in mins if m >= 60)
    ppa = total_pts / len(played) if played else 0.0
    p90 = total_pts / total_mins * 90 if total_mins >= 90 else None
    mins_share = total_mins / (n * 90) if n else 0
    recent = games[-3:]
    recent_played = [g for g in recent if g["mins"] > 0]
    recent_ppa = sum(g["pts"] for g in recent_played) / len(recent_played) if recent_played else 0.0
    recent_share = sum(g["mins"] for g in recent) / (len(recent) * 90) if recent else 0

    # leading run of games with no minutes, then he came back
    missed = 0
    for g in games:
        if g["mins"] == 0:
            missed += 1
        else:
            break
    returned = missed >= 2 and len(played) >= 1
    lost_spot = len(games) >= 4 and all(g["mins"] == 0 for g in games[-2:]) and any(g["mins"] >= 60 for g in games[:-2])

    notes = []
    status_bad = p["status"] in ("i", "s", "u", "n") or (p["chance"] is not None and p["chance"] <= 25)
    news = p["news"] or ""
    if p["status"] != "a" or (p["chance"] is not None and p["chance"] < 100):
        notes.append(f"Current news: {news or STATUS.get(p['status'], '')}"
                     + ("" if "chance" in news or p["chance"] is None else f" ({p['chance']}% chance of playing)") + ".")

    if returned:
        back = played[0]
        notes.append(f"Missed the first {missed} game{'s' if missed > 1 else ''} and is back since GW{back['gw']}. "
                     "Judge him on what he's done since returning, not his season minutes total.")
        if p90 and len(played) <= 2 and total_mins < 120:
            notes.append(f"{total_pts} pts in just {total_mins} minutes ({p90:.1f} per 90). A tiny sample, "
                         "but it shows what he does when he's on the pitch. Minutes should rise as he regains fitness.")
    elif lost_spot:
        notes.append("Played regularly early on but has had no minutes in the last 2 games. Check whether he's injured "
                     "or has lost his place before relying on him.")
    elif played and full < len(played) * 0.6 and len(played) >= 3:
        notes.append(f"Often subbed on or off: 60+ minutes in only {full} of {len(played)} appearances, which caps "
                     "his appearance and clean sheet points.")
    elif len(played) >= 3 and full == len(played) and p["pos"] in ("DEF", "GK"):
        notes.append(f"Has played 60+ minutes in all {full} appearances. Nailed on, so every week he's a "
                     "clean sheet and defensive contribution chance.")

    # returns against good opposition are worth more than returns against bad
    for g in played:
        if g["g"] + g["a"] == 0:
            continue
        opp = teams.get(g["opp"])
        if opp and def_rank.get(opp["id"], 99) <= 6:
            what = " and ".join(x for x in (f"{g['g']} goal{'s' if g['g'] > 1 else ''}" if g["g"] else "",
                                             f"{g['a']} assist{'s' if g['a'] > 1 else ''}" if g["a"] else "") if x)
            notes.append(f"Got {what} against {opp['full']} in GW{g['gw']}, one of the league's best defenses "
                         f"(#{def_rank[opp['id']]}). That's a strong signal, not a soft return.")
            break

    if len(played) >= 3:
        best = max(played, key=lambda g: g["pts"])
        if total_pts > 0 and best["pts"] / total_pts >= 0.5 and best["pts"] >= 10:
            notes.append(f"{best['pts']} of his {total_pts} points came in one game (GW{best['gw']}). "
                         "Outside that he's averaged "
                         f"{(total_pts - best['pts']) / (len(played) - 1):.1f} a game, so treat the total with caution.")
        elif min(g["pts"] for g in played) >= 2 and ppa >= 4:
            notes.append(f"Consistent: at least 2 points in every appearance, averaging {ppa:.1f}.")

    if len(played) >= 4:
        diff = recent_ppa - ppa
        if diff >= 1.5:
            notes.append(f"Trending up: {recent_ppa:.1f} pts a game over his last {len(recent_played)} vs {ppa:.1f} on the season.")
        elif diff <= -2 and recent_played:
            notes.append(f"Cooling off: {recent_ppa:.1f} pts a game over his last {len(recent_played)} vs {ppa:.1f} on the season.")

    if p["pos"] in ("MID", "FWD") and p["xgi"] >= 1 and total_mins >= 270:
        v = finishing_verdict(p)
        if v.startswith("Due"):
            notes.append(f"Underlying numbers are better than his returns: {p['g'] + p['a']} G+A from {p['xgi']:.1f} xGI.")
        elif v.startswith("Running hot"):
            notes.append(f"Returns are ahead of his chances: {p['g'] + p['a']} G+A from {p['xgi']:.1f} xGI. Expect some cooling.")
    if p["pos"] in ("DEF", "MID") and p["dc_hit"] >= 0.5 and len(played) >= 2:
        notes.append(f"Hits the defensive contribution bonus in {p['dc_hit']:.0%} of games, a near-free 2 points.")

    battle = position_battle(p, players)
    if battle:
        notes.insert(0, battle_note(p, battle))

    # ---- verdict
    alt = None
    free = [q for q in players.values() if q["pos"] == p["pos"] and not q["owner"] and q["mins"] >= 270
            and q["start_rate"] >= 0.5 and q["status"] == "a"]
    if free:
        alt = max(free, key=lambda q: q["ppg"])
    long_out = any(k in news.lower() for k in ("unknown return", "joined", "loan", "left the club", "departed"))
    hot_return = returned and recent_played and (recent_ppa >= 6 or (p90 or 0) >= 8)

    if status_bad:
        if long_out and not p["core"]:
            v, why = "drop", "Out with no return date and not one of your core players. The roster spot is worth more."
        elif p["core"]:
            v, why = "hold", "Injured, but he's one of your best players. Bench him and wait."
        else:
            v, why = "bench", "Not fit to play this week. Keep him if the injury is short, otherwise look at a replacement."
    elif battle and battle["replaced"]:
        q = battle["q"]
        if p["core"]:
            v, why = "monitor", f"{q['name']} is back and took his place last week. Keep him, but don't count on starts."
        else:
            v, why = "replace", (f"He was keeping the seat warm for {q['name']}, who's fit and started last week. "
                                 "A player without guaranteed minutes is a roster spot you can use better.")
    elif hot_return:
        v, why = "start", (f"Back from his absence and producing straight away ({recent_ppa:.0f} pts a game since returning). "
                           "Low season minutes are misleading here.")
    elif lost_spot and p["core"]:
        v, why = "monitor", ("One of your better players, but he hasn't played in 2 games. Check the news "
                             "before starting him; don't drop him on this alone.")
    elif p["core"]:
        if p.get("elite"):
            why = f"One of the best at his position ({rank_str(p)}). Keep starting him."
        else:
            why = f"One of your top scorers this season ({total_pts} pts). Keep starting him."
        v = "core"
    elif lost_spot or (len(games) >= 3 and recent_share < 0.35 and mins_share < 0.5 and not returned):
        if alt and alt["ppg"] > p["ppg"] + 1.5:
            v, why = "replace", f"Not getting minutes. {alt['name']} ({alt['team']}) is a free agent who plays."
        else:
            v, why = "monitor", "Minutes have dried up. Watch team news before starting him."
    elif len(played) >= 4 and recent_ppa - ppa >= 1.5:
        v, why = "form", f"Scoring {recent_ppa:.1f} a game lately, up from {ppa:.1f}. Ride the form."
    elif len(played) >= 4 and recent_ppa - ppa <= -2 and recent_share < mins_share:
        v, why = "monitor", "Points and minutes both trending down. Not a drop yet, but watch him."
    elif alt and ppa < 3 and len(played) >= 3 and alt["ppg"] > ppa + 1.5:
        v, why = "replace", (f"Averaging {ppa:.1f} pts a game. {alt['name']} ({alt['team']}) is a free agent "
                             f"averaging {alt['ppg']:.1f} and starting regularly.")
    else:
        v, why = "keep", (f"Averaging {ppa:.1f} pts a game, with 60+ minutes in {full} of {n} games. "
                          "Doing his job, no reason to change.")

    return dict(v=v, why=why, notes=notes, total=total_pts, ppa=ppa, p90=p90, total_mins=total_mins,
                n=n, full=full, apps=len(played), mins_share=mins_share,
                alt=alt if v == "replace" and not battle else None, battle=battle)


def next5(p, teams):
    """Next fixtures rated for this player. Attackers want opponents who concede a lot;
    keepers and defenders want opponents who score little. Uses each team's goal ratings
    (season blended with the last 5 matches), the same numbers as the team table."""
    t = teams[p["tid"]]
    attack = p["pos"] in ("MID", "FWD")
    key = "def_rating" if attack else "att_rating"
    played_t = [x for x in teams.values() if x["gp"]]
    avg = sum(x[key] for x in played_t) / len(played_t) if played_t else 1.3
    chips, ratios = [], []
    for x in t["run"]:
        o = teams[x["opp_id"]]
        val = o[key]
        ratio = val / avg if attack else avg / max(val, 0.1)
        ratios.append(ratio)
        cls = "good" if ratio >= 1.12 else "bad" if ratio <= 0.9 else ""
        verdict = "Good" if cls == "good" else "Hard" if cls == "bad" else "Average"
        where = "Home" if x["home"] else "Away"
        verb = "concede" if attack else "score"
        tip_txt = (f"GW{x['gw']}: vs {o['full']} ({where.lower()}). {o['full']} {verb} {val:.1f} goals a game "
                   f"(league average {avg:.1f}). {verdict} matchup for {'scoring' if attack else 'a clean sheet'}.")
        chips.append(f'<span class="chip {cls}" title="{esc(tip_txt)}"><span class="cgw">GW{x["gw"]} · {where}</span>'
                     f'<span class="cmain">{badge(o["id"], 18)}<b>{esc(o["name"])}</b></span>'
                     f'<span class="cval"><small>{val:.1f}</small> {verb}s/g</span></span>')
    overall = ease_label(sum(ratios) / len(ratios)) if ratios else "Blank"
    what = ("Opponents' goals conceded per game. Higher is better for him." if attack else
            "Opponents' goals scored per game. Lower is better for him.")
    return overall, f'<div class="chips">{"".join(chips) or "No fixtures"}</div>', what


RATING_GAMES = 5
BPS_POOLS = {}  # (gw, pos) -> sorted BPS of everyone at that position who played 60+ that gameweek


def build_bps_pools(d, players):
    BPS_POOLS.clear()
    pos_of = {p["id"]: p["pos"] for p in players.values()}
    for gw, bps in d.get("live_bps", {}).items():
        mins = d.get("live_min", {}).get(gw, {})
        for el, v in bps.items():
            if mins.get(el, 0) >= 60 and el in pos_of:
                BPS_POOLS.setdefault((gw, pos_of[el]), []).append(v)
    for k in BPS_POOLS:
        BPS_POOLS[k].sort()


def match_ratings(p, teams):
    """Last few appearances rated by FPL's Bonus Points System, ranked against same-position starters that week."""
    import bisect
    games = [g for g in p["log"] if g["mins"] > 0][-RATING_GAMES:]
    out = []
    for g in games:
        pool = BPS_POOLS.get((g["gw"], p["pos"]))
        if not pool:
            continue
        pct = bisect.bisect_left(pool, g["bps"]) / len(pool)
        opp = teams.get(g["opp"])
        out.append(dict(gw=g["gw"], bps=g["bps"], pct=pct, mins=g["mins"], opp=g["opp"],
                        opp_name=opp["full"] if opp else "?", home=g["home"]))
    return out


def rank_words(pct):
    return f"top {max(1, round((1 - pct) * 100))}%" if pct >= 0.5 else f"bottom {max(1, round(pct * 100))}%"


def ratings_html(p, teams):
    rs = match_ratings(p, teams)
    if not rs:
        return ""
    pos_word = {"GK": "goalkeepers", "DEF": "defenders", "MID": "midfielders", "FWD": "forwards"}[p["pos"]]
    chips = []
    for r in rs:
        cls = "hi" if r["pct"] >= 0.75 else "lo" if r["pct"] < 0.25 else ""
        sub = r["mins"] < 60
        tip = (f"GW{r['gw']} vs {r['opp_name']} ({'H' if r['home'] else 'A'}), {r['mins']} min: BPS {r['bps']}, "
               f"better than {r['pct']:.0%} of {pos_word} who played 60+ that week"
               + (". Short appearance, so a lower score is expected." if sub else ""))
        chips.append(f'<div class="rt {cls}" title="{esc(tip)}"><b>{r["bps"]}</b><span>{rank_words(r["pct"])}</span>'
                     f'<small>{badge(r["opp"], 12)}GW{r["gw"]}{" · " + str(r["mins"]) + "\'" if sub else ""}</small></div>')
    avg = sum(r["pct"] for r in rs) / len(rs)
    word = "Excellent" if avg >= 0.75 else "Good" if avg >= 0.55 else "Average" if avg >= 0.4 else "Poor"
    n = len(rs)
    return (f'<div class="rv-lbl">Match ratings, last {"game" if n == 1 else f"{n} games"} <span class="lg2">FPL BPS · '
            f'avg: {word}, {rank_words(avg)} of {pos_word}</span></div>'
            f'<div class="rts">{"".join(chips)}</div>')


PREDICT_AHEAD = 3  # gameweeks of predicted points shown on each player's bar chart
POS_BASE = {}       # league-wide points per appearance by position, filled in squad_review_section


def predict_points(p, teams):
    """Simple, explainable points prediction for the next few gameweeks.
    Base: points per appearance (60% season, 40% last 3), pulled toward the position average when the
    sample is small. Then scaled by opponent strength, home/away, and the chance he plays."""
    t = teams[p["tid"]]
    games = p["log"]
    played = [g for g in games if g["mins"] > 0]
    pos_avg = POS_BASE.get(p["pos"], 3.5)
    if played:
        season = sum(g["pts"] for g in played) / len(played)
        last = played[-3:]
        recent = sum(g["pts"] for g in last) / len(last)
        base = 0.6 * season + 0.4 * recent
        k = 2.0  # small samples lean toward the position average
        base = (len(played) * base + k * pos_avg) / (len(played) + k)
    else:
        base = pos_avg * 0.8

    # chance he plays: recent appearances, counting only games since a return from absence
    lead = 0
    for g in games:
        if g["mins"] == 0:
            lead += 1
        else:
            break
    window = games[lead:][-3:] if lead >= 2 and len(games) > lead else games[-3:]
    play = (sum(1 for g in window if g["mins"] > 0) / len(window)) if window else 0.5
    if p["chance"] is not None:
        play = min(play, p["chance"] / 100) if p["chance"] < 100 else play
    if p["status"] in ("i", "s", "u", "n"):
        play = 0.0

    attack = p["pos"] in ("MID", "FWD")
    key = "def_rating" if attack else "att_rating"
    rated = [x for x in teams.values() if x["gp"]]
    lg = sum(x[key] for x in rated) / len(rated) if rated else 1.3
    first_gw = t["run"][0]["gw"] if t["run"] else None
    out = []
    for gw in range(first_gw or 0, (first_gw or 0) + PREDICT_AHEAD):
        fixtures = [x for x in t["run"] if x["gw"] == gw]
        if not fixtures:
            out.append(dict(gw=gw, pts=0.0, opps=[], tip=f"GW{gw}: no fixture (blank gameweek)"))
            continue
        total, parts = 0.0, []
        for x in fixtures:
            o = teams[x["opp_id"]]
            ratio = (o[key] / lg) if attack else (lg / max(o[key], 0.1))
            opp_f = max(0.6, min(1.5, ratio ** 0.8))
            venue = 1.05 if x["home"] else 0.95
            pts = base * opp_f * venue * play
            total += pts
            parts.append(f"vs {o['full']} ({'home' if x['home'] else 'away'}): opponent x{opp_f:.2f}, "
                         f"{'home' if x['home'] else 'away'} x{venue:.2f}")
        tip = (f"GW{gw} predicted {total:.1f} pts. Averages {base:.1f} when he plays; " + "; ".join(parts)
               + f"; {play:.0%} chance he plays.")
        out.append(dict(gw=gw, pts=total, opps=[x["opp_id"] for x in fixtures], tip=tip))
    return out


def gw_bars(p, teams):
    games = p["log"]
    preds = predict_points(p, teams)
    if not games and not preds:
        return '<p class="empty">No match history yet.</p>'
    top = max([12] + [g["pts"] for g in games] + [x["pts"] for x in preds])
    cols = []
    for g in games:
        h = max(4, g["pts"] / top * 64) if g["pts"] > 0 else 3
        cls = "dnp" if g["mins"] == 0 else "full" if g["mins"] >= 60 else "part"
        opp = teams.get(g["opp"])
        what = []
        if g["g"]:
            what.append(f"{g['g']}G")
        if g["a"]:
            what.append(f"{g['a']}A")
        if g["cs"] and p["pos"] in ("GK", "DEF"):
            what.append("CS")
        tip = (f"GW{g['gw']} vs {opp['full'] if opp else '?'} ({'H' if g['home'] else 'A'}): "
               f"{g['pts']} pts, {g['mins']} min" + (f", {' '.join(what)}" if what else ""))
        label = "–" if g["mins"] == 0 else g["pts"]
        cols.append(f'<div class="gb" title="{esc(tip)}"><span class="gv">{label}</span>'
                    f'<div class="gbar {cls}" style="height:{h:.0f}px"></div>'
                    f'{badge(g["opp"], 16)}<small>GW{g["gw"]}</small></div>')
    if preds:
        cols.append('<div class="gsep" title="Predictions start here"></div>')
    for x in preds:
        h = max(4, x["pts"] / top * 64) if x["pts"] > 0 else 3
        badges = "".join(badge(o, 16) for o in x["opps"]) or '<span class="mut" style="font-size:10px">blank</span>'
        cols.append(f'<div class="gb pred" title="{esc(x["tip"])}"><span class="gv">{x["pts"]:.1f}</span>'
                    f'<div class="gbar fut" style="height:{h:.0f}px"></div>'
                    f'<span class="gbadges">{badges}</span><small>GW{x["gw"]}</small></div>')
    return f'<div class="gbs">{"".join(cols)}</div>'


def squad_review_section(squad, players, teams, d=None):
    ranks = team_ranks(teams)
    if d is not None:
        build_bps_pools(d, players)
    for pos in ("GK", "DEF", "MID", "FWD"):
        reg = [q for q in players.values() if q["pos"] == pos and q["starts"] >= 2 and q["log"]]
        apps = [g["pts"] for q in reg for g in q["log"] if g["mins"] > 0]
        POS_BASE[pos] = sum(apps) / len(apps) if apps else 3.5
    items = [(p, review(p, teams, players, ranks)) for p in squad]
    items.sort(key=lambda x: (VERDICTS[x[1]["v"]][2], -x[1]["total"]))
    counts = {}
    for _, r in items:
        counts[r["v"]] = counts.get(r["v"], 0) + 1
    summary = " ".join(f'<span class="vp {VERDICTS[k][1]}">{VERDICTS[k][0]} · {c}</span>'
                       for k, c in sorted(counts.items(), key=lambda kv: VERDICTS[kv[0]][2]))
    cards = []
    for p, r in items:
        label, color, _ = VERDICTS[r["v"]]
        alt = ""
        if r["alt"]:
            a = r["alt"]
            alt = (f'<div class="alt">{photo(a, 36)}<div><b>Option: {esc(a["name"])}</b>'
                   f'<div class="tm">{badge(a["tid"], 14)}{esc(a["team"])} · free agent · {a["ppg"]:.1f} pts a game, '
                   f'{a["starts"]} starts</div></div></div>')
        if r["battle"] and not r["battle"]["q"]["mine"]:
            q = r["battle"]["q"]
            how = (f"owned by {esc(q['owner'])}, trade target" if q["owner"] else "free agent, pick him up")
            alt = (f'<div class="alt">{photo(q, 36)}<div><b>The starter: {esc(q["name"])}</b>'
                   f'<div class="tm">{badge(q["tid"], 14)}{esc(q["team"])} · {how} · '
                   f'{q["starts"]} starts this season</div></div></div>')
        p90 = f'{r["p90"]:.1f}' if r["p90"] is not None else "-"
        notes = "".join(f"<li>{esc(x)}</li>" for x in r["notes"])
        n5_label, n5_chips, n5_what = next5(p, teams)
        cards.append(f'''<div class="rv">
<div class="rv-top">{photo(p, 60)}<div class="pc-id"><div class="pc-name">{esc(p["name"])}</div>
<div class="tm">{badge(p["tid"], 16)}{esc(teams[p["tid"]]["full"])} · {p["pos"]}{" · " + rank_str(p) if p.get("pos_rank") else ""}</div>
<div class="pc-slot">{"Starting XI" if p.get("slot") == "XI" else "Bench"}</div>
<div class="rv-acts"><button class="cmp-add" data-id="{p["id"]}">+ Compare</button>
<button class="cmp-add cmp-repl-btn" data-id="{p["id"]}">Find replacements</button></div></div>
<span class="vp {color}">{label}</span></div>
<p class="rv-why">{esc(r["why"])}</p>
<div class="rv-stats">
<div><span class="k">Points</span><span class="v">{r["total"]}</span></div>
<div><span class="k">Per game</span><span class="v">{r["ppa"]:.1f}</span></div>
<div><span class="k">Per 90</span><span class="v">{p90}</span>{'<span class="k2">needs 90+ min</span>' if r["p90"] is None else ''}</div>
<div><span class="k">Minutes</span><span class="v">{r["total_mins"]}</span><span class="k2">{r["mins_share"]:.0%} of possible</span></div>
<div><span class="k">60+ min</span><span class="v">{r["full"]}/{r["n"]}</span></div></div>
<div class="rv-lbl">Points by gameweek <span class="lg2"><i class="full"></i>60+ min <i class="part"></i>under 60 <i class="dnp"></i>didn't play <i class="fut"></i>predicted</span></div>
{gw_bars(p, teams)}
{ratings_html(p, teams)}
<div class="rv-lbl">Next {HORIZON} games {pill(n5_label)}</div>
<p class="note" style="margin:-8px 0 0;font-size:12px">{n5_what}</p>
{n5_chips}
{f'<ul class="rv-notes">{notes}</ul>' if notes else ''}{alt}</div>''')
    return f"""<section id="squad"><h2>My squad</h2>
<p class="note">Every player on your team: what he's scored each gameweek since the start of the season, how reliable his
minutes are, the context behind the numbers, and whether his next {HORIZON} games are good matchups. Sorted from
strongest call to weakest. Hover a bar or a fixture box for details.</p>
<div class="vsum">{summary}</div>
<div class="rvgrid">{''.join(cards)}</div></section>"""


# ---------------------------------------------------------------- advice

# ---------------------------------------------------------------- rendering

PHOTO = "https://resources.premierleague.com/premierleague/photos/players/110x140/p{code}.png"
BADGE = "https://resources.premierleague.com/premierleague/badges/70/t{code}.png"
TEAMS = {}  # filled in render() so the image helpers can find badge codes


def badge(tid, size=20):
    t = TEAMS.get(tid)
    if not t:
        return ""
    return (f'<img class="badge" src="{BADGE.format(code=t["code"])}" alt="" width="{size}" height="{size}" '
            f'loading="lazy" onerror="this.style.visibility=\'hidden\'">')


def photo(p, size=44):
    initials = "".join(w[0] for w in p["name"].replace(".", " ").split()[:2]).upper()
    img = (f'<img src="{PHOTO.format(code=p["code"])}" alt="" loading="lazy" '
           f'onerror="this.remove()">') if p.get("code") else ""
    return f'<span class="ph" style="--s:{size}px"><span class="ini">{esc(initials)}</span>{img}</span>'


def who(p):
    star = '<span class="star" title="Your player">★</span>' if p["mine"] else ""
    return (f'<div class="who">{photo(p)}<div><div class="nm">{esc(p["name"])}{star}'
            f'<button class="cmp-add" data-id="{p["id"]}">+ Compare</button></div>'
            f'<div class="tm">{badge(p["tid"], 16)}{esc(p["team"])} · {p["pos"]}</div></div></div>')


def team_cell(t):
    return f'<div class="who">{badge(t["id"], 32)}<div><div class="nm">{esc(t["full"])}</div></div></div>'


def table(rows, cols, mark_mine=True):
    if not rows:
        return '<p class="empty">Nothing to show right now.</p>'
    head = "".join(f"<th>{c}</th>" for c, _ in cols)
    body = []
    for r in rows:
        cls = ' class="mine"' if mark_mine and r.get("mine") else ""
        cells = "".join(f"<td>{fn(r)}</td>" for _, fn in cols)
        body.append(f"<tr{cls}>{cells}</tr>")
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def diff_cell(v):
    cls = "pos" if v >= 0.75 else "neg" if v <= -0.75 else "mut"
    return f'<span class="num {cls}">{v:+.1f}</span>'


def pill(label):
    cls = {"Easy": "e1", "Good": "e2", "Average": "e3", "Tough": "e4", "Very tough": "e5", "Blank": "e5"}.get(label, "e3")
    return f'<span class="pill {cls}">{label}</span>'


def verdict_pill(v):
    cls = ("e1" if v.startswith(("Due", "Slightly unlucky")) else
           "blue" if v.startswith("Elite") else
           "e4" if v.startswith("Running") else "e3")
    return f'<span class="pill {cls}">{esc(v)}</span>'


def owner_cell(p):
    if p["mine"]:
        return '<span class="tag you">You</span>'
    return f'<span class="tag">{esc(p["owner"])}</span>' if p["owner"] else '<span class="tag fa">Free agent</span>'


def big(v):
    return f'<span class="bignum">{v}</span>'


def pct_bar(v):
    v = max(0.0, min(1.0, v))
    return f'<div class="bar"><span style="width:{v * 100:.0f}%"></span></div><span class="num">{v:.0%}</span>'


def finishing_section(players):
    outfield = [p for p in players.values() if p["pos"] != "GK" and p["mins"] >= 180]
    cols = [("Player", who), ("Rank", lambda p: f'<span class="tag">{rank_str(p)}</span>'),
            ("Goals", lambda p: f'{big(p["g"])}<span class="sub2">xG {p["xg"]:.1f}</span>'),
            ("+/-", lambda p: diff_cell(p["g_diff"])),
            ("Assists", lambda p: f'{big(p["a"])}<span class="sub2">xA {p["xa"]:.1f}</span>'),
            ("+/-", lambda p: diff_cell(p["a_diff"])),
            ("Verdict", lambda p: verdict_pill(finishing_verdict(p))), ("Owner", owner_cell)]

    mine = sorted((p for p in outfield if p["mine"]), key=lambda p: p["gi_diff"])
    due = sorted((p for p in outfield if not p["mine"] and p["xgi"] >= 1.2 and p["start_rate"] >= 0.6),
                 key=lambda p: p["gi_diff"])[:10]
    hot = sorted((p for p in outfield if p["xgi"] >= 0.5), key=lambda p: p["gi_diff"], reverse=True)[:8]

    return (f'<div class="card"><h3>Your squad</h3>{table(mine, cols, False)}</div>'
            '<div class="card"><h3>Buy-low targets</h3>'
            '<p class="note">Getting the chances but not the returns yet. Over a season, finishing drifts back toward xG.</p>'
            f'{table(due, cols)}</div>'
            '<div class="card"><h3>Running hot</h3>'
            '<p class="note">Scoring more than their chances suggest. <b>Elite and clinical</b> means still a top-25% '
            'player after cooling, so keep. <b>Running hot</b> means ordinary underlying numbers, so the points may not last.</p>'
            f'{table(hot, cols)}</div>')


def market_section(players, teams):
    out = ""
    for pos, label in (("GK", "Goalkeepers"), ("DEF", "Defenders"), ("MID", "Midfielders"), ("FWD", "Forwards")):
        cols = [("Player", who),
                ("Points", lambda p: f'{big(p["total"])}<span class="sub2">{p["ppg"]:.1f} a game</span>'),
                ("Starts", lambda p: f'{big(p["starts"])}<span class="sub2">of {p["gp"]}</span>'),
                ("G+A / xGI", lambda p: f'{big(p["g"] + p["a"])}<span class="sub2">xGI {p["xgi"]:.1f}</span>'),
                (f"Next {HORIZON}", lambda p: pill(next5(p, teams)[0]))]
        if pos in ("DEF", "MID"):
            cols.insert(4, ("Hits DC bonus", lambda p: pct_bar(p["dc_hit"])))
        fa = sorted((p for p in players.values() if p["pos"] == pos and not p["owner"] and p["status"] == "a"
                     and p["mins"] >= 270 and p["start_rate"] >= 0.5),
                    key=lambda p: p["ppg"], reverse=True)[:6]
        out += f'<div class="card"><h3>{label}</h3>{table(fa, cols, False)}</div>'
    return out


CSS = """
:root{--bg:#f3f6fb;--card:#fff;--ink:#0f1b2d;--muted:#5b6b82;--line:#e3e9f2;--blue:#1d4ed8;--blue2:#2563eb;
--blue-soft:#e8efff;--navy:#0b2a5b;--green:#15803d;--green-soft:#e7f6ec;--red:#c0262d;--red-soft:#fdecec;
--amber:#b45309;--amber-soft:#fdf3e2;--mine:#f2f7ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:linear-gradient(135deg,var(--navy),var(--blue));color:#fff;padding:40px 20px 34px}
header .in,main{max-width:1180px;margin:0 auto}
header h1{margin:0;font-size:32px;letter-spacing:-.5px}
header p{margin:8px 0 0;opacity:.85}
.stats{display:flex;flex-wrap:wrap;gap:14px;margin-top:22px}
.stat{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.2);border-radius:14px;padding:12px 18px;min-width:130px}
.stat b{display:block;font-size:24px}.stat span{font-size:13px;opacity:.85}
nav{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line)}
nav .in{max-width:1180px;margin:0 auto;display:flex;gap:6px;overflow-x:auto;padding:10px 16px}
nav a{color:var(--blue);text-decoration:none;font-weight:600;font-size:14px;padding:8px 14px;border-radius:999px;white-space:nowrap}
nav a:hover{background:var(--blue-soft)}
main{padding:12px 16px 80px}
section{margin-top:48px}
h2{font-size:26px;margin:0 0 6px;color:var(--navy);letter-spacing:-.3px}
h2+p.note{margin-bottom:20px}
h3{font-size:19px;margin:0 0 6px;color:var(--navy)}
h4{font-size:14px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:22px 0 10px}
h3.lbl{margin:28px 0 14px}
.note{color:var(--muted);font-size:15px;margin:0 0 16px;max-width:820px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin:20px 0;
box-shadow:0 1px 2px rgba(15,27,45,.04)}
.explain{background:var(--blue-soft);border-radius:14px;padding:18px 22px;color:var(--navy);margin:16px 0 8px;max-width:900px}
.tw{overflow-x:auto;margin:0 -8px}
table{border-collapse:separate;border-spacing:0;width:100%;font-size:15px}
th{text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:700;
padding:10px 14px;border-bottom:2px solid var(--line);white-space:nowrap}
td{padding:14px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
tr:last-child td{border-bottom:0}
tr.mine td{background:var(--mine)}
.who{display:flex;align-items:center;gap:12px}
.nm{font-weight:700;font-size:16px}.tm{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}
.star{color:var(--blue);margin-left:6px}
.ph{position:relative;display:inline-flex;align-items:flex-end;justify-content:center;width:var(--s);height:var(--s);
border-radius:50%;background:var(--blue-soft);overflow:hidden;flex:none;border:2px solid #fff;box-shadow:0 0 0 1px var(--line)}
.ph img{position:absolute;inset:auto 0 0 0;width:100%;height:auto;top:6%}
.ini{font-weight:700;color:var(--blue);font-size:calc(var(--s)*.34);align-self:center}
.badge{flex:none;object-fit:contain}
.bignum{font-size:20px;font-weight:700;display:block;line-height:1.2}
.sub2{display:block;font-size:12px;color:var(--muted)}
.num{font-weight:700}.pos{color:var(--green)}.neg{color:var(--red)}.mut{color:var(--muted)}
.pill{display:inline-block;padding:4px 12px;border-radius:999px;font-size:13px;font-weight:700}
.e1{background:var(--green-soft);color:var(--green)}.e2{background:#eef8f1;color:#2f855a}
.e3{background:#eef1f6;color:var(--muted)}.e4{background:var(--amber-soft);color:var(--amber)}
.e5{background:var(--red-soft);color:var(--red)}.blue{background:var(--blue-soft);color:var(--blue)}
.tag{display:inline-block;padding:3px 10px;border-radius:8px;background:#eef1f6;font-size:13px;font-weight:600}
.tag.you{background:var(--blue);color:#fff}.tag.fa{background:var(--green-soft);color:var(--green)}
.bar{display:inline-block;width:70px;height:8px;border-radius:9px;background:#e8edf5;vertical-align:middle;margin-right:8px;overflow:hidden}
.bar span{display:block;height:100%;background:var(--blue2);border-radius:9px}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:10px;background:#f1f4f9;font-size:13px;border:1px solid var(--line)}
.chip i{font-style:normal;color:var(--muted);font-size:12px}.chip small{font-weight:700}
.chip.good{background:var(--green-soft);border-color:#c7ebd3}.chip.good small{color:var(--green)}
.chip.bad{background:var(--red-soft);border-color:#f5c9cb}.chip.bad small{color:var(--red)}
.tips{display:grid;gap:14px}
.tip{display:flex;gap:18px;align-items:center;background:#fff;border:1px solid var(--line);border-left:6px solid var(--blue);
border-radius:16px;padding:18px 22px}
.tip.t-green{border-left-color:var(--green)}.tip.t-red{border-left-color:var(--red)}.tip.t-amber{border-left-color:var(--amber)}
.faces{display:flex}.faces .ph+.ph{margin-left:-14px}
.tip-kind{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.7px;color:var(--muted)}
.tip-title{font-size:18px;font-weight:700;margin:2px 0 4px}.tip p{margin:0;color:#334155}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:18px}
.pc{background:#fff;border:1px solid var(--line);border-radius:18px;padding:20px;display:flex;flex-direction:column;gap:16px}
.pc.bench{background:#fafbfd}
.pc-top{display:flex;gap:14px;align-items:center}.pc-id{flex:1;min-width:0}
.pc-name{font-size:19px;font-weight:800}.pc-slot{font-size:13px;color:var(--muted);margin-top:2px}
.pc-proj{text-align:center;background:var(--blue-soft);color:var(--blue);border-radius:14px;padding:8px 12px}
.pc-proj span{display:block;font-size:24px;font-weight:800;line-height:1.1}.pc-proj small{font-size:11px;font-weight:600}
.pc-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;background:#f7f9fc;border-radius:12px;padding:12px}
.pc-stats div{text-align:center}.k{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.v{font-weight:800;font-size:16px}
.pc-fix{display:flex;flex-direction:column;align-items:flex-start;gap:10px}
.flags{display:flex;flex-wrap:wrap;gap:8px}
.flag{font-size:13px;padding:4px 10px;border-radius:8px;background:var(--amber-soft);color:var(--amber);font-weight:600}
.flag.ok{background:var(--blue-soft);color:var(--blue)}.flag.good{background:var(--green-soft);color:var(--green)}
.inj{display:flex;gap:14px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}.inj:last-child{border:0}
.empty{color:var(--muted);font-style:italic}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:20px}.two .card{margin:0}\n.ch-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}\n.kpi{text-align:right;flex:none}.kpi b{display:block;font-size:34px;line-height:1;color:var(--navy)}.kpi span{font-size:14px;font-weight:700}\n.legend{display:flex;gap:18px;font-size:13px;color:var(--muted);margin:4px 0 2px}.legend span{display:flex;align-items:center;gap:6px}\n.sw{display:inline-block;width:18px;height:3px;border-radius:2px}.sw.dash{background:repeating-linear-gradient(90deg,#d97706 0 6px,transparent 6px 10px)}\n.chart{width:100%;height:auto;display:block;margin-top:6px}.chart .grid{stroke:#e8edf5;stroke-width:1}\n.chart text{font-size:12px;fill:#5b6b82}.chart .ytick{text-anchor:end}.chart .xtick{text-anchor:middle}\n.chart .pt:hover circle:last-child{r:7}\n.placestrip{display:flex;gap:6px;margin-top:14px}.ps{flex:1;min-width:0;text-align:center;background:#f3f6fb;border-radius:10px;padding:8px 4px}\n.ps small{display:block;font-size:11px;color:var(--muted)}.ps b{font-size:16px}.ps.now{background:var(--blue);color:#fff}.ps.now small{color:#dbe6ff}\n@media (max-width:600px){.two{grid-template-columns:1fr}.kpi b{font-size:28px}.chart text{font-size:22px}.ps b{font-size:14px}.ps small{font-size:10px}}\n.chart.big{margin-top:14px}.chart .axt{font-weight:600}\n.chart .endlab text{font-size:13px;font-weight:600}.chart .endlab.me text{font-weight:800}.chartwrap{overflow-x:auto}\n.chart .series,.chart .endlab{transition:opacity .15s}.chart .dim{opacity:.12}\n.lg-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}\n.lg-btn{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 12px;font:600 13px/1 inherit;color:var(--ink);cursor:pointer}\n.lg-btn:hover{background:var(--blue-soft)}.lg-btn.on{border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-soft)}\n.lg-btn .sw,.lgn .sw{width:12px;height:12px;border-radius:50%}.lgn{display:inline-flex;align-items:center;gap:8px;font-weight:600}\n.lgt td,.lgt th{text-align:center;padding:10px 12px}.lgt td:first-child,.lgt th:first-child{text-align:left}\n.lgt td.wk{font-weight:800;color:var(--navy)}\n@media (max-width:600px){.chart.big{min-width:760px}.chart.big text{font-size:13px}}\n.sortbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:4px 0 14px}\n.sb{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 14px;font:600 13px/1 inherit;color:var(--blue);cursor:pointer}\n.sb.on{background:var(--blue);color:#fff;border-color:var(--blue)}\n.tt td.rk{font-weight:800;color:var(--muted);width:36px}\n.fms{display:flex;gap:4px}.fm{display:inline-flex;width:24px;height:24px;border-radius:6px;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff}.fm.W{background:var(--green)}.fm.D{background:#94a3b8}.fm.L{background:var(--red)}\n.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}\n.mc{border:1px solid var(--line);border-radius:16px;padding:18px;background:#fff;display:flex;flex-direction:column;gap:12px}\n.mc.hot{border-color:#bfd2ff;box-shadow:0 0 0 3px var(--blue-soft)}\n.mko{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.5px}\n.mrow{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:10px}\n.ms{display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}.mn{font-weight:800;font-size:15px}\n.mscore{display:flex;flex-wrap:wrap;justify-content:center;align-items:baseline;gap:6px;max-width:120px}\n.mscore span{font-size:28px;font-weight:800;color:var(--navy)}.mscore i{font-style:normal;color:var(--muted)}\n.mscore small{width:100%;text-align:center;font-size:11px;color:var(--muted)}\n.mflag{display:flex;gap:10px;align-items:flex-start;border-radius:12px;padding:10px 12px;font-size:14px;line-height:1.5}\n.mflag.att{background:var(--green-soft)}.mflag.cs{background:var(--blue-soft)}\n.mft{flex:none;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;padding:2px 8px;border-radius:6px;color:#fff;margin-top:2px}\n.mflag.att .mft{background:var(--green)}.mflag.cs .mft{background:var(--blue)}\n.mflag .badge{margin-top:2px}b.g{color:var(--green)}b.b{color:var(--blue)}\n.mmine{display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-top:1px solid var(--line);padding-top:10px}\n.mp{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:13px;background:#f3f6fb;border-radius:999px;padding:3px 10px 3px 3px}\n@media (max-width:600px){.mgrid{grid-template-columns:1fr}.mflag{flex-wrap:wrap}}\n.vsum{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 18px}\n.vp{display:inline-block;padding:5px 12px;border-radius:999px;font-size:13px;font-weight:800;white-space:nowrap}\n.vp.g{background:var(--green-soft);color:var(--green)}.vp.b{background:var(--blue-soft);color:var(--blue)}\n.vp.a{background:var(--amber-soft);color:var(--amber)}.vp.r{background:var(--red-soft);color:var(--red)}\n.rvgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:18px}\n.rv{background:#fff;border:1px solid var(--line);border-radius:18px;padding:20px;display:flex;flex-direction:column;gap:14px}\n.rv-top{display:flex;gap:14px;align-items:center}.rv-top .vp{flex:none}\n.rv-why{margin:0;font-size:15px;font-weight:600;color:var(--navy)}\n.rv-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;background:#f7f9fc;border-radius:12px;padding:12px 8px}\n.rv-stats div{text-align:center}.k2{display:block;font-size:10px;color:var(--muted)}\n.rv-lbl{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px}\n.lg2{display:flex;gap:10px;align-items:center;text-transform:none;letter-spacing:0;font-weight:600}\n.lg2 i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:-6px}\n.lg2 i.full,.gbar.full{background:var(--blue2)}.lg2 i.part,.gbar.part{background:#9ec5f4}.lg2 i.dnp,.gbar.dnp{background:#d5dbe5}\n.gbs{display:flex;gap:6px;align-items:flex-end;overflow-x:auto;padding-bottom:2px}\n.gb{flex:1;min-width:34px;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:default}\n.gbar{width:100%;max-width:30px;border-radius:5px 5px 2px 2px}.gv{font-size:13px;font-weight:800}\n.gb small{font-size:10px;color:var(--muted)}\n.rv-notes{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px;color:#334155;font-size:14px;line-height:1.55}\n.alt{display:flex;gap:10px;align-items:center;background:var(--green-soft);border-radius:12px;padding:10px 12px}\n@media (max-width:600px){.rvgrid{grid-template-columns:1fr}.rv-stats{grid-template-columns:repeat(3,1fr)}}\n.links{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 16px}.links a{color:var(--blue);font-weight:600;font-size:13px;text-decoration:none;border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 12px}\n.links a:hover{background:var(--blue-soft)}.cnt{display:inline-block;background:var(--blue-soft);color:var(--blue);font-size:13px;border-radius:999px;padding:1px 9px;vertical-align:middle}\n.chip{flex-direction:column;align-items:flex-start;gap:2px;padding:6px 10px;cursor:help}\n.cgw{font-size:11px;color:var(--muted);font-weight:600}.cmain{display:flex;align-items:center;gap:6px}\n.cval{font-size:11px;color:var(--muted)}.cval small{font-size:14px;font-weight:800;color:var(--ink)}\n.chip.good .cval small{color:var(--green)}.chip.bad .cval small{color:var(--red)}\n.ckey{background:#f7f9fc;border-radius:12px;padding:12px 14px;font-size:14px;line-height:2;margin:0 0 8px}\n.ckey .chip{display:inline-flex;vertical-align:middle;padding:2px 8px;margin:0 2px;cursor:default}\n.rv-lbl .pill{text-transform:none;letter-spacing:0;font-size:12px}\n.wdl{font-weight:700;font-size:15px}.wdl i{font-style:normal;color:var(--muted);margin:0 3px}\n.gih{color:var(--amber);font-weight:700}\n.scrow{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin:8px 0}\n.sc{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px}.sc h4{margin:0 0 10px}\n.scg{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.scg b{display:block;font-size:22px;color:var(--navy)}\n.scg span{font-size:12px;color:var(--muted)}\n.pbar{display:flex;height:26px;border-radius:8px;overflow:hidden;font-size:11px;font-weight:700;color:#fff}\n.pbar span{display:flex;align-items:center;justify-content:center;min-width:28px}\n.pbar .pb-h{background:var(--blue2)}.pbar .pb-d{background:#94a3b8}.pbar .pb-a{background:#d97706}\n.pgw{background:#fff;border:1px solid var(--line);border-radius:16px;margin:14px 0;padding:6px 16px}\n.pgw summary{cursor:pointer;font-weight:800;color:var(--navy);padding:10px 0}\n.ok{color:var(--green);font-weight:800}.no{color:var(--red);font-weight:800}\n#predictions td{white-space:normal;vertical-align:top}#predictions .who{margin:2px 0}\n.dchips{display:flex;gap:5px}.dcol{display:flex;flex-direction:column;align-items:center;gap:2px}\n.dcol small{font-size:10px;color:var(--muted)}\n.dchip{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:28px;border-radius:7px;font-weight:800;font-size:13px;background:#eef1f6;color:var(--ink);cursor:default}\n.dchip.up{background:var(--green-soft);color:var(--green)}.dchip.down{background:var(--red-soft);color:var(--red)}\n.dchip.dnp{color:var(--muted);background:#f5f6f8}\n.bxt td{text-align:center;padding:10px 8px}.bxt td:first-child{text-align:left}\n.bx{border-radius:10px;background:#f3f5f9;border:3px solid #fff}\n.bx.up{background:#d8f2e0;color:var(--green)}.bx.down{background:#fbdcdc;color:var(--red)}\n.bx-word{display:block;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px}\n.bx-gap{display:block;font-size:20px;font-weight:800;line-height:1.2}\n.bx-sub{display:block;font-size:11px;color:var(--muted);font-weight:600}\n.bxt tr.sum td{border-top:2px solid var(--line)}\n.rec{border-radius:10px;border:3px solid #fff}.rec.good{background:var(--green);color:#fff}\n.rec.bad{background:var(--red);color:#fff}.rec.even{background:#94a3b8;color:#fff}\n.rec-n{display:block;font-size:20px;font-weight:800}.rec-v{display:block;font-size:12px;font-weight:700;opacity:.9}\n@media (max-width:600px){.bxt{font-size:12px;table-layout:fixed}.bxt td,.bxt th{white-space:normal}.bxt td,.bxt th{padding:5px 2px}.bxt td:first-child{width:52px;font-size:12px}.bx-gap{font-size:15px}.bx-word{font-size:9px}.bx-sub{font-size:9px}.rec-n{font-size:15px}.rec-v{font-size:10px}.bx,.rec{border-width:2px}}\n.gbar.fut,.lg2 i.fut{background:repeating-linear-gradient(135deg,#d97706 0 4px,#f5b257 4px 8px)}\n.gb.pred .gv{color:#b45309}.gb.pred small{color:#b45309;font-weight:700}\n.gsep{width:2px;align-self:stretch;background:repeating-linear-gradient(180deg,#cbd5e1 0 4px,transparent 4px 8px);margin:0 4px}\n.gbadges{display:flex;gap:2px;min-height:16px;align-items:center}\n.mday{margin:26px 0 12px;font-size:15px;color:var(--navy);text-transform:none;letter-spacing:0;border-bottom:2px solid var(--line);padding-bottom:6px}\n.rts{display:flex;gap:6px;flex-wrap:wrap}\n.rt{flex:1;min-width:58px;max-width:90px;text-align:center;border-radius:10px;padding:6px 4px;background:#eef1f6;cursor:default}\n.rt b{display:block;font-size:18px;color:var(--ink)}.rt span{display:block;font-size:11px;font-weight:700;color:var(--muted)}\n.rt small{display:flex;align-items:center;justify-content:center;gap:3px;font-size:10px;color:var(--muted)}\n.rt.hi{background:var(--green-soft)}.rt.hi b,.rt.hi span{color:var(--green)}\n.rt.lo{background:var(--red-soft)}.rt.lo b,.rt.lo span{color:var(--red)}\n.cmp-tools{display:flex;flex-direction:column;gap:12px}.cmp-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center}\n.cmp-row label{font-weight:700;color:var(--navy)}\n.cmp-row input,.cmp-row select{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:#fff;min-width:0}\n.cmp-row input[type=search]{flex:1;min-width:200px}.cmp-row input[type=checkbox]{width:18px;height:18px;padding:0;margin:0}\n.cmp-res{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}\n.cmp-r{display:flex;align-items:center;gap:10px;text-align:left;border:1px solid var(--line);background:#fff;border-radius:12px;padding:8px 10px;cursor:pointer;font:inherit}.cmp-r:hover{border-color:var(--blue);background:var(--blue-soft)}\n.cmp-r > span:not(.ph){flex:1;min-width:0}.cmp-r b{display:block}.cmp-r small{display:flex;align-items:center;gap:4px;color:var(--muted);font-size:12px}\n.cmp-r em{font-style:normal;font-weight:800;color:var(--blue);font-size:13px;white-space:nowrap}\n.cmp-bar{display:flex;justify-content:space-between;align-items:center;margin:16px 0 8px;font-weight:700;color:var(--navy)}\n.cv{background:var(--navy);color:#fff;border-radius:16px;padding:16px 20px;margin:0 0 14px}\n.cv-k{display:inline-block;background:rgba(255,255,255,.18);border-radius:999px;padding:2px 10px;font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;margin-right:8px}.cv b{font-size:20px}.cv p{margin:8px 0 0;opacity:.92}\n.ct{table-layout:fixed}.ct th,.ct td{white-space:normal;vertical-align:top}\n.ct thead th{background:#fff;color:var(--ink);text-transform:none;letter-spacing:0;font-size:14px;border-bottom:2px solid var(--line)}\n.ct tbody th{text-align:left;color:var(--muted);font-size:12px;width:150px}.ct-l{width:150px}\n@media (max-width:600px){.ct tbody th,.ct-l{width:92px;font-size:11px}.ct td{font-size:14px}.ch{flex-direction:column;align-items:flex-start;gap:4px}.ch .ph{--s:40px!important}.ct th,.ct td{padding:8px 6px}.lgx{display:none}}\n.ct td{font-weight:800;font-size:16px}.ct td small{display:block;font-weight:600;font-size:11px;color:var(--muted)}\n.ct td.best{background:var(--green-soft);color:var(--green)}\n.ch{display:flex;gap:10px;align-items:center;position:relative}.ch b{display:block;font-size:15px}\n.ch small{display:flex;align-items:center;gap:4px;font-weight:600;color:var(--muted);font-size:12px}\n.cx{position:absolute;top:-6px;right:-4px;border:0;background:#eef1f6;border-radius:50%;width:24px;height:24px;cursor:pointer;font-size:16px;line-height:1}\n.cw{display:flex;align-items:flex-end;gap:3px;height:50px;margin-top:10px}.cw i{flex:1;max-width:14px;border-radius:3px 3px 1px 1px}\n.cw i.f,.lgx i.f{background:var(--blue2)}.cw i.p,.lgx i.p{background:#9ec5f4}.cw i.d,.lgx i.d{background:#d5dbe5}\n.cw i.x,.lgx i.x{background:repeating-linear-gradient(135deg,#d97706 0 3px,#f5b257 3px 6px)}\n.cmp-sep{width:1px;align-self:stretch;background:#cbd5e1;margin:0 2px}\n.lgx{display:flex;flex-wrap:wrap;gap:6px;font-size:11px;font-weight:600;color:var(--muted);text-transform:none;letter-spacing:0}\n.lgx i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:-3px}\n.ccs{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.cc{font-size:11px;font-weight:700;background:#eef1f6;border-radius:6px;padding:2px 6px;color:var(--ink)}\n.cc small{display:inline;margin-left:2px;color:var(--muted)}.cc.good{background:var(--green-soft);color:var(--green)}.cc.bad{background:var(--red-soft);color:var(--red)}\n.cmp-add{border:1px solid var(--line);background:#fff;color:var(--blue);border-radius:999px;padding:2px 9px;font:700 11px/1.6 inherit;cursor:pointer;margin-left:8px;vertical-align:middle;white-space:nowrap}.cmp-add:hover{background:var(--blue-soft)}\n.rv-acts{display:flex;gap:6px;margin-top:6px}.rv-acts .cmp-add{margin-left:0}\n.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);background:var(--navy);color:#fff;padding:10px 16px;border-radius:999px;font-weight:700;font-size:14px;opacity:0;pointer-events:none;transition:.2s;z-index:20}\n.toast.on{opacity:1;transform:translateX(-50%) translateY(0)}\n.cand-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:12px}\n.cand-tools{margin-bottom:10px}.cand-sortl,.cand-chk{display:flex;align-items:center;gap:6px;font-size:14px;color:var(--muted);font-weight:600}\n.cand-tw{max-height:560px;overflow:auto}.cand thead{position:sticky;top:0;z-index:3}.cand thead th{background:#fff}\n.cand td{white-space:normal}.cand td b{display:block;font-size:15px}.cand td small{display:block;font-size:12px;font-weight:700}\n.cand tr.cand-me td{background:var(--blue-soft)}.cand tr.cand-on td{background:#f2fbf5}\n.cand-btn{border:1px solid var(--blue);background:#fff;color:var(--blue);border-radius:999px;padding:5px 12px;font:700 12px/1.4 inherit;cursor:pointer;white-space:nowrap}\n.cand-btn.on{background:var(--green);border-color:var(--green);color:#fff}\n.ms{font-weight:800;font-size:17px}.ms-l{font-size:11px;font-weight:700;border-radius:6px;padding:0 6px}
.ms.m1,.ms.m2{color:var(--green)}.ms.m3{color:var(--amber)}.ms.m4{color:var(--red)}
.ms-l.m1{background:#e3f6ea;color:#0b6b35}.ms-l.m2{background:#eef7f1;color:#2d7a4b}.ms-l.m3{background:var(--amber-soft);color:var(--amber)}.ms-l.m4{background:#fdeaea;color:var(--red)}
.cand td small.ms-l{display:inline-block;margin-top:2px}
.spyn{display:inline-block;font-weight:800;border-radius:6px;padding:0 8px}.spyn.yes{color:#fff;background:var(--green)}.spyn.no{color:var(--muted);background:#eef1f6}
.sp-tag{display:inline-block;margin-left:4px;font-size:11px;font-weight:700;color:#0b6b35;background:#e3f6ea;border-radius:6px;padding:0 6px}
.cand-flag{display:inline-block;margin-left:4px;font-size:11px;font-weight:700;color:var(--amber);background:var(--amber-soft);border-radius:6px;padding:0 6px}\n.cand-btn-m{display:none;margin-top:4px}\n@media (max-width:600px){.cand th,.cand td{padding:8px 6px}.cand-head h3{font-size:16px}.cand-act{display:none}.cand-btn-m{display:inline-block}}\nnav a.on{background:var(--blue);color:#fff}nav a.on:hover{background:var(--blue)}\nmain > section{margin-top:28px}main > section[hidden]{display:none}\n.toast.go{pointer-events:auto;cursor:pointer}\n.rvgrid > *,.grid > *,.mgrid > *{min-width:0}\n@media (max-width:600px){.ms .fms{gap:3px}.ms .fm{width:19px;height:19px;font-size:10px;border-radius:5px}.mrow{gap:6px}.mscore span{font-size:24px}.mn{font-size:14px}.mc{padding:14px}}\nnav .ngrp{align-self:center;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);padding:0 4px 0 10px;white-space:nowrap}nav .ngrp:first-child{padding-left:0}\n.glance{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:8px 0 12px}\n.glance div{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px 14px}.glance b{display:block;font-size:24px;color:var(--navy)}\n.glance span{font-size:12px;color:var(--muted);font-weight:600}\n.rcgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:18px;margin-top:16px;align-items:start}.rcgrid > *{min-width:0}\n.rc{background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px 20px;display:flex;flex-direction:column;gap:10px}\n.rc-top{display:flex;justify-content:space-between;align-items:center;gap:8px}.rc-when{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.4px}\n.rc-v{font-size:12px;font-weight:800;border-radius:999px;padding:3px 10px;white-space:nowrap}.rc-v.ok{background:#eef1f6;color:var(--muted)}\n.rc-v.surprise{background:var(--amber-soft);color:var(--amber)}.rc-v.upset{background:var(--red-soft);color:var(--red)}\n.rc-score{display:grid;grid-template-columns:auto 1fr auto 1fr auto;align-items:center;gap:10px}\n.rc-score b{font-size:16px}.rc-score b:nth-of-type(2){text-align:right}.rc-n{font-size:30px;font-weight:800;color:var(--navy);padding:0 6px}\n.rc-head{margin:0;font-weight:600;color:var(--navy)}\n.rc-exp{background:#f7f9fc;border-radius:10px;padding:8px 12px;font-size:14px}.rc-exp small{display:block;color:var(--muted);font-size:11px}\n.rc-k{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-right:6px}\n.rc-goals{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:14px;font-weight:600}.rc-goals > div:last-child{text-align:right}\n.rc-goals small{display:block;color:var(--muted);font-weight:500;font-size:12px}\n.rc h5{margin:6px 0 2px;font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}\n.rc-who{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px;font-size:14px}\n.rc-who li{display:flex;align-items:center;gap:8px}.rc-who.up li span b{color:var(--green)}.rc-who.down li span b{color:var(--red)}\n.rc-notes{margin:0;padding-left:18px;font-size:14px;display:flex;flex-direction:column;gap:4px}\n.rc-notes li.up::marker{color:var(--green)}.rc-notes li.down::marker{color:var(--red)}\n.rc-notes li.av{list-style:none;margin-left:-18px;display:flex;gap:8px}.avi{width:18px;flex:none;text-align:center}\n.rc-mine{display:flex;flex-wrap:wrap;gap:6px}.rc-mine small{color:var(--muted);margin-left:2px}\n.rc-link{align-self:flex-start;color:var(--blue);font-weight:700;font-size:13px;text-decoration:none;margin-top:4px}\n@media (max-width:600px){.rcgrid{grid-template-columns:1fr}.rc{padding:14px}.rc-score b{font-size:14px}.rc-n{font-size:24px}}\n.mwdl{display:flex;flex-direction:column;gap:4px}.mwdl .pbar{height:22px}\n.mwdl-l{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);font-weight:600}\n.mwdl-s{font-size:12px;color:var(--muted)}.mwdl-s b{color:var(--navy)}.mwdl-s i{font-style:normal;color:var(--muted)}\n.mpos{white-space:nowrap;font-size:12px;font-weight:800;color:var(--navy);background:#eef1f6;border-radius:999px;padding:2px 9px}.mpos.top{background:var(--green-soft);color:var(--green)}.mpos.bot{background:var(--red-soft);color:var(--red)}\n@media (max-width:600px){.mpos{font-size:10px;padding:2px 6px}}\n.ct td small.dv{font-weight:800;font-size:11px;margin-top:2px}.ct td small.dv.pos{color:var(--green)}.ct td small.dv.neg{color:var(--red)}\n.ct tbody th,.ct thead th:first-child{position:sticky;left:0;z-index:2;background:#fff}\n.ct td.base,.ct th.base{background:var(--blue-soft);position:sticky;left:150px;z-index:1;box-shadow:2px 0 0 var(--line)}\n.ct td.base.best{background:#dff3e6}\n@media (max-width:600px){.ct td.base,.ct th.base{left:92px}}\n.ch .tag.you{display:inline-block;margin-bottom:3px;font-size:10px;padding:1px 7px}\n.cand thead tr.cand-me td{border-bottom:2px solid var(--blue);text-align:left;font-weight:400}\nfooter{color:var(--muted);font-size:13px;margin-top:60px;max-width:820px}
@media (max-width:600px){header h1{font-size:26px}.card{padding:18px}.tip{flex-direction:column;align-items:flex-start}}
"""


# ---------------------------------------------------------------- match predictions and tracking

PRED_PATH = Path(os.environ.get("PRED_PATH") or Path(__file__).with_name("predictions.json"))
ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ODDS_URL = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds/"

# FPL short club names -> names bookmakers use (fuzzy matching covers the rest)
ODDS_ALIASES = {
    "man city": "manchester city", "man utd": "manchester united", "spurs": "tottenham hotspur",
    "nott'm forest": "nottingham forest", "wolves": "wolverhampton wanderers", "brighton": "brighton and hove albion",
    "west ham": "west ham united", "newcastle": "newcastle united", "leeds": "leeds united",
    "bournemouth": "afc bournemouth", "coventry": "coventry city", "sheffield utd": "sheffield united",
    "ipswich": "ipswich town", "leicester": "leicester city", "luton": "luton town", "norwich": "norwich city",
    "west brom": "west bromwich albion", "stoke": "stoke city", "hull": "hull city", "swansea": "swansea city",
    "cardiff": "cardiff city",
}


def pois(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


DC_RHO = -0.13  # Dixon-Coles: plain Poisson underrates 0-0 and 1-1, this corrects it


def score_grid(lh, la, n=8):
    """Probability of every scoreline, with the Dixon-Coles low-score correction so draws
    get their real share instead of being systematically underpredicted."""
    g = {}
    for i in range(n):
        for j in range(n):
            v = pois(i, lh) * pois(j, la)
            if (i, j) == (0, 0):
                v *= 1 - lh * la * DC_RHO
            elif (i, j) == (0, 1):
                v *= 1 + lh * DC_RHO
            elif (i, j) == (1, 0):
                v *= 1 + la * DC_RHO
            elif (i, j) == (1, 1):
                v *= 1 - DC_RHO
            g[(i, j)] = max(v, 0.0)
    total = sum(g.values())
    return {k: v / total for k, v in g.items()}


def outcome_of(h, a):
    return "H" if h > a else "A" if a > h else "D"


def pick_from_lambdas(lh, la):
    """Most likely result (home win, draw or away win), then the most likely scoreline within it."""
    grid = score_grid(lh, la)
    probs = {"H": 0.0, "D": 0.0, "A": 0.0}
    for (i, j), v in grid.items():
        probs[outcome_of(i, j)] += v
    total = sum(probs.values())
    probs = {k: v / total for k, v in probs.items()}
    res = max(probs, key=probs.get)
    (h, a), _ = max(((k, v) for k, v in grid.items() if outcome_of(*k) == res), key=lambda kv: kv[1])
    return dict(h=h, a=a, res=res, pH=round(probs["H"], 3), pD=round(probs["D"], 3), pA=round(probs["A"], 3),
                lh=round(lh, 3), la=round(la, 3),
                pO=round(sum(v for (i, j), v in grid.items() if i + j > 2.5), 3),
                top=[[i, j, round(v, 3)] for (i, j), v in sorted(grid.items(), key=lambda kv: -kv[1])[:3]],
                cs_h=round(sum(v for (i, j), v in grid.items() if j == 0), 3),
                cs_a=round(sum(v for (i, j), v in grid.items() if i == 0), 3))


_FIT_VALUES = [round(0.10 + 0.05 * i, 2) for i in range(75)]  # 0.10 to 3.80 expected goals
_FIT_POIS = {lam: [pois(k, lam) for k in range(8)] for lam in _FIT_VALUES}


def _grid_summary(lh, la):
    ph, pa = _FIT_POIS[lh], _FIT_POIS[la]
    H = D = A = O = 0.0
    for i in range(8):
        for j in range(8):
            v = ph[i] * pa[j]
            if i == 0 and j == 0:
                v *= 1 - lh * la * DC_RHO
            elif i == 0 and j == 1:
                v *= 1 + lh * DC_RHO
            elif i == 1 and j == 0:
                v *= 1 + la * DC_RHO
            elif i == 1 and j == 1:
                v *= 1 - DC_RHO
            if i > j:
                H += v
            elif i == j:
                D += v
            else:
                A += v
            if i + j > 2.5:
                O += v
    t = H + D + A
    return H / t, D / t, A / t, O / t


def fit_lambdas(pH, pD, pA, pO=None):
    """Expected goals for each side that best reproduce the bookmakers' prices: the win/draw/loss
    odds and, when available, the over/under 2.5 goals odds (which fix how many goals to expect)."""
    best, err = (1.3, 1.1), 1e9
    for lh in _FIT_VALUES:
        for la in _FIT_VALUES:
            h, dr, a, o = _grid_summary(lh, la)
            e = (h - pH) ** 2 + (dr - pD) ** 2 + (a - pA) ** 2
            if pO is not None:
                e += (o - pO) ** 2
            if e < err:
                best, err = (lh, la), e
    return best


def norm_team(name):
    import re
    n = name.lower().replace("&", "and").strip()
    n = ODDS_ALIASES.get(n, n)
    return re.sub(r"\b(fc|afc)\b", "", n).strip()


def fetch_odds():
    """Bookmaker win/draw/win probabilities per match, vig removed and averaged across books."""
    if not ODDS_KEY:
        return []
    try:
        r = SESSION.get(ODDS_URL, params=dict(apiKey=ODDS_KEY, regions="uk", markets="h2h,totals",
                                              oddsFormat="decimal"), timeout=30)
        r.raise_for_status()
        events = r.json()
    except (requests.RequestException, ValueError) as err:
        print(f"Odds unavailable: {err}")
        return []
    out = []
    for ev in events:
        books, overs = [], []
        for bk in ev.get("bookmakers", []):
            for m in bk.get("markets", []):
                outcomes = [o for o in m.get("outcomes", []) if o.get("price")]
                if m.get("key") == "h2h":
                    pr = {o["name"]: 1 / o["price"] for o in outcomes}
                    h, a = pr.get(ev["home_team"]), pr.get(ev["away_team"])
                    dr = pr.get("Draw")
                    if h and a and dr:
                        s = h + a + dr
                        books.append((h / s, dr / s, a / s))
                elif m.get("key") == "totals":
                    pr = {o["name"]: 1 / o["price"] for o in outcomes if abs(num(o.get("point")) - 2.5) < 0.01}
                    if pr.get("Over") and pr.get("Under"):
                        overs.append(pr["Over"] / (pr["Over"] + pr["Under"]))
        if books:
            n = len(books)
            out.append(dict(home=norm_team(ev["home_team"]), away=norm_team(ev["away_team"]),
                            t=ev.get("commence_time"), n=n,
                            pH=sum(b[0] for b in books) / n, pD=sum(b[1] for b in books) / n,
                            pA=sum(b[2] for b in books) / n,
                            pO=sum(overs) / len(overs) if overs else None))
    return out


def match_odds(odds, home, away, kickoff):
    import difflib
    names = list({o["home"] for o in odds} | {o["away"] for o in odds})

    def resolve(team):
        n = norm_team(team["full"])
        if n in names:
            return n
        hit = difflib.get_close_matches(n, names, n=1, cutoff=0.6)
        return hit[0] if hit else None

    h, a = resolve(home), resolve(away)
    k = datetime.datetime.fromisoformat(kickoff.replace("Z", "+00:00")) if kickoff else None
    for o in odds:
        if o["home"] == h and o["away"] == a:
            if k and o["t"]:
                t = datetime.datetime.fromisoformat(o["t"].replace("Z", "+00:00"))
                if abs((t - k).total_seconds()) > 36 * 3600:
                    continue
            return o
    return None


def likely_scorers(team_id, lam, n_goals, players):
    """Split a team's expected goals across its available players by their scoring share."""
    pool = []
    for p in players.values():
        if p["tid"] != team_id or p["pos"] == "GK" or p["mins"] < 90:
            continue
        if p["status"] != "a" and (p["chance"] or 0) < 75:
            continue
        rate = (0.7 * p["xg"] + 0.3 * p["g"]) / p["mins"] * 90
        weight = rate * min(p["avg_mins"] / 90, 1.0) * max(p["start_rate"], 0.2)
        if weight > 0:
            pool.append((p, weight))
    total = sum(w for _, w in pool) or 1
    ranked = sorted(((p, lam * w / total) for p, w in pool), key=lambda x: -x[1])
    picks = [dict(code=p["code"], name=p["name"], xg=round(x, 3), p=round(1 - math.exp(-x), 3))
             for p, x in ranked[:max(n_goals, 0)]]
    top = [dict(code=p["code"], name=p["name"], p=round(1 - math.exp(-x), 3)) for p, x in ranked[:3]]
    return picks, top


def load_log():
    try:
        return json.loads(PRED_PATH.read_text())
    except (OSError, ValueError):
        return {"gws": {}}


def save_log(log):
    PRED_PATH.write_text(json.dumps(log, indent=1, sort_keys=True))


def update_predictions(d, teams, players):
    """Make or refresh predictions for unstarted matches, freeze them at kickoff, grade finished ones."""
    log = load_log()
    now = datetime.datetime.now(datetime.timezone.utc)
    games = matchups(d, teams)
    odds = fetch_odds() if games else []
    gw = str(d["next"])
    entry = log["gws"].setdefault(gw, {"matches": {}})
    fx_by_id = {f["id"]: f for f in d["fixtures"]}

    for g in games:
        f = next((f for f in d["fixtures"] if f.get("event") == d["next"]
                  and f["team_h"] == g["h"]["id"] and f["team_a"] == g["a"]["id"]), None)
        if not f:
            continue
        k = datetime.datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00")) if f.get("kickoff_time") else None
        if f.get("started") or (k and k <= now):
            continue  # frozen at kickoff
        model = pick_from_lambdas(g["lh"], g["la"])
        o = match_odds(odds, g["h"], g["a"], f.get("kickoff_time"))
        market = None
        if o:
            mlh, mla = fit_lambdas(o["pH"], o["pD"], o["pA"], o.get("pO"))
            market = pick_from_lambdas(mlh, mla)
            market.update(books=o["n"], oH=round(o["pH"], 3), oD=round(o["pD"], 3), oA=round(o["pA"], 3),
                          oO=round(o["pO"], 3) if o.get("pO") is not None else None)
        main = market or model
        sh, top_h = likely_scorers(g["h"]["id"], main["lh"], main["h"], players)
        sa, top_a = likely_scorers(g["a"]["id"], main["la"], main["a"], players)
        entry["matches"][str(f["id"])] = dict(
            fixture=f["id"], home=g["h"]["full"], away=g["a"]["full"], home_id=g["h"]["id"], away_id=g["a"]["id"],
            kickoff=f.get("kickoff_time"), updated=now.isoformat(timespec="minutes"), model=model, market=market,
            basis="market" if market else "model", scorers_h=sh, scorers_a=sa, top_h=top_h, top_a=top_a)

    # grade anything that has finished
    classic_codes = None
    for gwk, ent in log["gws"].items():
        for mid, m in ent["matches"].items():
            if m.get("result", {}).get("final"):
                continue  # graded with confirmed numbers; provisional results get refreshed
            f = fx_by_id.get(m["fixture"])
            if not f or not has_ended(f) or f.get("team_h_score") is None:
                continue
            if classic_codes is None and d.get("classic_boot"):
                classic_codes = {e["id"]: e["code"] for e in d["classic_boot"]["elements"]}
            if classic_codes is None:
                try:
                    classic_codes = {e["id"]: e["code"] for e in get(f"{CLASSIC}/bootstrap-static/")["elements"]}
                except requests.RequestException:
                    classic_codes = {}
            scorers = {"h": [], "a": []}
            for st in f.get("stats", []):
                if st.get("identifier") == "goals_scored":
                    for side in ("h", "a"):
                        for x in st.get(side, []):
                            scorers[side].append(dict(code=classic_codes.get(x["element"]), n=x.get("value", 1)))
            m["result"] = dict(h=f["team_h_score"], a=f["team_a_score"], scorers=scorers,
                               final=bool(f.get("finished")))
    save_log(log)
    return log


def grade(m, which):
    """Score one logged prediction against the real result."""
    p, r = m.get(which), m.get("result")
    if not p or not r:
        return None
    actual = outcome_of(r["h"], r["a"])
    picked = {x["code"] for x in m["scorers_h"] + m["scorers_a"]} if which == m["basis"] else set()
    scored = {x["code"] for x in r["scorers"]["h"] + r["scorers"]["a"] if x["code"]}
    cs_calls = [(p["a"] == 0, r["a"] == 0), (p["h"] == 0, r["h"] == 0)]
    return dict(result=p["res"] == actual, exact=(p["h"], p["a"]) == (r["h"], r["a"]),
                scorer_picks=len(picked), scorer_hits=len(picked & scored),
                cs_right=sum(1 for c, a in cs_calls if c == a), cs_total=len(cs_calls),
                cs_called=sum(1 for c, _ in cs_calls if c), cs_called_hit=sum(1 for c, a in cs_calls if c and a))


def predictions_section(log, players):
    names = {p["code"]: p for p in players.values() if p.get("code")}
    gws = sorted(log.get("gws", {}).items(), key=lambda kv: int(kv[0]))

    def tally(which):
        t = dict(n=0, result=0, exact=0, sp=0, sh=0, csr=0, cst=0)
        for _, ent in gws:
            for m in ent["matches"].values():
                gr = grade(m, which)
                if not gr:
                    continue
                t["n"] += 1
                t["result"] += gr["result"]
                t["exact"] += gr["exact"]
                t["sp"] += gr["scorer_picks"]
                t["sh"] += gr["scorer_hits"]
                t["csr"] += gr["cs_right"]
                t["cst"] += gr["cs_total"]
        return t

    def pct(a, b):
        return f"{a / b:.0%}" if b else "-"

    model_t, market_t = tally("model"), tally("market")
    scorer_t = dict(sp=model_t["sp"] + market_t["sp"], sh=model_t["sh"] + market_t["sh"])

    def card(label, t):
        return (f'<div class="sc"><h4>{label}</h4>'
                f'<div class="scg"><div><b>{pct(t["result"], t["n"])}</b><span>Results right</span></div>'
                f'<div><b>{pct(t["exact"], t["n"])}</b><span>Exact scores</span></div>'
                f'<div><b>{pct(t["csr"], t["cst"])}</b><span>Clean sheet calls</span></div>'
                f'<div><b>{t["n"]}</b><span>Matches graded</span></div></div></div>')

    scorecard = (f'<div class="scrow">{card("Stats model", model_t)}{card("Betting market", market_t)}'
                 f'<div class="sc"><h4>Goalscorer picks</h4><div class="scg">'
                 f'<div><b>{pct(scorer_t["sh"], scorer_t["sp"])}</b><span>Picks who scored</span></div>'
                 f'<div><b>{scorer_t["sh"]}/{scorer_t["sp"]}</b><span>Hits / picks</span></div></div></div></div>'
                 '<p class="note">Each match gets whichever result is most likely, draws included. For scale: always picking the home team gets about 45% of results right, '
                 'and exact scores land roughly 10% of the time even for good models.</p>')

    def scorer_list(xs):
        return ", ".join(f'{esc(x["name"])}' for x in xs) or "<span class='mut'>none</span>"

    def mrow(m, show_result):
        main = m[m["basis"]]
        res = m.get("result")
        cs = []
        if main["a"] == 0:
            cs.append(f'{esc(TEAMS.get(m["home_id"], {}).get("name", m["home"]))} {main["cs_h"]:.0%}')
        if main["h"] == 0:
            cs.append(f'{esc(TEAMS.get(m["away_id"], {}).get("name", m["away"]))} {main["cs_a"]:.0%}')
        alt = ""
        if m["market"] and m["model"]:
            mo = m["model"]
            alt = f'<span class="sub2">Stats model: {mo["h"]}–{mo["a"]}</span>'
        probs = (f'<div class="pbar"><span class="pb-h" style="width:{main["pH"]*100:.0f}%">{main["pH"]:.0%}</span>'
                 f'<span class="pb-d" style="width:{main["pD"]*100:.0f}%">{main["pD"]:.0%}</span>'
                 f'<span class="pb-a" style="width:{main["pA"]*100:.0f}%">{main["pA"]:.0%}</span></div>')
        actual = ""
        if show_result and res:
            gr = grade(m, m["basis"])
            act_names = [names.get(x["code"], {}).get("name", "?") + (f" ×{x['n']}" if x["n"] > 1 else "")
                         for x in res["scorers"]["h"] + res["scorers"]["a"]]
            ok = lambda b: '<span class="ok">✓</span>' if b else '<span class="no">✗</span>'
            actual = (f'<td><b>{res["h"]}–{res["a"]}</b> {ok(gr["result"])}'
                      f'{" <span class=ok>exact</span>" if gr["exact"] else ""}'
                      f'<span class="sub2">{esc(", ".join(act_names)) or "no scorers"}</span>'
                      f'<span class="sub2">Scorer picks: {gr["scorer_hits"]}/{gr["scorer_picks"]}</span></td>')
        elif show_result:
            actual = '<td><span class="mut">Not played yet</span></td>'
        src = "Odds" if m["basis"] == "market" else "Stats"
        return (f'<tr><td><div class="who">{badge(m["home_id"], 22)}<b>{esc(m["home"])}</b></div>'
                f'<div class="who">{badge(m["away_id"], 22)}<span>{esc(m["away"])}</span></div></td>'
                f'<td><span class="bignum">{main["h"]}–{main["a"]}</span><span class="sub2">{src}</span>{alt}</td>'
                f'<td style="min-width:190px">{probs}</td>'
                f'<td><span class="why">{"; ".join(cs) or "<span class=mut>none</span>"}</span></td>'
                f'<td><span class="why">{scorer_list(m["scorers_h"])}<br>{scorer_list(m["scorers_a"])}</span></td>'
                f'{actual}</tr>')

    head = ("<tr><th>Match</th><th>Predicted</th><th>Home · Draw · Away</th><th>Clean sheet</th>"
            "<th>Scorers (home / away)</th>{}</tr>")
    blocks = []
    for gwk, ent in reversed(gws):
        ms = sorted(ent["matches"].values(), key=lambda m: m.get("kickoff") or "")
        if not ms:
            continue
        done = sum(1 for m in ms if m.get("result"))
        rows = "".join(mrow(m, True) for m in ms)
        tbl = (f'<div class="tw"><table><thead>{head.format("<th>Actual</th>")}</thead>'
               f'<tbody>{rows}</tbody></table></div>')
        is_open = " open" if gwk == str(max(int(k) for k, _ in gws)) else ""
        blocks.append(f'<details class="pgw"{is_open}><summary>Gameweek {gwk} · {done} of {len(ms)} played</summary>'
                      f'{tbl}</details>')

    odds_note = ("Predictions use betting odds where available, with the stats model logged alongside for comparison."
                 if ODDS_KEY else
                 "Running on the stats model only. Add an Odds API key to the repo to log betting-market predictions too.")
    return f"""<section id="predictions"><h2>Prediction tracker</h2>
<p class="note">A season-long experiment: a predicted result, score, clean sheets and goalscorers for every match,
saved before kickoff and graded after full time. Predictions refresh with the latest team news until each match kicks
off, then they're locked. {odds_note}</p>
{scorecard}
{''.join(blocks) or '<p class="empty">The first predictions will appear on the next run.</p>'}</section>"""


# ---------------------------------------------------------------- me vs the league

def league_benchmark(d, players):
    """Average points per counted player, by position and gameweek, across opponents' lineups."""
    pos_of = {p["id"]: p["pos"] for p in players.values()}
    gws = sorted(d.get("live", {}))
    bench, mine = {}, {}
    for gw in gws:
        pts = d["live"].get(gw, {})
        opp = {k: [] for k in ("GK", "DEF", "MID", "FWD")}
        me = {k: [] for k in ("GK", "DEF", "MID", "FWD")}
        for (eid, g), xi in d.get("lineups", {}).items():
            if g != gw:
                continue
            target = me if eid == ENTRY_ID else opp
            for el in xi:
                pos = pos_of.get(el)
                if pos:
                    target[pos].append((el, pts.get(el, 0)))
        bench[gw] = {k: (sum(x for _, x in v) / len(v) if v else None, len(v)) for k, v in opp.items()}
        mine[gw] = me
    pooled = {}
    for pos in ("GK", "DEF", "MID", "FWD"):
        allv = []
        for (eid, g), xi in d.get("lineups", {}).items():
            if eid == ENTRY_ID:
                continue
            for el in xi:
                if pos_of.get(el) == pos:
                    allv.append(d["live"].get(g, {}).get(el, 0))
        pooled[pos] = sum(allv) / len(allv) if allv else 0.0
    return gws, bench, mine, pooled


def diff_chip(v, avg, label=None):
    if v is None:
        return '<span class="dchip dnp">–</span>'
    if avg is None:
        return f'<span class="dchip">{v}</span>'
    cls = "up" if v > avg else "down" if v < avg else ""
    title = f"{label + ': ' if label else ''}{v} pts vs league average {avg:.1f}"
    return f'<span class="dchip {cls}" title="{esc(title)}">{v}</span>'


def benchmark_section(d, players, squad):
    gws, bench, mine, pooled = league_benchmark(d, players)
    if not gws or not d.get("lineups"):
        return ""
    positions = ("GK", "DEF", "MID", "FWD")

    # 1) position averages each week: my counted players vs opponents' counted players
    head = "".join(f"<th>{p}</th>" for p in positions)
    body = ""
    wins = {p: 0 for p in positions}
    played_weeks = {p: 0 for p in positions}
    my_all = {p: [] for p in positions}
    for gw in gws:
        cells = ""
        for pos in positions:
            avg, n = bench[gw][pos]
            mv = [x for _, x in mine[gw][pos]]
            my_all[pos].extend(mv)
            my_avg = sum(mv) / len(mv) if mv else None
            if avg is None or my_avg is None:
                cells += (f'<td class="bx"><span class="mut">No players</span>'
                          f'<span class="bx-sub">league {avg:.1f}</span></td>' if avg is not None else "<td>-</td>")
                continue
            dlt = round(my_avg - avg, 1) + 0.0  # avoid showing -0.0
            played_weeks[pos] += 1
            if dlt > 0:
                wins[pos] += 1
            cls = "bx up" if dlt > 0 else "bx down" if dlt < 0 else "bx even"
            word = "Above" if dlt > 0 else "Below" if dlt < 0 else "Level"
            cells += (f'<td class="{cls}"><span class="bx-word">{word}</span><span class="bx-gap">{dlt:+.1f}</span>'
                      f'<span class="bx-sub">you {my_avg:.1f} · league {avg:.1f}</span></td>')
        body += f"<tr><td><b>GW{gw}</b></td>{cells}</tr>"

    season_cells, record_cells = "", ""
    for pos in positions:
        my_s = sum(my_all[pos]) / len(my_all[pos]) if my_all[pos] else None
        lg_s = pooled[pos]
        if my_s is None:
            season_cells += '<td class="bx"><span class="mut">-</span></td>'
        else:
            dlt = round(my_s - lg_s, 1) + 0.0
            cls = "bx up" if dlt > 0 else "bx down" if dlt < 0 else "bx even"
            season_cells += (f'<td class="{cls}"><span class="bx-gap">{dlt:+.1f}</span>'
                             f'<span class="bx-sub">you {my_s:.1f} · league {lg_s:.1f}</span></td>')
        w, t = wins[pos], played_weeks[pos]
        share = w / t if t else 0
        cls = "rec good" if share > 0.5 else "rec bad" if share < 0.5 else "rec even"
        verdict = "A strength" if share > 0.5 else "A weakness" if share < 0.5 else "Even"
        record_cells += (f'<td class="{cls}"><span class="rec-n">{w} of {t}</span>'
                         f'<span class="rec-v">{verdict}</span></td>')
    table1 = (f'<div class="tw"><table class="lgt bxt"><thead><tr><th>Gameweek</th>{head}</tr></thead>'
              f'<tbody>{body}'
              f'<tr class="sum"><td><b>Season avg</b></td>{season_cells}</tr>'
              f'<tr class="sum"><td><b>Weeks above</b></td>{record_cells}</tr>'
              f'</tbody></table></div>')

    # 2) my current players, week by week against the average at their position
    def per_week(p):
        out = []
        for gw in gws:
            avg = bench[gw][p["pos"]][0]
            v = d["live"].get(gw, {}).get(p["id"], 0)
            played = d.get("live_min", {}).get(gw, {}).get(p["id"], 0) > 0
            out.append((gw, v, avg, played))
        return out

    rows = []
    for p in squad:
        wk = per_week(p)
        total = sum(v for _, v, _, _ in wk)
        avg_pp = total / len(wk) if wk else 0
        bm = pooled[p["pos"]]
        above = sum(1 for _, v, a, _ in wk if a is not None and v > a)
        pl = [v for _, v, _, played in wk if played]
        avg_pl = sum(pl) / len(pl) if pl else None
        rows.append(dict(p=p, wk=wk, avg=avg_pp, bm=bm, gap=round(avg_pp - bm, 1) + 0.0, above=above, mine=True,
                         avg_pl=avg_pl, n_pl=len(pl),
                         gap_pl=(round(avg_pl - bm, 1) + 0.0) if avg_pl is not None else None))
    rows.sort(key=lambda r: -r["gap"])
    cols = [("Player", lambda r: who(r["p"])),
            ("Each gameweek", lambda r: '<div class="dchips">' + "".join(
                f'<div class="dcol"><small>GW{gw}</small>{diff_chip(v if pl else None, a, f"GW{gw}")}</div>' for gw, v, a, pl in r["wk"]) + "</div>"),
            ("Avg per GW", lambda r: big(f"{r['avg']:.1f}")),
            ("League avg", lambda r: f'<span class="mut">{r["bm"]:.1f}</span><span class="sub2">{r["p"]["pos"]}</span>'),
            ("Gap", lambda r: f'<span class="bignum {"pos" if r["gap"] > 0 else "neg" if r["gap"] < 0 else "mut"}">{r["gap"]:+.1f}</span>'),
            ("When he played", lambda r: "-" if r["avg_pl"] is None else
                f'{r["avg_pl"]:.1f} <b class="{"pos" if r["gap_pl"] > 0 else "neg" if r["gap_pl"] < 0 else "mut"}">'
                f'{r["gap_pl"]:+.1f}</b><span class="sub2">in {r["n_pl"]} of {len(r["wk"])} games</span>'),
            ("Weeks above", lambda r: f'{r["above"]}/{len(r["wk"])}')]
    table2 = table(rows, cols, False)

    # 3) free agents who beat the benchmark for their position
    fa_rows = []
    for p in players.values():
        if p["owner"] or p["status"] not in ("a", "d") or p["mins"] < 180:
            continue
        wk = per_week(p)
        if not wk:
            continue
        avg_pp = sum(v for _, v, _, _ in wk) / len(wk)
        last3 = [v for _, v, _, _ in wk[-3:]]
        l3 = sum(last3) / len(last3) if last3 else 0
        bm = pooled[p["pos"]]
        above = sum(1 for _, v, a, _ in wk if a is not None and v > a)
        fa_rows.append(dict(p=p, wk=wk, avg=avg_pp, l3=l3, bm=bm, gap=round(avg_pp - bm, 1) + 0.0, above=above))
    fa_rows.sort(key=lambda r: -r["gap"])
    fa_html = ""
    for pos, label in (("GK", "Goalkeepers"), ("DEF", "Defenders"), ("MID", "Midfielders"), ("FWD", "Forwards")):
        everyone = [r for r in fa_rows if r["p"]["pos"] == pos]
        rs = [r for r in everyone if r["gap"] > 0][:6]
        note = ""
        if not rs:
            # nobody beats the bar: say so and show the closest options instead of hiding the position
            rs = everyone[:4]
            note = ('<p class="note">No free agent at this position is beating the league average right now. '
                    'These are the closest options.</p>')
        if not rs:
            fa_html += (f'<h4>{label} <span class="mut">(league avg {pooled[pos]:.1f})</span></h4>'
                        '<p class="empty">No free agents with enough minutes to compare.</p>')
            continue
        fcols = [("Player", lambda r: who(r["p"])),
                 ("Each gameweek", cols[1][1]),
                 ("Avg per GW", lambda r: big(f"{r['avg']:.1f}")),
                 ("Last 3 GWs", lambda r: f'{r["l3"]:.1f}<span class="sub2">{"rising" if r["l3"] > r["avg"] + 1 else "falling" if r["l3"] < r["avg"] - 1 else "steady"}</span>'),
                 ("Gap", lambda r: f'<span class="bignum {"pos" if r["gap"] > 0 else "neg" if r["gap"] < 0 else "mut"}">{r["gap"]:+.1f}</span>'),
                 ("Weeks above", lambda r: f'{r["above"]}/{len(r["wk"])}'),
                 (f"Next {HORIZON}", lambda r: pill(next5(r["p"], TEAMS)[0]))]
        fa_html += f'<h4>{label} <span class="mut">(league avg {pooled[pos]:.1f})</span></h4>{note}{table(rs, fcols, False)}'

    return f"""<section id="vsleague"><h2>Me vs the league</h2>
<p class="note">The benchmark is the average points scored by players who actually counted for your opponents each
gameweek (their starting XI after automatic subs), split by position. Beating it means your player is out-earning
what the other Hope Bros managers are getting from that slot. Green = above the average, red = below.</p>
<div class="card"><h3>League average by position, each gameweek</h3>
<p class="note">Each box compares your counted players at that position with your opponents' counted players that
week. <b class="pos">Green</b> = you got more per player than the league, <b class="neg">red</b> = less. The bottom row
counts how many weeks you came out ahead, which shows where you're stronger than your competition and where you're weaker.</p>
{table1}</div>
<div class="card"><h3>Your players vs the average at their position</h3>
<p class="note">Every gameweek this season, including weeks before you owned him or when he was on your bench, so you're
judging the player, not your lineup choices. A dash means he didn't feature. "When he played" only counts games
he was on the pitch, which is the fairer read for anyone who's been injured. Sorted best to worst.</p>
{table2}</div>
<div class="card"><h3>Free agents beating the average</h3>
<p class="note">Unowned players whose average per gameweek is higher than what your opponents' players at that position average.</p>
{fa_html or '<p class="empty">No free agents are beating the league average right now.</p>'}</div>
</section>"""


# ---------------------------------------------------------------- interactive player comparison

def compare_data(d, players, teams):
    """Everything the in-page comparison tool needs, for every player, as compact JSON."""
    gws = sorted(d.get("live", {}))
    live, live_min = d.get("live", {}), d.get("live_min", {})
    _, _, _, pooled = league_benchmark(d, players)
    sp_code, sp_team = set_piece_orders(d)
    rows = []
    for p in players.values():
        wk = [[gw, live.get(gw, {}).get(p["id"], 0), live_min.get(gw, {}).get(p["id"], 0)] for gw in gws]
        if not any(m for _, _, m in wk) and not p["mine"] and p["mins"] == 0:
            continue  # never played and not yours: nothing to compare
        # predictions need a match log; fall back to weekly data for players without a fetched history
        q = p if p["log"] else dict(p, log=[dict(gw=g, pts=pt, mins=m) for g, pt, m in wk])
        preds = predict_points(q, teams)
        n5_label, _, _ = next5(p, teams)
        t = teams[p["tid"]]
        attack = p["pos"] in ("MID", "FWD")
        key = "def_rating" if attack else "att_rating"
        chips = [[x["gw"], teams[x["opp_id"]]["name"], 1 if x["home"] else 0,
                  round(teams[x["opp_id"]][key], 2)] for x in t["run"]]
        rs = match_ratings(p, teams) if p["log"] else []
        played = [pt for _, pt, m in wk if m > 0]
        last3 = [pt for _, pt, _ in wk[-3:]]
        rows.append(dict(
            id=p["id"], n=p["name"], t=t["name"], tf=t["full"], tid=p["tid"], pos=p["pos"], code=p.get("code"),
            own="You" if p["mine"] else (p["owner"] or "Free agent"), mine=p["mine"], fa=not p["owner"],
            st=p["status"], news=p["news"], ch=p["chance"],
            tot=p["total"], ppg=round(p["ppg"], 2), ppa=round(sum(played) / len(played), 2) if played else 0,
            mins=p["mins"], starts=p["starts"], gp=p["gp"], g=p["g"], a=p["a"], xg=round(p["xg"], 2),
            xa=round(p["xa"], 2), dc=round(p["dc_hit"], 2) if p["pos"] in ("DEF", "MID") else None,
            l3=round(sum(last3) / len(last3), 2) if last3 else 0, wk=wk,
            pr=[[x["gw"], round(x["pts"], 1)] for x in preds],
            r5=round(sum(r["pct"] for r in rs) / len(rs), 2) if rs else None, r5n=len(rs),
            n5=n5_label, ch5=chips, bm=round(pooled.get(p["pos"], 0), 2),
            sp=set_piece_info(p, sp_code, sp_team), ms=minutes_security(p, t, live_min)))
    played_t = [x for x in teams.values() if x["gp"]]
    avgs = {k: (sum(x[k] for x in played_t) / len(played_t) if played_t else 1.3) for k in ("def_rating", "att_rating")}
    return dict(players=rows, teams={tid: dict(n=t["name"], f=t["full"], c=t["code"]) for tid, t in teams.items()},
                avg=avgs, photo=PHOTO, badge=BADGE)


COMPARE_JS = r"""
(function(){
const D = JSON.parse(document.getElementById('cmp-data').textContent);
const P = {}; D.players.forEach(p => P[p.id] = p);
const MAX = 4;
let picks = [];
let anchor = null;   // the player of yours being replaced; stays in slot 1
const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ini = n => n.replace(/\./g,' ').split(/\s+/).slice(0,2).map(w=>w[0]||'').join('').toUpperCase();
const photo = (p, s) => `<span class="ph" style="--s:${s}px"><span class="ini">${esc(ini(p.n))}</span>${p.code ? `<img src="${D.photo.replace('{code}', p.code)}" alt="" onerror="this.remove()">` : ''}</span>`;
const badge = (tid, s) => { const t = D.teams[tid]; return t ? `<img class="badge" src="${D.badge.replace('{code}', t.c)}" width="${s}" height="${s}" alt="" onerror="this.style.visibility='hidden'">` : ''; };
const MSC = {'Nailed': 'm1', 'Secure': 'm2', 'Rotation risk': 'm3', 'Fringe': 'm4'};
const FIX = {'Easy':1.2,'Good':1.1,'Average':1,'Tough':0.9,'Very tough':0.8,'Blank':0.7};
const avail = p => (p.st === 'i' || p.st === 's' || p.st === 'u' || p.st === 'n') ? 0 : (p.ch == null ? 1 : p.ch / 100);

// one number per player for the verdict: recent form, season output, and the next-3 prediction
// (which already accounts for opponents, home/away and chance of playing)
function score(p){
  const next3 = p.pr.reduce((s, x) => s + x[1], 0) / Math.max(p.pr.length, 1);
  return 0.3 * p.l3 + 0.3 * p.ppg + 0.4 * next3;
}
const onCompare = () => !document.getElementById('compare').hidden;
function save(){
  if (!onCompare()) return;   // picks made from other tabs are remembered and written when you open Compare
  const h = picks.length ? '#compare=' + picks.join(',') + (anchor ? '&for=' + anchor : '') : '#compare';
  history.replaceState(null, '', h);
}
document.addEventListener('tabshown', e => { if (e.detail === 'compare') save(); });
function add(id){
  id = +id; if (!P[id] || picks.includes(id)) return;
  if (picks.length >= MAX){
    const i = picks.findIndex(x => x !== anchor);   // drop the oldest pick, never your player
    picks.splice(i, 1);
  }
  picks.push(id); save(); draw(); drawCands();
}
function remove(id){
  id = +id; picks = picks.filter(x => x !== id);
  if (id === anchor){ anchor = null; }
  save(); draw(); drawCands();
}
function toggle(id){ picks.includes(+id) ? remove(id) : add(id); }

// ---------- search
function results(){
  const q = $('#cmp-q').value.trim().toLowerCase();
  const pos = $('#cmp-pos').value, who = $('#cmp-who').value;
  let rs = D.players.filter(p => (!pos || p.pos === pos) && (who === 'all' || (who === 'fa' && p.fa) || (who === 'mine' && p.mine))
    && (!q || p.n.toLowerCase().includes(q) || p.tf.toLowerCase().includes(q) || p.t.toLowerCase().includes(q)));
  rs.sort((a, b) => b.ppg - a.ppg);
  rs = rs.slice(0, 8);
  $('#cmp-res').innerHTML = rs.length ? rs.map(p => `<button class="cmp-r" data-id="${p.id}">${photo(p, 30)}
     <span><b>${esc(p.n)}</b><small>${badge(p.tid, 12)}${esc(p.t)} · ${p.pos} · ${esc(p.own)}</small></span>
     <em>${p.ppg.toFixed(1)} pts/g</em></button>`).join('') : '<p class="empty">No players match.</p>';
}

// ---------- compare grid
function weekBars(p, top){
  const bars = p.wk.map(([gw, pts, m]) => {
    const h = m ? Math.max(4, pts / top * 46) : 3;
    return `<i class="${m ? (m >= 60 ? 'f' : 'p') : 'd'}" style="height:${h}px" title="GW${gw}: ${m ? pts + ' pts, ' + m + ' min' : 'did not play'}"></i>`;
  }).join('');
  const pr = p.pr.map(([gw, pts]) => `<i class="x" style="height:${Math.max(3, pts / top * 46)}px" title="GW${gw} predicted: ${pts} pts"></i>`).join('');
  return `<div class="cw">${bars}<span class="cmp-sep"></span>${pr}</div>`;
}
function chips(p){
  const attack = p.pos === 'MID' || p.pos === 'FWD';
  const avg = attack ? D.avg.def_rating : D.avg.att_rating;
  return p.ch5.map(([gw, opp, home, v]) => {
    const r = attack ? v / avg : avg / Math.max(v, 0.1);
    const c = r >= 1.12 ? 'good' : r <= 0.9 ? 'bad' : '';
    return `<span class="cc ${c}" title="GW${gw} vs ${esc(opp)} (${home ? 'home' : 'away'}): they ${attack ? 'concede' : 'score'} ${v.toFixed(1)}/game">${esc(opp)}<small>${home ? 'H' : 'A'}</small></span>`;
  }).join('') || '<span class="mut">No fixtures</span>';
}
// [label, display, value used to pick the best, difference vs your player: (p, base) => [number, decimals, unit]]
const ROWS = [
  ['Next 5 fixtures', p => `<span class="pill ${({'Easy':'e1','Good':'e2','Average':'e3','Tough':'e4','Very tough':'e5','Blank':'e5'})[p.n5] || 'e3'}">${p.n5}</span><div class="ccs">${chips(p)}</div>`, p => FIX[p.n5] || 1, null],
  ['Status', p => p.st === 'a' && (p.ch == null || p.ch >= 100) ? '<span class="pos">Available</span>' : `<span class="neg">${esc(p.news || 'Flagged')}</span>`, p => avail(p), null],
  ['Minutes security', p => { const m = p.ms; if (!m) return '<span class="mut">-</span>';
      return `<span class="ms ${MSC[m.label]}">${Math.round(m.pct * 100)}%</span> <span class="ms-l ${MSC[m.label]}">${m.label}</span>`
        + `<small>${m.mins} of ${m.poss} available min</small><small>Started ${m.starts} of ${m.games} available games</small>`
        + `<small>Last 3 available: ${Math.round(m.r3 * 100)}%</small>` + (m.out ? `<small>${m.out} game${m.out === 1 ? '' : 's'} excluded (flagged)</small>` : ''); },
    p => p.ms ? p.ms.pct + 0.5 * p.ms.r3 : -1,
    (p, b) => p.ms && b.ms ? [Math.round((p.ms.pct - b.ms.pct) * 100), 0, '%'] : null],
  ...[['pen', 'Penalties'], ['fk', 'Direct free kicks'], ['ck', 'Corners']].map(([k, label]) =>
    [`On ${label.toLowerCase()}?`, p => { const [y, note] = p.sp[k]; return y == null ? `<span class="mut">Unknown</span><small>${esc(note)}</small>` : `<span class="spyn ${y ? 'yes' : 'no'}">${y ? 'Yes' : 'No'}</span><small>${esc(note)}</small>`; },
     p => p.sp[k][0] == null ? 0 : p.sp[k][0], null]),
  ['Owner', p => p.mine ? '<span class="tag you">You</span>' : p.fa ? '<span class="tag fa">Free agent</span>' : `<span class="tag">${esc(p.own)}</span>`, null, null],
  ['Total points', p => p.tot, p => p.tot, (p, b) => [p.tot - b.tot, 0, 'pts']],
  ['Points per game', p => p.ppg.toFixed(1) + '<small>per team game</small>', p => p.ppg, (p, b) => [p.ppg - b.ppg, 1, '']],
  ['When he plays', p => p.ppa.toFixed(1) + '<small>per appearance</small>', p => p.ppa, (p, b) => [p.ppa - b.ppa, 1, '']],
  ['Last 3 gameweeks', p => p.l3.toFixed(1) + '<small>avg pts</small>', p => p.l3, (p, b) => [p.l3 - b.l3, 1, '']],
  ['Next 3 predicted', p => next3(p).toFixed(1) + '<small>' + p.pr.map(x => x[1]).join(' · ') + '</small>', p => next3(p), (p, b) => [next3(p) - next3(b), 1, 'pts']],
  ['vs league avg', p => { const g = p.ppg - p.bm; return `<span class="${g > 0 ? 'pos' : 'neg'}">${g >= 0 ? '+' : ''}${g.toFixed(1)}</span><small>${p.pos} avg ${p.bm.toFixed(1)}</small>`; }, p => p.ppg - p.bm, null],
  ['Goals + assists', p => `${p.g + p.a}<small>${p.g}G ${p.a}A · xGI ${(p.xg + p.xa).toFixed(1)}</small>`, p => p.g + p.a, (p, b) => [(p.g + p.a) - (b.g + b.a), 0, '']],
  ['Match rating', p => p.r5 == null ? '<span class="mut">-</span>' : (p.r5 >= 0.5 ? 'Top ' + Math.max(1, Math.round((1 - p.r5) * 100)) + '%' : 'Bottom ' + Math.max(1, Math.round(p.r5 * 100)) + '%') + `<small>last ${p.r5n}, vs ${p.pos}</small>`, p => p.r5 == null ? -1 : p.r5, null],
  ['Defensive contribution', p => p.dc == null ? '<span class="mut">-</span>' : Math.round(p.dc * 100) + '%<small>of games hit bonus</small>', p => p.dc == null ? -1 : p.dc, (p, b) => p.dc == null || b.dc == null ? null : [Math.round((p.dc - b.dc) * 100), 0, '%']],
];
function vsMine(p, base, dfn){
  if (!base || !dfn || p.id === base.id) return '';
  const r = dfn(p, base); if (!r) return '';
  const [v, dp, unit] = r;
  if (Math.abs(v) < (dp ? 0.05 : 0.5)) return `<small class="dv">same as ${esc(base.n)}</small>`;
  return `<small class="dv ${v > 0 ? 'pos' : 'neg'}">${v > 0 ? '+' : ''}${v.toFixed(dp)}${unit ? ' ' + unit : ''} vs ${esc(base.n)}</small>`;
}

function verdict(ps){
  if (ps.length < 2) return '';
  const sc = ps.map(p => ({p, s: score(p) * (0.5 + 0.5 * avail(p))}));
  sc.sort((a, b) => b.s - a.s);
  const best = sc[0].p;
  // when one of yours is in the comparison, measure the winner against him: he's the one you'd drop
  const yours = ps.find(p => p.mine && p.id !== best.id);
  const second = yours || sc[1].p;
  const why = [];
  const d1 = best.ppg - second.ppg; if (d1 >= 0.5) why.push(`scores ${d1.toFixed(1)} more points per game than ${esc(second.n)}`);
  const d2 = best.l3 - second.l3; if (d2 >= 1) why.push(`is in better recent form (${best.l3.toFixed(1)} vs ${second.l3.toFixed(1)} over the last 3)`);
  const pa = best.pr.reduce((s, x) => s + x[1], 0), pb = second.pr.reduce((s, x) => s + x[1], 0);
  if (pa - pb >= 1) why.push(`is predicted ${(pa - pb).toFixed(1)} more points over the next 3`);
  if ((FIX[best.n5] || 1) > (FIX[second.n5] || 1)) why.push(`has easier fixtures (${best.n5} vs ${second.n5})`);
  const sa = best.gp ? best.starts / best.gp : 0, sb = second.gp ? second.starts / second.gp : 0;
  if (sa - sb >= 0.2) why.push(`starts more often (${best.starts} of ${best.gp} vs ${second.starts} of ${second.gp})`);
  const spb = [['pen', 'penalties'], ['fk', 'direct free kicks'], ['ck', 'corners']].filter(([k]) => best.sp[k][0] === 1 && second.sp[k][0] !== 1).map(x => x[1]);
  if (spb.length) why.push(`is on ${spb.join(' and ')} and ${esc(second.n)} isn't`);
  if (avail(second) < 1 && avail(best) >= avail(second)) why.push(`${esc(second.n)} is flagged: ${esc(second.news || 'doubtful')}`);
  const sSecond = sc.find(x => x.p.id === second.id).s;
  const margin = (sc[0].s - sSecond) / Math.max(sSecond, 0.5);
  const strength = margin > 0.25 ? 'Clear edge' : margin > 0.08 ? 'Edge' : 'Too close to call, slight lean';
  let action = '';
  const mine = ps.find(p => p.mine);
  if (mine && best.id !== mine.id) action = best.fa ? ` Worth a waiver claim: drop ${esc(mine.n)} for ${esc(best.n)}.` :
      !best.mine ? ` ${esc(best.n)} is owned by ${esc(best.own)}, so a trade would be the way.` : '';
  if (mine && best.id === mine.id) action = ` Keep ${esc(mine.n)}.`;
  return `<div class="cv"><span class="cv-k">${strength}</span><b>${esc(best.n)}</b>
    <p>${why.length ? esc(best.n) + ' ' + why.join(', ') + '.' : 'The numbers are very close across the board.'}${action}</p></div>`;
}

function draw(){
  let ps = picks.map(id => P[id]).filter(Boolean);
  // your player (the one being replaced, or any of yours) always sits in the first column
  const base = ps.find(p => p.id === anchor) || ps.find(p => p.mine) || null;
  if (base) ps = [base, ...ps.filter(p => p.id !== base.id)];
  $('#cmp-count').textContent = ps.length ? `${ps.length} of ${MAX} selected` : 'Pick up to 4 players';
  if (!ps.length){ $('#cmp-grid').innerHTML = '<p class="empty">Search for a player above, tap "Find replacements" on one of yours, or use the Compare button next to any player in the report.</p>'; $('#cmp-verdict').innerHTML=''; return; }
  const top = Math.max(10, ...ps.flatMap(p => p.wk.map(w => w[1]).concat(p.pr.map(x => x[1]))));
  const head = ps.map(p => `<th class="${base && p.id === base.id ? 'base' : ''}"><div class="ch">${photo(p, 52)}<div>${base && p.id === base.id ? '<span class="tag you">Your player</span>' : ''}<b>${esc(p.n)}</b><small>${badge(p.tid, 14)}${esc(p.tf)} · ${p.pos}</small></div>
      <button class="cx" data-id="${p.id}" title="Remove">×</button></div>${weekBars(p, top)}</th>`).join('');
  const body = ROWS.map(([label, fmt, val, dfn]) => {
    let bestV = null;
    if (val && ps.length > 1){ const vs = ps.map(val); const mx = Math.max(...vs); if (vs.filter(v => v === mx).length < vs.length) bestV = mx; }
    return `<tr><th>${label}</th>${ps.map(p => `<td class="${bestV != null && val(p) === bestV ? 'best' : ''}${base && p.id === base.id ? ' base' : ''}">${fmt(p)}${vsMine(p, base, dfn)}</td>`).join('')}</tr>`;
  }).join('');
  const minW = (innerWidth < 600 ? 92 : 150) + ps.length * (innerWidth < 600 ? 140 : 190);
  $('#cmp-grid').innerHTML = `<div class="tw"><table class="ct" style="min-width:${minW}px"><thead><tr><th class="ct-l">
      <span class="lgx"><i class="f"></i>60+ <i class="p"></i>sub <i class="d"></i>DNP <i class="x"></i>predicted</span></th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  $('#cmp-verdict').innerHTML = verdict(ps);
}

function replacements(id){
  const me = P[+id]; if (!me) return;
  if (!onCompare() && window.showTab) window.showTab('compare');
  anchor = me.id; picks = [me.id];
  $('#cand-q').value = ''; $('#cand-sort').value = 'score'; $('#cand-mins').checked = true;
  save(); draw(); drawCands();
  document.getElementById('cmp-cands').scrollIntoView({behavior: 'smooth', block: 'start'});
}

const next3 = p => p.pr.reduce((s, x) => s + x[1], 0);
function diff(v, base, dp){
  if (base == null || v == null) return '';
  const d = v - base; if (Math.abs(d) < 0.05) return '<small class="mut">same</small>';
  return `<small class="${d > 0 ? 'pos' : 'neg'}">${d > 0 ? '+' : ''}${d.toFixed(dp)}</small>`;
}
const SORTS = {
  score: [(a, b) => score(b) - score(a), 'Best overall'],
  ppg: [(a, b) => b.ppg - a.ppg, 'Points per game'],
  l3: [(a, b) => b.l3 - a.l3, 'Last 3 gameweeks'],
  n3: [(a, b) => next3(b) - next3(a), 'Next 3 predicted'],
  fix: [(a, b) => (FIX[b.n5] || 1) - (FIX[a.n5] || 1) || score(b) - score(a), 'Easiest fixtures'],
  starts: [(a, b) => (b.ms ? b.ms.pct : 0) - (a.ms ? a.ms.pct : 0) || b.ppg - a.ppg, 'Most secure minutes'],
};
function drawCands(){
  const box = $('#cmp-cands');
  const me = anchor ? P[anchor] : null;
  if (!me){ box.innerHTML = ''; return; }
  const q = $('#cand-q').value.trim().toLowerCase();
  const onlyPlaying = $('#cand-mins').checked;
  const all = D.players.filter(p => p.pos === me.pos && p.fa);
  let rs = all.filter(p => (!onlyPlaying || p.mins > 0) && (!q || p.n.toLowerCase().includes(q) || p.tf.toLowerCase().includes(q)));
  rs.sort(SORTS[$('#cand-sort').value][0]);
  const posWord = {GK:'goalkeepers', DEF:'defenders', MID:'midfielders', FWD:'forwards'}[me.pos];
  const pill = n => `<span class="pill ${({'Easy':'e1','Good':'e2','Average':'e3','Tough':'e4','Very tough':'e5','Blank':'e5'})[n] || 'e3'}">${n}</span>`;
  const rating = p => p.r5 == null ? '<span class="mut">-</span>' : (p.r5 >= 0.5 ? 'Top ' + Math.max(1, Math.round((1 - p.r5) * 100)) + '%' : 'Bottom ' + Math.max(1, Math.round(p.r5 * 100)) + '%');
  const status = p => (p.st === 'a' && (p.ch == null || p.ch >= 100)) ? '' : `<span class="cand-flag" title="${esc(p.news)}">${esc(p.news ? p.news.split(' - ')[0] : 'Flagged')}${p.ch != null ? ' · ' + p.ch + '%' : ''}</span>`;
  const spTag = p => { const on = [['pen', 'Pens'], ['fk', 'FKs'], ['ck', 'Corners']].filter(([k]) => p.sp[k][0] === 1).map(x => x[1]);
    return on.length ? `<span class="sp-tag" title="First-choice set-piece taker">On ${on.join(' · ')}</span>` : ''; };
  const row = (p, mine) => {
    const on = picks.includes(p.id);
    return `<tr class="${mine ? 'cand-me' : on ? 'cand-on' : ''}">
      <td><div class="who">${photo(p, 36)}<div><div class="nm">${esc(p.n)}${mine ? ' <span class="tag you">Your player</span>' : ''}</div>
        <div class="tm">${badge(p.tid, 14)}${esc(p.t)} ${status(p)} ${spTag(p)}</div>
        ${mine ? '' : `<button class="cand-btn cand-btn-m ${on ? 'on' : ''}" data-id="${p.id}">${on ? '✓ Comparing' : '+ Compare'}</button>`}</div></div></td>
      <td><b>${p.ppg.toFixed(1)}</b>${mine ? '' : diff(p.ppg, me.ppg, 1)}</td>
      <td><b>${p.l3.toFixed(1)}</b>${mine ? '' : diff(p.l3, me.l3, 1)}</td>
      <td><b>${next3(p).toFixed(1)}</b>${mine ? '' : diff(next3(p), next3(me), 1)}</td>
      <td>${p.ms ? `<b>${Math.round(p.ms.pct * 100)}%</b><small class="ms-l ${MSC[p.ms.label]}">${p.ms.label}</small>` : '-'}${mine || !p.ms || !me.ms ? '' : diff(Math.round(p.ms.pct * 100), Math.round(me.ms.pct * 100), 0)}</td>
      <td>${rating(p)}</td>
      <td>${pill(p.n5)}</td>
      <td class="cand-act">${mine ? '<span class="mut">Slot 1</span>' : `<button class="cand-btn ${on ? 'on' : ''}" data-id="${p.id}">${on ? '✓ Comparing' : '+ Compare'}</button>`}</td></tr>`;
  };
  const slots = MAX - 1 - picks.filter(x => x !== anchor).length;
  box.innerHTML = `<div class="card">
    <div class="cand-head"><div><h3>Free agent ${posWord} who could replace ${esc(me.n)}</h3>
      <p class="note" style="margin:0">${rs.length} of ${all.length} shown. Green/red numbers are better/worse than ${esc(me.n)}.
      Pick up to ${MAX - 1} to compare side by side below${slots < MAX - 1 ? ` (${slots} slot${slots === 1 ? '' : 's'} left; picking more swaps out the oldest)` : ''}.</p></div>
      <button id="cand-close" class="sb">Close</button></div>
    <div class="cmp-row cand-tools"><input id="cand-q2" type="search" placeholder="Filter by name or club" value="${esc($('#cand-q').value)}">
      <label class="cand-sortl">Sort by <select id="cand-sort2">${Object.entries(SORTS).map(([k, v]) => `<option value="${k}" ${k === $('#cand-sort').value ? 'selected' : ''}>${v[1]}</option>`).join('')}</select></label>
      <label class="cand-chk"><input id="cand-mins2" type="checkbox" ${onlyPlaying ? 'checked' : ''}> Only players with minutes</label></div>
    <div class="tw cand-tw"><table class="cand"><thead><tr><th>Player</th><th>Pts/game</th><th>Last 3</th><th>Next 3 pred.</th>
      <th title="Minutes played out of minutes available (games while injured or suspended don't count)">Mins secure</th><th>Rating</th><th>Next 5</th><th class="cand-act"></th></tr>${row(me, true)}</thead>
      <tbody>${rs.map(p => row(p, false)).join('') || '<tr><td colspan="8" class="empty">No free agents match.</td></tr>'}</tbody></table></div></div>`;
  // keep the hidden master controls in sync with the visible ones
  $('#cand-q2').addEventListener('input', e => { $('#cand-q').value = e.target.value; const pos = e.target.selectionStart; drawCands(); const el = $('#cand-q2'); el.focus(); el.setSelectionRange(pos, pos); });
  $('#cand-sort2').addEventListener('change', e => { $('#cand-sort').value = e.target.value; drawCands(); });
  $('#cand-mins2').addEventListener('change', e => { $('#cand-mins').checked = e.target.checked; drawCands(); });
  $('#cand-close').addEventListener('click', () => { anchor = null; save(); drawCands(); });
}

// ---------- wiring
$('#cmp-q').addEventListener('input', results);
$('#cmp-pos').addEventListener('change', results);
$('#cmp-who').addEventListener('change', results);
$('#cmp-res').addEventListener('click', e => { const b = e.target.closest('.cmp-r'); if (b){ add(b.dataset.id); } });
$('#cmp-grid').addEventListener('click', e => { const b = e.target.closest('.cx'); if (b) remove(b.dataset.id); });
$('#cmp-cands').addEventListener('click', e => { const b = e.target.closest('.cand-btn'); if (b) toggle(b.dataset.id); });
document.getElementById('cmp-toast').addEventListener('click', () => { if (window.showTab) window.showTab('compare'); });
$('#cmp-clear').addEventListener('click', () => { picks = []; anchor = null; save(); draw(); drawCands(); });
$('#cmp-mine').innerHTML = D.players.filter(p => p.mine).sort((a, b) => ['GK','DEF','MID','FWD'].indexOf(a.pos) - ['GK','DEF','MID','FWD'].indexOf(b.pos))
  .map(p => `<option value="${p.id}">${esc(p.n)} (${p.pos})</option>`).join('');
$('#cmp-repl').addEventListener('click', () => replacements($('#cmp-mine').value));
document.addEventListener('click', e => {
  const b = e.target.closest('.cmp-add'); if (!b || b.classList.contains('cmp-repl-btn')) return;
  e.preventDefault(); add(b.dataset.id);
  const n = P[+b.dataset.id];
  b.textContent = '✓ Added'; setTimeout(() => b.textContent = '+ Compare', 1500);
  const t = document.getElementById('cmp-toast');
  t.textContent = `${n ? n.n : 'Player'} added to Compare (${picks.length}/${MAX})` + (onCompare() ? '' : ' · Tap to view');
  t.classList.toggle('go', !onCompare());
  t.classList.add('on'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), 2200);
});
document.addEventListener('click', e => { const b = e.target.closest('.cmp-repl-btn'); if (b){ e.preventDefault(); replacements(b.dataset.id); } });
function fromHash(){
  const m = location.hash.match(/compare=([\d,]+)/);
  if (!m) return false;
  picks = m[1].split(',').map(Number).filter(id => P[id]).slice(0, MAX);
  const fm = location.hash.match(/for=(\d+)/);
  anchor = fm && P[+fm[1]] && picks.includes(+fm[1]) ? +fm[1] : null;
  return true;
}
fromHash();
// popstate fires before hashchange; read the new link first so nothing writes the old one back
['popstate', 'hashchange'].forEach(ev => window.addEventListener(ev, () => { if (fromHash()){ draw(); drawCands(); } }));
results(); draw(); drawCands();
})();
"""


def compare_section(d, players, teams):
    data = json.dumps(compare_data(d, players, teams), separators=(",", ":")).replace("</", "<\\/")
    return f"""<section id="compare"><h2>Compare players</h2>
<p class="note">Line up to 4 players side by side before a waiver claim or trade. Search any Premier League player, or pick one
of yours and tap Find replacements to see every free agent at his position and choose who to put next to him. Green = best in that row.
You can also tap <b>+ Compare</b> next to any player elsewhere in the report. Your comparison is saved in the page
address, so you can bookmark or share it. Data is from this morning's build; to refresh mid-day, run the workflow on GitHub.</p>
<div class="card cmp-tools">
<div class="cmp-row"><label>Find replacements for</label><select id="cmp-mine"></select>
<button id="cmp-repl" class="sb on">Find replacements</button></div>
<div class="cmp-row"><input id="cmp-q" type="search" placeholder="Search a player or club" autocomplete="off">
<select id="cmp-pos"><option value="">All positions</option><option>GK</option><option>DEF</option><option>MID</option><option>FWD</option></select>
<select id="cmp-who"><option value="all">Everyone</option><option value="fa">Free agents only</option><option value="mine">My players</option></select></div>
<div id="cmp-res" class="cmp-res"></div></div>
<div id="cmp-cands"></div>
<div hidden><input id="cand-q"><select id="cand-sort"><option value="score">score</option><option value="ppg"></option>
<option value="l3"></option><option value="n3"></option><option value="fix"></option><option value="starts"></option></select>
<input id="cand-mins" type="checkbox" checked></div>
<div class="cmp-bar"><span id="cmp-count"></span><button id="cmp-clear" class="sb">Clear</button></div>
<div id="cmp-verdict"></div>
<div id="cmp-grid"></div>
<script type="application/json" id="cmp-data">{data}</script>
<script>{COMPARE_JS}</script></section>"""


TABS_JS = r"""
(function(){
const secs = [...document.querySelectorAll('main > section[id]')];
const links = [...document.querySelectorAll('nav a[href^="#"]')];
const header = document.querySelector('header');
if (!secs.length) return;
const idFrom = h => { const id = h.replace(/^#/, '').split(/[=&]/)[0]; return secs.some(s => s.id === id) ? id : secs[0].id; };
function show(id, scroll){
  secs.forEach(s => s.hidden = s.id !== id);
  links.forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#' + id));
  const on = links.find(a => a.classList.contains('on'));
  if (on) on.scrollIntoView({block: 'nearest', inline: 'center'});
  if (scroll) window.scrollTo({top: header ? header.offsetHeight : 0});
  document.dispatchEvent(new CustomEvent('tabshown', {detail: id}));
}
window.showTab = function(id){
  if (idFrom(location.hash) !== id || !location.hash) history.pushState(null, '', '#' + id);
  show(id, true);
};
links.forEach(a => a.addEventListener('click', e => { e.preventDefault(); window.showTab(a.getAttribute('href').slice(1)); }));
window.addEventListener('popstate', () => show(idFrom(location.hash), true));
window.addEventListener('hashchange', () => show(idFrom(location.hash), true));
show(idFrom(location.hash), false);
})();
"""


# ---------------------------------------------------------------- last gameweek recaps

NOTABLE_RANK = 120  # players inside FPL's preseason top-N count as "key players" for availability news


def parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def exp_goals(teams, hid, aid):
    """Expected goals for each side from team ratings (same method as the matchup cards)."""
    played = [t for t in teams.values() if t["gp"]]
    if not played:
        return 1.3, 1.1
    lg_att = sum(t["att_rating"] for t in played) / len(played)
    lg_def = sum(t["def_rating"] for t in played) / len(played)
    h, a = teams[hid], teams[aid]
    lh = lg_att * (h["att_rating"] / lg_att) * (a["def_rating"] / lg_def) * HOME_BOOST
    la = lg_att * (a["att_rating"] / lg_att) * (h["def_rating"] / lg_def) / HOME_BOOST
    return lh, la


def teams_as_of(d, gw):
    """Team ratings using only results from before gameweek `gw`, for an honest pre-match expectation."""
    fx = []
    for f in d["fixtures"]:
        if (f.get("event") or 0) >= gw:
            f = dict(f, finished=False, finished_provisional=False, team_h_score=None, team_a_score=None)
        fx.append(f)
    t, _, _ = build_teams(dict(d, fixtures=fx, next=gw))
    return t


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def team_form_note(t, upto):
    """Form read for a team using results up to and including kickoff `upto`."""
    res = [r for r in t["results"] if r.get("kickoff", "") <= upto] if upto else t["results"]
    out = ["W" if r["gf"] > r["ga"] else "D" if r["gf"] == r["ga"] else "L" for r in res]
    if len(out) < 3:
        return None
    pts = lambda xs: sum(3 if x == "W" else 1 if x == "D" else 0 for x in xs)
    last3, prev3 = out[-3:], out[-6:-3]
    run = 1
    while run < len(out) and out[-1 - run] == out[-1]:
        run += 1
    unbeaten = 0
    for x in reversed(out):
        if x == "L":
            break
        unbeaten += 1
    winless = 0
    for x in reversed(out):
        if x == "W":
            break
        winless += 1
    name = t["full"]
    if out[-1] == "W" and run >= 3:
        return ("up", f"{name} have won {run} in a row.")
    if out[-1] == "L" and run >= 3:
        return ("down", f"{name} have lost {run} in a row.")
    if prev3 and pts(last3) >= 7 and pts(prev3) <= 4:
        return ("up", f"{name} are coming into form: {plural(pts(last3), 'point')} from their last 3 after {pts(prev3)} from the 3 before.")
    if prev3 and pts(last3) <= 2 and pts(prev3) >= 5:
        return ("down", f"{name} are going off the boil: {plural(pts(last3), 'point')} from their last 3 after {pts(prev3)} from the 3 before.")
    if unbeaten >= 4:
        return ("up", f"{name} are unbeaten in {unbeaten}.")
    if winless >= 4:
        return ("down", f"{name} are without a win in {winless}.")
    return None


def recap_match(f, d, players, teams, teams_pre, log, gw, roster):
    hid, aid = f["team_h"], f["team_a"]
    H, A = teams[hid], teams[aid]
    ended = has_ended(f) and f.get("team_h_score") is not None
    hs, as_ = f.get("team_h_score"), f.get("team_a_score")
    kickoff = parse_ts(f.get("kickoff_time"))
    side_of = {hid: "h", aid: "a"}

    # who played in this match, with their stats for it
    ps = [(players[el], st) for el, (st, fids) in d.get("recap_live", {}).items()
          if f["id"] in fids and el in players and players[el]["tid"] in side_of and st.get("minutes", 0) > 0]

    # ---- pre-match expectation: bookmakers if they were logged, otherwise the stats model as of kickoff
    logged = log.get("gws", {}).get(str(gw), {}).get("matches", {}).get(str(f["id"])) if log else None
    if logged and logged.get("market"):
        ex, src = logged["market"], "Bookmakers"
    elif logged and logged.get("model"):
        ex, src = logged["model"], "Stats model (logged before kickoff)"
    else:
        lh, la = exp_goals(teams_pre, hid, aid)
        ex, src = pick_from_lambdas(lh, la), "Stats model, using only results before this gameweek"
    fav = max((("H", ex["pH"]), ("D", ex["pD"]), ("A", ex["pA"])), key=lambda x: x[1])
    fav_txt = {"H": f"{H['full']} win", "D": "a draw", "A": f"{A['full']} win"}[fav[0]]

    out = dict(f=f, H=H, A=A, ended=ended, hs=hs, as_=as_, src=src, ex=ex, fav_txt=fav_txt, fav_p=fav[1])
    if not ended:
        return out

    actual = outcome_of(hs, as_)
    p_actual = {"H": ex["pH"], "D": ex["pD"], "A": ex["pA"]}[actual]
    if actual == fav[0]:
        verdict = ("ok", "Went as expected")
    elif p_actual < 0.25:
        verdict = ("upset", "Upset")
    else:
        verdict = ("surprise", "Surprise result")
    if hs > as_:
        headline = f"{H['full']} beat {A['full']} {hs}–{as_} at home."
    elif as_ > hs:
        headline = f"{A['full']} won {as_}–{hs} away at {H['full']}."
    else:
        headline = f"{H['full']} and {A['full']} drew {hs}–{as_}."
    out.update(actual=actual, p_actual=p_actual, verdict=verdict, headline=headline)

    # ---- xG: did the result reflect the chances?
    xg = {"h": 0.0, "a": 0.0}
    for p, st in ps:
        xg[side_of[p["tid"]]] += num(st.get("expected_goals"))
    out["xg"] = xg
    gd, xgd = hs - as_, xg["h"] - xg["a"]
    if abs(xgd) < 0.5:
        xg_txt = f"Little to separate them on chances (xG {xg['h']:.1f}–{xg['a']:.1f})."
    else:
        better = H["full"] if xgd > 0 else A["full"]
        if (xgd > 0 and gd > 0) or (xgd < 0 and gd < 0):
            xg_txt = f"{better} deserved it, creating the better chances (xG {xg['h']:.1f}–{xg['a']:.1f})."
        elif gd == 0:
            xg_txt = f"{better} should have won it on chances (xG {xg['h']:.1f}–{xg['a']:.1f})."
        else:
            xg_txt = f"{better} created more and can feel hard done by (xG {xg['h']:.1f}–{xg['a']:.1f})."
    out["xg_txt"] = xg_txt

    # ---- goals and assists
    def names(key, side):
        rows = []
        for p, st in ps:
            n = st.get(key, 0)
            if n and side_of[p["tid"]] == side:
                rows.append(p["name"] + (f" ×{n}" if n > 1 else ""))
        return rows
    out["goals"] = {s: names("goals_scored", s) for s in ("h", "a")}
    out["assists"] = {s: names("assists", s) for s in ("h", "a")}
    out["own_goals"] = [(p, side_of[p["tid"]]) for p, st in ps if st.get("own_goals")]

    # ---- standouts (bonus points) and better / worse than expected
    def why(p, st):
        bits = []
        if st.get("goals_scored"):
            bits.append(f"{st['goals_scored']} goal{'s' if st['goals_scored'] > 1 else ''}")
        if st.get("assists"):
            bits.append(f"{st['assists']} assist{'s' if st['assists'] > 1 else ''}")
        if st.get("clean_sheets") and p["pos"] in ("GK", "DEF"):
            bits.append("clean sheet")
        if st.get("saves", 0) >= 4:
            bits.append(f"{st['saves']} saves")
        if st.get("penalties_saved"):
            bits.append("penalty save")
        if not bits and st.get("defensive_contribution", 0) >= (10 if p["pos"] == "DEF" else 12):
            bits.append(f"{st['defensive_contribution']} defensive actions")
        return ", ".join(bits)
    stars = sorted(((p, st) for p, st in ps if st.get("bonus")), key=lambda x: (-x[1]["bonus"], -x[1].get("bps", 0)))
    out["stars"] = [dict(p=p, pts=st.get("total_points", 0), bonus=st["bonus"], why=why(p, st)) for p, st in stars[:3]]

    star_ids = {s["p"]["id"] for s in out["stars"]}
    better, worse = [], []
    for p, st in ps:
        g, xgp, pts, mins = st.get("goals_scored", 0), num(st.get("expected_goals")), st.get("total_points", 0), st.get("minutes", 0)
        prior = (p["total"] - pts) / max(p["gp"] - 1, 1)
        if g and xgp / g < 0.2:
            better.append((p, f"scored from very little ({xgp:.2f} xG)"))
        elif pts >= 8 and prior < 3.5 and p["id"] not in star_ids:
            better.append((p, f"{pts} points, well above his usual {prior:.1f} a game"))
        # ranked: the more telling the miss, the higher it sorts
        if st.get("penalties_missed"):
            worse.append((0, p, "missed a penalty"))
        if xgp >= 0.7 and not g:
            worse.append((1, p, f"couldn't convert big chances ({xgp:.1f} xG, no goal)"))
        elif mins >= 60 and pts <= 2 and (p["draft_rank"] <= 40 or prior >= 6):
            worse.append((2 + (0 if p["draft_rank"] <= 40 else 1), p,
                          f"blanked with {pts} point{'s' if pts != 1 else ''} (usually {prior:.1f} a game)"))
        if st.get("own_goals"):
            worse.append((1, p, "scored an own goal"))
    worse.sort(key=lambda x: (x[0], -x[1]["ppg"]))
    seen = set()
    worse = [(p, t) for _, p, t in worse if not (p["id"] in seen or seen.add(p["id"]))]
    out["better"], out["worse"] = better[:2], worse[:2]

    # ---- team form after this result
    upto = f.get("kickoff_time") or ""
    out["form"] = [n for n in (team_form_note(H, upto), team_form_note(A, upto)) if n]

    # ---- availability: returns, new injuries, suspensions, absences
    avail = []
    gws_before = sorted(g for g in d.get("live_min", {}) if g < gw)
    for p, st in ps:
        if st.get("red_cards"):
            nx = " Suspended for the next game." if "suspend" in (p["news"] or "").lower() else \
                 " A straight red usually means a 3-match ban; a second yellow, 1 match."
            avail.append(("red", p, f"{p['name']} was sent off.{nx}"))
        missed = 0
        for g in reversed(gws_before):
            if d["live_min"].get(g, {}).get(p["id"], 0) == 0:
                missed += 1
            else:
                break
        if missed >= 2 and (p["draft_rank"] <= NOTABLE_RANK or p["starts"] >= 3):
            avail.append(("back", p, f"{p['name']} returned after missing {missed} games and played {st['minutes']} minutes."))
    on_pitch = {p["id"] for p, _ in ps}
    for p in players.values():
        if p["tid"] not in side_of:
            continue
        news = p["news"] or ""
        added = parse_ts(p.get("news_added"))
        notable = p["draft_rank"] <= NOTABLE_RANK or p["starts"] >= 3
        if not news or not notable:
            continue
        low = news.lower()
        if any(k in low for k in ("joined", "loan", "transferred", "departed", "left the club")):
            continue
        new_since = added and kickoff and added >= kickoff
        if "suspend" in low and new_since and not any(x[1]["id"] == p["id"] and x[0] == "red" for x in avail):
            avail.append(("susp", p, f"{p['name']} is now suspended: {news}."))
        elif p["status"] in ("i", "d") and new_since and p["id"] in on_pitch:
            avail.append(("inj", p, f"{p['name']} picked up a knock: {news}."))
        elif p["status"] in ("i", "d", "s") and p["id"] not in on_pitch and p["starts"] >= 2 and not new_since:
            avail.append(("out", p, f"{p['name']} was missing: {news}."))
    order = {"red": 0, "susp": 1, "inj": 2, "back": 3, "out": 4}
    avail.sort(key=lambda x: (order[x[0]], x[1]["draft_rank"]))
    per_kind, kept = {}, []
    for k, pl, t in avail:  # at most 2 of each kind, most important players first
        if per_kind.get(k, 0) < 2:
            kept.append((k, pl, t))
            per_kind[k] = per_kind.get(k, 0) + 1
    out["avail"] = kept[:6]

    out["mine"] = [(p, st.get("total_points", 0), st.get("minutes", 0)) for p, st in ps if p["id"] in roster]
    q = f"{H['full']} vs {A['full']} {kickoff:%B %Y} match report" if kickoff else f"{H['full']} vs {A['full']} match report"
    out["report"] = "https://www.google.com/search?q=" + requests.utils.quote(q)
    return out


def recap_section(d, players, teams, log):
    gw = d["current"]
    fx = sorted((f for f in d["fixtures"] if f.get("event") == gw), key=lambda f: f.get("kickoff_time") or "")
    if not fx:
        return ""
    teams_pre = teams_as_of(d, gw)
    roster = {p["element"] for p in d["picks"]}
    recs = [recap_match(f, d, players, teams, teams_pre, log, gw, roster) for f in fx]
    done = [r for r in recs if r["ended"]]

    # gameweek at a glance
    goals = sum(r["hs"] + r["as_"] for r in done)
    upsets = [r for r in done if r["verdict"][0] == "upset"]
    as_exp = sum(1 for r in done if r["verdict"][0] == "ok")
    reds = sum(1 for r in done for a in r["avail"] if a[0] == "red")
    big = max(done, key=lambda r: abs(r["hs"] - r["as_"]), default=None)
    glance = (f'<div class="glance"><div><b>{len(done)}/{len(recs)}</b><span>Matches played</span></div>'
              f'<div><b>{goals}</b><span>Goals ({goals / max(len(done), 1):.1f} a game)</span></div>'
              f'<div><b>{as_exp}/{len(done)}</b><span>Went as expected</span></div>'
              f'<div><b>{len(upsets)}</b><span>Upsets</span></div>'
              f'<div><b>{reds}</b><span>Red cards</span></div></div>')
    if big and abs(big["hs"] - big["as_"]) >= 3:
        glance += f'<p class="note">Biggest win: {esc(big["headline"])}</p>'

    def plist(xs, empty="None"):
        return ", ".join(esc(x) for x in xs) if xs else f'<span class="mut">{empty}</span>'

    def who_line(items):
        return "".join(f'<li>{photo(p, 26)}<span><b>{esc(p["name"])}</b> ({esc(TEAMS[p["tid"]]["name"])}) {esc(t)}</span></li>'
                       for p, t in items)

    cards = []
    for r in recs:
        H, A, f = r["H"], r["A"], r["f"]
        ko = parse_ts(f.get("kickoff_time"))
        when = ko.astimezone(ZoneInfo("America/New_York")).strftime("%a %b %-d, %-I:%M %p ET") if ko else ""
        ex = r["ex"]
        exp_html = (f'<div class="rc-exp"><span class="rc-k">Expected</span>{esc(r["fav_txt"])} ({r["fav_p"]:.0%}), '
                    f'most likely {ex["h"]}–{ex["a"]}<small>{esc(r["src"])}</small></div>')
        if not r["ended"]:
            cards.append(f'<div class="rc"><div class="rc-when">{when}</div>'
                         f'<div class="rc-score">{badge(H["id"], 40)}<b>{esc(H["full"])}</b><span class="mut">v</span>'
                         f'<b>{esc(A["full"])}</b>{badge(A["id"], 40)}</div>{exp_html}'
                         f'<p class="empty">Not finished yet. The recap appears once it has been played.</p></div>')
            continue
        vcls, vtxt = r["verdict"]
        sections = []
        if r["stars"]:
            sections.append('<h5>Standout performers</h5><ul class="rc-who">' + "".join(
                f'<li>{photo(s["p"], 26)}<span><b>{esc(s["p"]["name"])}</b> ({esc(TEAMS[s["p"]["tid"]]["name"])}) '
                f'{s["pts"]} pts, +{s["bonus"]} bonus{": " + esc(s["why"]) if s["why"] else ""}</span></li>' for s in r["stars"]) + '</ul>')
        if r["better"]:
            sections.append(f'<h5>Better than expected</h5><ul class="rc-who up">{who_line(r["better"])}</ul>')
        if r["worse"]:
            sections.append(f'<h5>Worse than expected</h5><ul class="rc-who down">{who_line(r["worse"])}</ul>')
        if r["form"]:
            sections.append('<h5>Form</h5><ul class="rc-notes">' + "".join(
                f'<li class="{k}">{esc(t)}</li>' for k, t in r["form"]) + '</ul>')
        if r["avail"]:
            icon = {"red": "🟥", "susp": "⛔", "inj": "🩹", "back": "↩", "out": "✖"}
            sections.append('<h5>Team news</h5><ul class="rc-notes">' + "".join(
                f'<li class="av {k}"><span class="avi">{icon[k]}</span>{esc(t)}</li>' for k, p, t in r["avail"]) + '</ul>')
        if r["mine"]:
            sections.append('<h5>Your players</h5><div class="rc-mine">' + "".join(
                f'<span class="mp">{photo(p, 26)}{esc(p["name"])} <b>{pts} pts</b><small>{mins}\'</small></span>'
                for p, pts, mins in r["mine"]) + '</div>')
        cards.append(f'''<div class="rc">
<div class="rc-top"><span class="rc-when">{when}</span><span class="rc-v {vcls}">{vtxt}</span></div>
<div class="rc-score">{badge(H["id"], 40)}<b>{esc(H["full"])}</b><span class="rc-n">{r["hs"]}–{r["as_"]}</span><b>{esc(A["full"])}</b>{badge(A["id"], 40)}</div>
<p class="rc-head">{esc(r["headline"])} {esc(r["xg_txt"])}</p>
{exp_html}
<div class="rc-goals"><div><span class="rc-k">Goals</span>{plist(r["goals"]["h"], "-")}<small>Assists: {plist(r["assists"]["h"], "none")}</small></div>
<div><span class="rc-k">Goals</span>{plist(r["goals"]["a"], "-")}<small>Assists: {plist(r["assists"]["a"], "none")}</small></div></div>
{''.join(sections)}
<a class="rc-link" href="{r["report"]}" target="_blank" rel="noopener">Find match reports ↗</a></div>''')

    exp_note = ("Expected results come from bookmaker odds saved before kickoff where available."
                if any(r["src"] == "Bookmakers" for r in recs) else
                "Expected results come from the stats model using only what was known before the gameweek "
                "(bookmaker odds are used from the weeks the Prediction tracker logged them).")
    return f"""<section id="recap"><h2>Gameweek {gw} review</h2>
<p class="note">A recap of every match: result vs expectation, whether the scoreline matched the chances, who scored,
who stood out or disappointed, team form, and injury, return and suspension news. {exp_note} Player and match data
from the Premier League's official FPL feed; injury and suspension news is as of this morning.</p>
{glance}
<div class="rcgrid">{''.join(cards)}</div></section>"""


def render(d, players, teams, log=None):
    TEAMS.clear()
    TEAMS.update(teams)
    squad = [players[pk["element"]] for pk in sorted(d["picks"], key=lambda x: x["position"])]
    for i, p in enumerate(squad):
        p["slot"] = "XI" if i < 11 else f"Bench {i - 10}"
    e = d["entry"]
    rows, n_teams = season_data(d)
    place = f"{ordinal(rows[-1]['place'])} of {n_teams}" if rows else "-"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>Boofla Report</title><style>{CSS}</style></head><body>
<header><div class="in">
<h1>{esc(e['name'])} · Gameweek {d['next']}</h1>
<p>Updated {datetime.datetime.now(datetime.timezone.utc):%A %b %d, %H:%M} UTC</p>
<div class="stats">
<div class="stat"><b>{e.get('overall_points', '?')}</b><span>Total points</span></div>
<div class="stat"><b>{e.get('event_points', '?')}</b><span>Last gameweek</span></div>
<div class="stat"><b>{place}</b><span>Hope Bros position</span></div>
</div></div></header>
<nav><div class="in"><span class="ngrp">Review</span><a href="#season">Season</a><a href="#recap">Last gameweek</a><a href="#vsleague">Me vs the league</a><a href="#finishing">xG vs actual</a><span class="ngrp">Plan</span><a href="#squad">My squad</a><a href="#teams">Teams &amp; matchups</a><a href="#compare">Compare</a><a href="#market">Free agents</a><a href="#predictions">Predictions</a></div></nav>
<div id="cmp-toast" class="toast"></div>
<main>

{season_section(d)}

{recap_section(d, players, teams, log)}

{benchmark_section(d, players, squad)}

<section id="finishing"><h2>xG and xA vs actual returns</h2>
<p class="note">+/- is actual minus expected. Positive = finishing above their chances, negative = due for more.</p>
{finishing_section(players)}</section>

{squad_review_section(squad, players, teams, d)}

{teams_section(d, teams, players, log)}

{compare_section(d, players, teams)}

<section id="market"><h2>Best free agents</h2>
<p class="note">Regular starters nobody in Hope Bros owns, ranked by points per game this season, with how good their
next {HORIZON} matchups look.</p>
{market_section(players, teams)}</section>

{predictions_section(log, players) if log is not None else ""}

<footer>Matchup ratings use each team's goals scored and conceded per game, blending the full season with their last 5
matches. Data from the FPL Draft and FPL APIs. Photos and badges from the Premier League.</footer>
</main><script>{TABS_JS}</script></body></html>"""


def main():
    try:
        d = load_data()
    except requests.RequestException as err:
        sys.exit(f"Could not reach the FPL API: {err}")
    teams, _, _ = build_teams(d)
    players = build_players(d, teams)
    rank_and_tag(players, [pk["element"] for pk in d["picks"]])
    TEAMS.clear()
    TEAMS.update(teams)
    log = update_predictions(d, teams, players)
    out = Path(os.environ.get("REPORT_PATH") or Path(__file__).with_name("fpl_report.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d, players, teams, log), encoding="utf-8")
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
