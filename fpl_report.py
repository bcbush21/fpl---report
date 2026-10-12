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

    # Per-match history for the players we care about: my squad plus every
    # DEF/MID with real minutes (needed for defensive contribution hit rates).
    mine = {p["element"] for p in d["picks"]}
    wanted = [e["id"] for e in boot["elements"]
              if e["id"] in mine or (e["element_type"] in (2, 3) and e.get("minutes", 0) >= 180)]

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
    return d

# ---------------------------------------------------------------- helpers

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

    done = [f for f in d["fixtures"] if f.get("finished") and f.get("team_h_score") is not None]
    for f in sorted(done, key=lambda f: f.get("kickoff_time") or ""):
        h, a = teams[f["team_h"]], teams[f["team_a"]]
        hs, as_ = f["team_h_score"], f["team_a_score"]
        h["gp"] += 1; a["gp"] += 1
        h["gf"] += hs; h["ga"] += as_
        a["gf"] += as_; a["ga"] += hs
        h["results"].append(dict(gf=hs, ga=as_, opp=f["team_a"], home=True))
        a["results"].append(dict(gf=as_, ga=hs, opp=f["team_h"], home=False))

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
        season_att = (t["xg_pg"] + t["gf_pg"]) / 2
        season_def = (t["xgc90"] + t["ga_pg"]) / 2
        if t["n5"]:
            t["att_rating"] = 0.6 * season_att + 0.4 * t["gf5"] / t["n5"]
            t["def_rating"] = 0.6 * season_def + 0.4 * t["ga5"] / t["n5"]
        else:
            t["att_rating"], t["def_rating"] = season_att, season_def

    played = [t for t in teams.values() if t["gp"]]
    lg_xg = sum(t["xg_pg"] for t in played) / len(played) if played else 1.3
    lg_xgc = sum(t["xgc90"] for t in played) / len(played) if played else 1.3

    # next HORIZON gameweeks of fixtures for each team
    last_gw = d["next"] + HORIZON - 1
    for t in teams.values():
        t["run"] = []
    for f in sorted(d["fixtures"], key=lambda f: (f.get("event") or 99, f.get("kickoff_time") or "")):
        ev = f.get("event")
        if not ev or not (d["next"] <= ev <= last_gw) or f.get("finished"):
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

def build_players(d, teams):
    names = {}
    for e in d["details"].get("league_entries", []):
        label = e.get("entry_name") or e.get("player_first_name", "?")
        names[e.get("entry_id")] = label
        names[e.get("id")] = label
    owner = {s["element"]: names.get(s.get("owner")) for s in d["status"] if s.get("owner")}
    my_name = d["entry"]["name"]
    mine = {p["element"] for p in d["picks"]}

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
        players[el["id"]] = p
    return players


def project(p, teams, lg_xgc):
    """Expected FPL points per upcoming fixture, built from the pieces Ben cares about."""
    if p["status"] in ("i", "s", "u", "n") or p["mins"] < 90:
        p["proj_next"], p["proj_run"], p["proj_parts"] = 0.0, 0.0, {}
        return
    t = teams[p["tid"]]
    play = p["start_rate"]
    if p["chance"] is not None and p["chance"] < 100:
        play *= p["chance"] / 100
    long_game = min(p["avg_mins"] / 90, 1.0)

    def one(fx):
        att = fx["att"]
        lam = t["xgc90"] * fx["dfn"]          # goals we expect to concede
        p_cs = math.exp(-lam)
        parts = dict(
            appear=2.0,
            goals=p["xg90"] * long_game * att * GOAL_PTS[p["pos"]],
            assists=p["xa90"] * long_game * att * 3,
            cs=p_cs * CS_PTS[p["pos"]] * (p["full90_rate"] if p["pos"] in ("GK", "DEF") else 1),
            dc=p["dc_hit"] * 2,
            conceded=-(lam / 2) if p["pos"] in ("GK", "DEF") else 0.0,
            saves=p["saves90"] / 3 if p["pos"] == "GK" else 0.0,
            bonus=p["bonus90"],
        )
        return {k: v * play for k, v in parts.items()}, p_cs

    runs = [one(fx) for fx in t["run"]]
    p["proj_run"] = round(sum(sum(r[0].values()) for r in runs), 1)
    if runs:
        first_gw = t["run"][0]["gw"]
        nxt = [r for r, fx in zip(runs, t["run"]) if fx["gw"] == first_gw]
        p["proj_next"] = round(sum(sum(r[0].values()) for r in nxt), 1)
        p["proj_parts"] = runs[0][0]
        p["cs_next"] = runs[0][1]
    else:
        p["proj_next"], p["proj_parts"], p["cs_next"] = 0.0, {}, 0.0
    p["cs_avg"] = sum(r[1] for r in runs) / len(runs) if runs else 0.0


def rank_and_tag(players, squad_ids):
    """Rank everyone within their position, then tag Core players who should be protected."""
    for pos in POS.values():
        group = sorted((p for p in players.values() if p["pos"] == pos and p["mins"] >= 270),
                       key=lambda p: p["proj_run"], reverse=True)
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


def flags(p, teams):
    out = []
    if p.get("core"):
        out.append("Core: keep" if not p.get("kept") else "On your keep list")
    if p["status"] != "a" or (p["chance"] is not None and p["chance"] < 100):
        out.append(f"{STATUS.get(p['status'], p['status'])}: {p['news'] or 'no detail'}")
    if p["gp"] and p["start_rate"] < 0.6 and p["pos"] != "GK":
        out.append(f"Rotation risk ({p['starts']}/{p['gp']} starts)")
    t = teams[p["tid"]]
    ease = t["def_ease"] if p["pos"] in ("GK", "DEF") else t["att_ease"]
    lab = ease_label(ease)
    if lab in ("Tough", "Very tough", "Blank"):
        out.append(f"{lab} next {HORIZON}")
    elif lab == "Easy":
        out.append(f"Easy next {HORIZON}")
    v = finishing_verdict(p)
    if v.startswith(("Due", "Running")):
        out.append(v)
    if p["pos"] in ("DEF", "MID") and p["dc_hit"] >= 0.5:
        out.append(f"DC banker ({p['dc_hit']:.0%})")
    return out

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


def team_table_section(teams):
    rows = [t for t in teams.values()]
    body = []
    for t in rows:
        gd_cls = "pos" if t["gd"] > 0 else "neg" if t["gd"] < 0 else "mut"
        body.append(
            f'<tr data-gd="{t["gd"]}" data-gf="{t["gf"]}" data-ga="{t["ga"]}" data-name="{esc(t["full"])}">'
            f'<td class="rk"></td><td>{team_cell(t)}</td><td>{t["gp"]}</td>'
            f'<td><span class="bignum">{t["gf"]}</span><span class="sub2">xG {t["xg_pg"]:.1f}/g</span></td>'
            f'<td><span class="bignum">{t["ga"]}</span><span class="sub2">xGA {t["xgc90"]:.1f}/g</span></td>'
            f'<td><span class="bignum {gd_cls}">{t["gd"]:+d}</span></td>'
            f'<td><div class="fms">{form_chips(t)}</div>'
            f'<span class="sub2">{t["gf5"]} scored, {t["ga5"]} conceded</span></td></tr>')
    return f"""<div class="card"><h3>Premier League: goals for and against</h3>
<p class="note">Tap a button to re-rank. Small grey numbers are expected goals per game, which show whether
the real totals are backed up by the chances created and allowed. Last 5 reads oldest to newest.</p>
<div class="sortbar"><span class="note" style="margin:0">Rank by:</span>
<button class="sb on" data-k="gd" data-dir="desc">Goal difference</button>
<button class="sb" data-k="gf" data-dir="desc">Most goals scored</button>
<button class="sb" data-k="ga" data-dir="asc">Fewest goals conceded</button>
<button class="sb" data-k="ga" data-dir="desc">Most goals conceded</button></div>
<div class="tw"><table class="tt" id="tt"><thead><tr><th>#</th><th>Team</th><th>P</th><th>Scored</th><th>Conceded</th>
<th>GD</th><th>Last 5</th></tr></thead><tbody>{''.join(body)}</tbody></table></div></div>
<script>
(function(){{var tb=document.querySelector('#tt tbody');if(!tb)return;
function sort(k,dir){{var rows=[].slice.call(tb.rows);
rows.sort(function(a,b){{var d=(+a.dataset[k])-(+b.dataset[k]);if(dir==='desc')d=-d;
if(d===0)d=(+b.dataset.gd)-(+a.dataset.gd);if(d===0)d=a.dataset.name.localeCompare(b.dataset.name);return d;}});
rows.forEach(function(r,i){{r.cells[0].textContent=i+1;tb.appendChild(r);}});}}
document.querySelectorAll('.sb').forEach(function(b){{b.addEventListener('click',function(){{
document.querySelectorAll('.sb').forEach(function(x){{x.classList.toggle('on',x===b);}});sort(b.dataset.k,b.dataset.dir);}});}});
sort('gd','desc');}})();
</script>"""


def matchups(d, teams):
    played = [t for t in teams.values() if t["gp"]]
    if not played:
        return []
    lg_att = sum(t["att_rating"] for t in played) / len(played)
    lg_def = sum(t["def_rating"] for t in played) / len(played)
    games = []
    for f in sorted((f for f in d["fixtures"] if f.get("event") == d["next"] and not f.get("finished")),
                    key=lambda f: f.get("kickoff_time") or ""):
        h, a = teams[f["team_h"]], teams[f["team_a"]]
        lam_h = lg_att * (h["att_rating"] / lg_att) * (a["def_rating"] / lg_def) * HOME_BOOST
        lam_a = lg_att * (a["att_rating"] / lg_att) * (h["def_rating"] / lg_def) / HOME_BOOST
        games.append(dict(h=h, a=a, lh=lam_h, la=lam_a, cs_h=math.exp(-lam_a), cs_a=math.exp(-lam_h),
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


def matchup_section(d, teams, players):
    games = matchups(d, teams)
    if not games:
        return ""
    mine_by_team = {}
    for p in players.values():
        if p["mine"]:
            mine_by_team.setdefault(p["tid"], []).append(p)

    cards = []
    for g in games:
        h, a = g["h"], g["a"]
        ko = ""
        if g["kickoff"]:
            try:
                dt = datetime.datetime.fromisoformat(g["kickoff"].replace("Z", "+00:00"))
                et = dt.astimezone(ZoneInfo("America/New_York"))
                ko = et.strftime("%a %b %d, %-I:%M %p ET")
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
            return (f'<div class="ms {align}">{badge(t["id"], 48)}<div class="mn">{esc(t["full"])}</div>'
                    f'<div class="fms">{form_chips(t)}</div>'
                    f'<div class="sub2">{cs:.0%} clean sheet</div></div>')

        cards.append((not g["flags"], f'''<div class="mc{hot}">
<div class="mko">{ko}</div>
<div class="mrow">{side(h, g["lh"], g["cs_h"], "l")}
<div class="mscore"><span>{g["lh"]:.1f}</span><i>–</i><span>{g["la"]:.1f}</span><small>projected goals</small></div>
{side(a, g["la"], g["cs_a"], "r")}</div>{flags}{mine_html}</div>'''))

    flagged = sum(1 for g in games if g["flags"])
    return f"""<div class="card"><h3>Gameweek {d['next']} matchups</h3>
<p class="note">Projected goals come from each side's attack rating against the other's defense rating, adjusted
for home advantage. Ratings blend the full season (60%) with the last 5 matches (40%), so recent form counts.
{flagged} of {len(games)} games are flagged: <b class="g">Goals likely</b> = strong attack vs leaky defense,
<b class="b">Clean sheet spot</b> = tight defense vs weak attack. Flagged games are listed first.</p>
<div class="mgrid">{''.join(c for _, c in sorted(cards, key=lambda x: x[0]))}</div></div>"""


def teams_section(d, teams, players):
    return (f'<section id="teams"><h2>Premier League teams and this week\'s matchups</h2>'
            f'{team_table_section(teams)}{matchup_section(d, teams, players)}</section>')


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


# ---------------------------------------------------------------- advice

def tip(kind, title, body, *people):
    return dict(kind=kind, title=title, body=body, people=[p for p in people if p])


def advice(squad, players, teams):
    tips = []
    xi, bench = squad[:11], squad[11:]

    for p in xi:
        if p["status"] in ("i", "s", "u", "n") or (p["chance"] is not None and p["chance"] <= 25):
            why = p["news"] or (f"{p['chance']}% chance of playing" if p["chance"] is not None
                                else STATUS.get(p["status"]))
            tips.append(tip("bench", f"Bench {p['name']}", why, p))

    for b in bench:
        same = [p for p in xi if p["pos"] == b["pos"]]
        if same:
            worst = min(same, key=lambda p: p["proj_next"])
            if b["proj_next"] > worst["proj_next"] + 0.8:
                tips.append(tip("start", f"Start {b['name']} over {worst['name']}",
                                f"Projected {b['proj_next']} pts next gameweek vs {worst['proj_next']}.",
                                b, worst))

    # only free agents with a real sample of minutes, so one cameo doesn't look like a star
    free = [p for p in players.values() if not p["owner"] and p["proj_run"] > 0
            and p["mins"] >= 270 and p["start_rate"] >= 0.5]
    for pos in ("GK", "DEF", "MID", "FWD"):
        mine = [p for p in squad if p["pos"] == pos and not p["core"]]
        if not mine:
            continue
        weakest = min(mine, key=lambda p: p["proj_run"])
        best = max((p for p in free if p["pos"] == pos), key=lambda p: p["proj_run"], default=None)
        if best and best["proj_run"] > weakest["proj_run"] + 2.5:
            why = []
            if pos in ("DEF", "MID") and best["dc_hit"] >= 0.4:
                why.append(f"hits defensive contribution in {best['dc_hit']:.0%} of games")
            why.append(f"{ease_label(teams[best['tid']]['def_ease' if pos in ('GK', 'DEF') else 'att_ease']).lower()} fixtures")
            if finishing_verdict(best).startswith("Due"):
                why.append(f"{best['g'] + best['a']} G+A from {best['xgi']:.1f} xGI, so returns are due")
            tips.append(tip("waiver", f"Pick up {best['name']}, drop {weakest['name']}",
                            f"Projected {best['proj_run']} pts over the next {HORIZON} vs {weakest['proj_run']}. "
                            f"Why: {', '.join(why)}.", best, weakest))

    for p in squad:
        ga = p["g"] + p["a"]
        if p["gi_diff"] >= 1.5 and p["pos_rank"]:
            if p["core"] and p["xgi"] >= 1.0:
                tips.append(tip("keep", f"Keep {p['name']}",
                                f"{ga} G+A from {p['xgi']:.1f} xGI, so some cooling is likely, but his underlying "
                                f"numbers still rank {rank_str(p)}. He's a core piece.", p))
            elif not p["core"] and p["pos_pct"] > 0.5:
                # hot streak propping up below-average underlying numbers: the real sell-high case
                tips.append(tip("sell", f"Sell-high: {p['name']}",
                                f"{ga} G+A from only {p['xgi']:.1f} xGI, and his underlying numbers rank "
                                f"{rank_str(p)}. If someone offers a better-rated player off his points, take it.", p))
        if finishing_verdict(p).startswith("Due") and p["start_rate"] >= 0.7:
            tips.append(tip("hold", f"Hold {p['name']}",
                            f"{ga} G+A from {p['xgi']:.1f} xGI ({rank_str(p)}). The chances are there, "
                            "returns should follow.", p))

    return tips

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
    return (f'<div class="who">{photo(p)}<div><div class="nm">{esc(p["name"])}{star}</div>'
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


def run_chips(t, side):
    chips = []
    for x in t["run"]:
        ha = "H" if x["home"] else "A"
        val = x["o_xgc"] if side == "att" else x["o_xg"]
        good = x["att"] >= 1.05 if side == "att" else x["dfn"] <= 0.95
        bad = x["att"] <= 0.9 if side == "att" else x["dfn"] >= 1.1
        cls = "good" if good else "bad" if bad else ""
        chips.append(f'<span class="chip {cls}">{badge(x["opp_id"], 18)}<b>{esc(x["opp"])}</b>'
                     f'<i>{ha}</i><small>{val:.1f}</small></span>')
    return f'<div class="chips">{"".join(chips) or "No fixtures"}</div>'


def fixture_section(teams):
    ranked_def = sorted((t for t in teams.values() if t["n_fix"]), key=lambda t: t["def_ease"], reverse=True)
    ranked_att = sorted((t for t in teams.values() if t["n_fix"]), key=lambda t: t["att_ease"], reverse=True)

    def block(ranked, key, side, title, note):
        cols = [("Team", team_cell),
                ("Run", lambda t: pill(ease_label(t[key]))),
                ("Upcoming opponents", lambda t: run_chips(t, side))]
        return (f'<div class="card"><h3>{title}</h3><p class="note">{note}</p>'
                f'<h4>Best runs</h4>{table(ranked[:6], cols, False)}'
                f'<h4>Toughest runs</h4>{table(ranked[-4:][::-1], cols, False)}</div>')

    return (block(ranked_def, "def_ease", "def", "Goalkeepers and defenders",
                  "Each chip shows the opponent and how many expected goals they create per game. "
                  "Green = weak attack, good for clean sheets. Red = dangerous attack.")
            + block(ranked_att, "att_ease", "att", "Midfielders and forwards",
                    "Each chip shows the opponent and how many expected goals they concede per game. "
                    "Green = leaky defense, good for goals. Red = tight defense."))


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


def defense_section(players, teams):
    def cols_for(pos):
        thr = DC_THRESHOLD[pos]
        return [
            ("Player", who), ("Owner", owner_cell),
            ("Starts", lambda p: f'{big(p["starts"])}<span class="sub2">of {p["gp"]}</span>'),
            ("Full 90s", lambda p: pct_bar(p["full90_rate"])),
            (f"Actions / 90", lambda p: f'{big(format(p["dc90"], ".1f"))}<span class="sub2">need {thr}</span>'),
            ("Hits DC bonus", lambda p: pct_bar(p["dc_hit"]) + ("<sup>*</sup>" if p["dc_source"] == "estimate" else "")),
            ("Clean sheet next", lambda p: f'<span class="num">{p.get("cs_next", 0):.0%}</span>'),
            ("Fixtures", lambda p: pill(ease_label(teams[p['tid']]['def_ease']))),
            (f"Proj {HORIZON} GWs", lambda p: big(p["proj_run"])),
        ]

    defs = sorted((p for p in players.values() if p["pos"] == "DEF" and p["mins"] >= 180
                   and p["status"] == "a"),
                  key=lambda p: (p["start_rate"] >= 0.8, p["proj_run"]), reverse=True)
    my_defs = sorted((p for p in players.values() if p["pos"] == "DEF" and p["mine"]),
                     key=lambda p: p["proj_run"], reverse=True)
    fa_defs = [p for p in defs if not p["owner"]][:6]
    mids = sorted((p for p in players.values() if p["pos"] == "MID" and p["mins"] >= 180 and p["status"] == "a"),
                  key=lambda p: p["dc_hit"], reverse=True)[:8]

    return ('<div class="explain"><b>How it works:</b> a defender earns <b>+2 points</b> when he makes 10 or more '
            'clearances, blocks, interceptions and tackles in a match. Midfielders and forwards need 12, and recoveries count. '
            'It only pays if he\'s on the pitch, so starts and full 90s matter as much as the rate. '
            '<b>Hits DC bonus</b> = share of his matches where he reached it. <sup>*</sup> = estimated from season totals.</div>'
            f'<div class="card"><h3>Your defenders</h3>{table(my_defs, cols_for("DEF"), False)}</div>'
            f'<div class="card"><h3>Most reliable defenders in the league</h3>{table(defs[:12], cols_for("DEF"))}</div>'
            f'<div class="card"><h3>Best free agent defenders</h3>{table(fa_defs, cols_for("DEF"))}</div>'
            '<div class="card"><h3>Midfielders who bank DC points</h3>'
            '<p class="note">Defensive mids rarely haul, but hitting the bonus in 60%+ of games is close to a free extra point every week.</p>'
            f'{table(mids, cols_for("MID"))}</div>')


def squad_cards(squad, teams):
    def card(p):
        t = teams[p["tid"]]
        lab = ease_label(t["def_ease" if p["pos"] in ("GK", "DEF") else "att_ease"])
        fl = [f for f in flags(p, teams) if not f.startswith(("Easy", "Tough", "Very tough"))]
        def fcls(f):
            if f.startswith(("Core", "On your")):
                return " ok"
            if f.startswith(("DC banker", "Due")):
                return " good"
            return ""
        flag_html = "".join(f'<span class="flag{fcls(f)}">{esc(f)}</span>' for f in fl)
        dc = (f'<div><span class="k">DC hit</span><span class="v">{p["dc_hit"]:.0%}</span></div>'
              if p["pos"] != "GK" else
              f'<div><span class="k">Saves/90</span><span class="v">{p["saves90"]:.1f}</span></div>')
        return f'''<div class="pc{' bench' if p['slot'] != 'XI' else ''}">
  <div class="pc-top">{photo(p, 64)}<div class="pc-id">
    <div class="pc-name">{esc(p['name'])}</div>
    <div class="tm">{badge(p['tid'], 18)}{esc(t['full'])} · {p['pos']}</div>
    <div class="pc-slot">{p['slot']} · {rank_str(p)}</div></div>
    <div class="pc-proj"><span>{p['proj_next']}</span><small>next GW</small></div></div>
  <div class="pc-stats">
    <div><span class="k">Points</span><span class="v">{p['total']}</span></div>
    <div><span class="k">Starts</span><span class="v">{p['starts']}/{p['gp']}</span></div>
    <div><span class="k">G+A / xGI</span><span class="v">{p['g'] + p['a']} / {p['xgi']:.1f}</span></div>
    {dc}
  </div>
  <div class="pc-fix">{pill(lab)}{run_chips(t, 'def' if p['pos'] in ('GK', 'DEF') else 'att')}</div>
  {f'<div class="flags">{flag_html}</div>' if flag_html else ''}
</div>'''
    xi = "".join(card(p) for p in squad if p["slot"] == "XI")
    bench = "".join(card(p) for p in squad if p["slot"] != "XI")
    return f'<h3 class="lbl">Starting XI</h3><div class="grid">{xi}</div><h3 class="lbl">Bench</h3><div class="grid">{bench}</div>'


def market_section(players, teams):
    out = ""
    for pos, label in (("GK", "Goalkeepers"), ("DEF", "Defenders"), ("MID", "Midfielders"), ("FWD", "Forwards")):
        cols = [("Player", who),
                ("Starts", lambda p: f'{big(p["starts"])}<span class="sub2">of {p["gp"]}</span>'),
                ("G+A / xGI", lambda p: f'{big(p["g"] + p["a"])}<span class="sub2">xGI {p["xgi"]:.1f}</span>'),
                ("Fixtures", lambda p, pos=pos: pill(ease_label(teams[p['tid']]['def_ease' if pos in ('GK', 'DEF') else 'att_ease']))),
                (f"Proj {HORIZON} GWs", lambda p: big(p["proj_run"]))]
        if pos in ("DEF", "MID"):
            cols.insert(3, ("Hits DC bonus", lambda p: pct_bar(p["dc_hit"])))
        fa = sorted((p for p in players.values() if p["pos"] == pos and not p["owner"] and p["proj_run"] > 0),
                    key=lambda p: p["proj_run"], reverse=True)[:5]
        out += f'<div class="card"><h3>{label}</h3>{table(fa, cols, False)}</div>'
    return out


def trade_section(players):
    cols = [("Player", who), ("Owner", owner_cell), ("Rank", lambda p: f'<span class="tag">{rank_str(p)}</span>'),
            ("G+A / xGI", lambda p: f'{big(p["g"] + p["a"])}<span class="sub2">xGI {p["xgi"]:.1f}</span>'),
            ("Verdict", lambda p: verdict_pill(finishing_verdict(p))),
            (f"Proj {HORIZON} GWs", lambda p: big(p["proj_run"]))]
    rows = sorted((p for p in players.values() if p["owned_by_other"] and p["proj_run"] > 0),
                  key=lambda p: p["proj_run"], reverse=True)[:12]
    return f'<div class="card">{table(rows, cols, False)}</div>'


TIP_STYLE = {"bench": ("Bench", "t-red"), "start": ("Lineup", "t-blue"), "waiver": ("Waiver", "t-green"),
             "keep": ("Keep", "t-blue"), "sell": ("Sell-high", "t-amber"), "hold": ("Hold", "t-green")}


def tip_cards(tips):
    if not tips:
        return '<div class="tip"><div class="tip-body"><div class="tip-title">No obvious moves this week</div>' \
               '<p>Your squad looks set. Check back after team news.</p></div></div>'
    out = []
    for t in tips:
        label, color = TIP_STYLE.get(t["kind"], ("Note", "t-blue"))
        faces = "".join(photo(p, 56) for p in t["people"])
        out.append(f'<div class="tip {color}"><div class="faces">{faces}</div><div class="tip-body">'
                   f'<span class="tip-kind">{label}</span><div class="tip-title">{esc(t["title"])}</div>'
                   f'<p>{esc(t["body"])}</p></div></div>')
    return "".join(out)


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
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:20px}.two .card{margin:0}\n.ch-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}\n.kpi{text-align:right;flex:none}.kpi b{display:block;font-size:34px;line-height:1;color:var(--navy)}.kpi span{font-size:14px;font-weight:700}\n.legend{display:flex;gap:18px;font-size:13px;color:var(--muted);margin:4px 0 2px}.legend span{display:flex;align-items:center;gap:6px}\n.sw{display:inline-block;width:18px;height:3px;border-radius:2px}.sw.dash{background:repeating-linear-gradient(90deg,#d97706 0 6px,transparent 6px 10px)}\n.chart{width:100%;height:auto;display:block;margin-top:6px}.chart .grid{stroke:#e8edf5;stroke-width:1}\n.chart text{font-size:12px;fill:#5b6b82}.chart .ytick{text-anchor:end}.chart .xtick{text-anchor:middle}\n.chart .pt:hover circle:last-child{r:7}\n.placestrip{display:flex;gap:6px;margin-top:14px}.ps{flex:1;min-width:0;text-align:center;background:#f3f6fb;border-radius:10px;padding:8px 4px}\n.ps small{display:block;font-size:11px;color:var(--muted)}.ps b{font-size:16px}.ps.now{background:var(--blue);color:#fff}.ps.now small{color:#dbe6ff}\n@media (max-width:600px){.two{grid-template-columns:1fr}.kpi b{font-size:28px}.chart text{font-size:22px}.ps b{font-size:14px}.ps small{font-size:10px}}\n.chart.big{margin-top:14px}.chart .axt{font-weight:600}\n.chart .endlab text{font-size:13px;font-weight:600}.chart .endlab.me text{font-weight:800}.chartwrap{overflow-x:auto}\n.chart .series,.chart .endlab{transition:opacity .15s}.chart .dim{opacity:.12}\n.lg-row{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}\n.lg-btn{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 12px;font:600 13px/1 inherit;color:var(--ink);cursor:pointer}\n.lg-btn:hover{background:var(--blue-soft)}.lg-btn.on{border-color:var(--blue);box-shadow:0 0 0 2px var(--blue-soft)}\n.lg-btn .sw,.lgn .sw{width:12px;height:12px;border-radius:50%}.lgn{display:inline-flex;align-items:center;gap:8px;font-weight:600}\n.lgt td,.lgt th{text-align:center;padding:10px 12px}.lgt td:first-child,.lgt th:first-child{text-align:left}\n.lgt td.wk{font-weight:800;color:var(--navy)}\n@media (max-width:600px){.chart.big{min-width:760px}.chart.big text{font-size:13px}}\n.sortbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:4px 0 14px}\n.sb{border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 14px;font:600 13px/1 inherit;color:var(--blue);cursor:pointer}\n.sb.on{background:var(--blue);color:#fff;border-color:var(--blue)}\n.tt td.rk{font-weight:800;color:var(--muted);width:36px}\n.fms{display:flex;gap:4px}.fm{display:inline-flex;width:24px;height:24px;border-radius:6px;align-items:center;justify-content:center;font-size:12px;font-weight:800;color:#fff}.fm.W{background:var(--green)}.fm.D{background:#94a3b8}.fm.L{background:var(--red)}\n.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}\n.mc{border:1px solid var(--line);border-radius:16px;padding:18px;background:#fff;display:flex;flex-direction:column;gap:12px}\n.mc.hot{border-color:#bfd2ff;box-shadow:0 0 0 3px var(--blue-soft)}\n.mko{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.5px}\n.mrow{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:10px}\n.ms{display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center}.mn{font-weight:800;font-size:15px}\n.mscore{display:flex;flex-wrap:wrap;justify-content:center;align-items:baseline;gap:6px;max-width:120px}\n.mscore span{font-size:28px;font-weight:800;color:var(--navy)}.mscore i{font-style:normal;color:var(--muted)}\n.mscore small{width:100%;text-align:center;font-size:11px;color:var(--muted)}\n.mflag{display:flex;gap:10px;align-items:flex-start;border-radius:12px;padding:10px 12px;font-size:14px;line-height:1.5}\n.mflag.att{background:var(--green-soft)}.mflag.cs{background:var(--blue-soft)}\n.mft{flex:none;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.5px;padding:2px 8px;border-radius:6px;color:#fff;margin-top:2px}\n.mflag.att .mft{background:var(--green)}.mflag.cs .mft{background:var(--blue)}\n.mflag .badge{margin-top:2px}b.g{color:var(--green)}b.b{color:var(--blue)}\n.mmine{display:flex;flex-wrap:wrap;gap:8px;align-items:center;border-top:1px solid var(--line);padding-top:10px}\n.mp{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:13px;background:#f3f6fb;border-radius:999px;padding:3px 10px 3px 3px}\n@media (max-width:600px){.mgrid{grid-template-columns:1fr}.mflag{flex-wrap:wrap}}\nfooter{color:var(--muted);font-size:13px;margin-top:60px;max-width:820px}
@media (max-width:600px){header h1{font-size:26px}.card{padding:18px}.tip{flex-direction:column;align-items:flex-start}}
"""


def render(d, players, teams):
    TEAMS.clear()
    TEAMS.update(teams)
    squad = [players[pk["element"]] for pk in sorted(d["picks"], key=lambda x: x["position"])]
    for i, p in enumerate(squad):
        p["slot"] = "XI" if i < 11 else f"Bench {i - 10}"

    injuries = [p for p in squad if p["status"] != "a" or (p["chance"] is not None and p["chance"] < 100)]
    inj = "".join(f'<div class="inj">{photo(p, 48)}<div><div class="nm">{esc(p["name"])}</div>'
                  f'<div class="note" style="margin:0">{esc(p["news"] or STATUS.get(p["status"], ""))}'
                  + ("" if "chance" in (p["news"] or "") or p["chance"] is None
                     else f' · {p["chance"]}% chance of playing')
                  + '</div></div></div>'
                  for p in injuries) or '<p class="empty">No injury news on your squad.</p>'
    e = d["entry"]
    gw_range = f"GW{d['next']}–{d['next'] + HORIZON - 1}"
    proj_xi = sum(p["proj_next"] for p in squad[:11])

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">
<title>Boofla Report</title><style>{CSS}</style></head><body>
<header><div class="in">
<h1>{esc(e['name'])} · Gameweek {d['next']}</h1>
<p>Updated {datetime.datetime.now(datetime.timezone.utc):%A %b %d, %H:%M} UTC · fixtures cover {gw_range}</p>
<div class="stats">
<div class="stat"><b>{e.get('overall_points', '?')}</b><span>Total points</span></div>
<div class="stat"><b>{e.get('event_points', '?')}</b><span>Last gameweek</span></div>
<div class="stat"><b>{proj_xi:.0f}</b><span>Projected next GW (XI)</span></div>
</div></div></header>
<nav><div class="in"><a href="#season">Season</a><a href="#teams">Teams &amp; matchups</a><a href="#advice">Advice</a><a href="#squad">My squad</a><a href="#fixtures">Fixtures</a>
<a href="#finishing">xG vs actual</a><a href="#defense">Defensive contribution</a>
<a href="#market">Free agents</a><a href="#trades">Trade targets</a></div></nav>
<main>

{season_section(d)}

{teams_section(d, teams, players)}

<section id="advice"><h2>This week's advice</h2>
<p class="note">Lineup calls, waiver moves and keep or sell reads. Core players are never suggested as drops.</p>
<div class="tips">{tip_cards(advice(squad, players, teams))}</div>
<div class="card"><h3>Injuries and news</h3>{inj}</div></section>

<section id="squad"><h2>My squad</h2>
<p class="note">The big blue number is projected points next gameweek: chance of starting, xG and xA against this
opponent, clean sheet odds, defensive contribution, saves and bonus.</p>
{squad_cards(squad, teams)}</section>

<section id="fixtures"><h2>Fixture outlook, {gw_range}</h2>
<p class="note">Who has the easy and hard runs coming up, and why, adjusted for home and away.</p>
{fixture_section(teams)}</section>

<section id="finishing"><h2>xG and xA vs actual returns</h2>
<p class="note">+/- is actual minus expected. Positive = finishing above their chances, negative = due for more.</p>
{finishing_section(players)}</section>

<section id="defense"><h2>Defensive contribution</h2>
<p class="note">Defenders who play 90 minutes and rack up actions bank points even when the clean sheet goes.</p>
{defense_section(players, teams)}</section>

<section id="market"><h2>Best free agents</h2>
<p class="note">Ranked by projected points over the next {HORIZON} gameweeks.</p>
{market_section(players, teams)}</section>

<section id="trades"><h2>Trade targets on other teams</h2>
{trade_section(players)}</section>

<footer>Fixture ease is relative to an average run. Team xG = sum of its players' xG; team xGC = its main keeper's
xG conceded per 90. Data from the FPL Draft and FPL APIs. Photos and badges from the Premier League.</footer>
</main></body></html>"""


def main():
    try:
        d = load_data()
    except requests.RequestException as err:
        sys.exit(f"Could not reach the FPL API: {err}")
    teams, _, lg_xgc = build_teams(d)
    players = build_players(d, teams)
    for p in players.values():
        project(p, teams, lg_xgc)
    rank_and_tag(players, [pk["element"] for pk in d["picks"]])
    out = Path(os.environ.get("REPORT_PATH") or Path(__file__).with_name("fpl_report.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d, players, teams), encoding="utf-8")
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
