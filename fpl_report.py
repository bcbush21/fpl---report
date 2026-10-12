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
                       "gp": 0, "gf": 0, "ga": 0, "xg": 0.0, "xgc90": None}
             for t in d["boot"]["teams"]}

    for f in d["fixtures"]:
        if f.get("finished") and f.get("team_h_score") is not None:
            h, a = teams[f["team_h"]], teams[f["team_a"]]
            h["gp"] += 1; a["gp"] += 1
            h["gf"] += f["team_h_score"]; h["ga"] += f["team_a_score"]
            a["gf"] += f["team_a_score"]; a["ga"] += f["team_h_score"]

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
                gw=ev, opp=o["name"], home=home,
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

# ---------------------------------------------------------------- advice

def advice(squad, players, teams):
    tips = []
    xi, bench = squad[:11], squad[11:]

    for p in xi:
        if p["status"] in ("i", "s", "u", "n") or (p["chance"] is not None and p["chance"] <= 25):
            why = p["news"] or (f"{p['chance']}% chance of playing" if p["chance"] is not None
                                else STATUS.get(p["status"]))
            tips.append(f"Bench {p['name']} ({why}).")

    for b in bench:
        same = [p for p in xi if p["pos"] == b["pos"]]
        if same:
            worst = min(same, key=lambda p: p["proj_next"])
            if b["proj_next"] > worst["proj_next"] + 0.8:
                tips.append(f"Start {b['name']} over {worst['name']}: projected "
                            f"{b['proj_next']} vs {worst['proj_next']} pts next GW.")

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
                why.append(f"hits DC {best['dc_hit']:.0%} of games")
            why.append(f"{ease_label(teams[best['tid']]['def_ease' if pos in ('GK', 'DEF') else 'att_ease']).lower()} fixtures")
            if finishing_verdict(best).startswith("Due"):
                why.append(f"xGI {best['xgi']:.1f} vs {best['g'] + best['a']} G+A")
            tips.append(f"Waiver: {weakest['name']} out, {best['name']} ({best['team']}) in. "
                        f"Next {HORIZON}: {best['proj_run']} vs {weakest['proj_run']} pts ({', '.join(why)}).")

    for p in squad:
        ga = p["g"] + p["a"]
        if p["gi_diff"] >= 1.5 and p["pos_rank"]:
            if p["core"] and p["xgi"] >= 1.0:
                tips.append(f"Keep {p['name']}: {ga} G+A from {p['xgi']:.1f} xGI, so some cooling is likely, "
                            f"but his underlying numbers still rank {rank_str(p)}. He's a core piece.")
            elif not p["core"] and p["pos_pct"] > 0.5:
                # hot streak propping up below-average underlying numbers: the real sell-high case
                tips.append(f"Sell-high candidate: {p['name']} has {ga} G+A from only {p['xgi']:.1f} xGI and "
                            f"his underlying numbers rank {rank_str(p)}. If someone offers a better-rated "
                            "player off his points, take it.")
        if finishing_verdict(p).startswith("Due") and p["start_rate"] >= 0.7:
            tips.append(f"Hold {p['name']}: {ga} G+A from {p['xgi']:.1f} xGI ({rank_str(p)}). "
                        "The chances are there, returns should follow.")

    return tips or ["No obvious moves this week."]

# ---------------------------------------------------------------- rendering

def table(rows, cols, mark_mine=True):
    head = "".join(f"<th>{c}</th>" for c, _ in cols)
    body = []
    for r in rows:
        cls = ' class="mine"' if mark_mine and r.get("mine") else ""
        cells = "".join(f"<td>{fn(r)}</td>" for _, fn in cols)
        body.append(f"<tr{cls}>{cells}</tr>")
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def diff_cell(v):
    cls = "pos" if v >= 0.75 else "neg" if v <= -0.75 else ""
    return f'<span class="{cls}">{v:+.1f}</span>'


def pill(label):
    cls = {"Easy": "e1", "Good": "e2", "Average": "e3", "Tough": "e4", "Very tough": "e5", "Blank": "e5"}.get(label, "")
    return f'<span class="pill {cls}">{label}</span>'


def who(p):
    tag = " ★" if p["mine"] else ""
    return f"<b>{esc(p['name'])}</b>{tag}"


def owner_cell(p):
    if p["mine"]:
        return "You"
    return esc(p["owner"]) if p["owner"] else '<span class="fa">Free agent</span>'


def fixture_section(teams):
    ranked_def = sorted((t for t in teams.values() if t["n_fix"]), key=lambda t: t["def_ease"], reverse=True)
    ranked_att = sorted((t for t in teams.values() if t["n_fix"]), key=lambda t: t["att_ease"], reverse=True)

    def block(ranked, key, side, title, note):
        cols = [("Team", lambda t: f"<b>{esc(t['name'])}</b>"),
                ("Run", lambda t: pill(ease_label(t[key]))),
                ("Ease", lambda t: f"{t[key]:.2f}"),
                ("Own xG/g" if side == "att" else "Own xGC/g",
                 lambda t: f"{t['xg_pg']:.2f}" if side == "att" else f"{t['xgc90']:.2f}"),
                ("Why", lambda t: f'<span class="why">{esc(run_str(t, side))}</span>')]
        return (f"<h3>{title}</h3><p class='note'>{note}</p>"
                + table(ranked[:6], cols, False)
                + "<p class='note'>Toughest runs</p>" + table(ranked[-4:][::-1], cols, False))

    return (block(ranked_def, "def_ease", "def", "GK / DEF: clean sheet fixtures",
                  "Ranked by how little the upcoming opponents create (their xG per game), adjusted for home/away. "
                  "Low opponent xG = clean sheet chances.")
            + block(ranked_att, "att_ease", "att", "MID / FWD: attacking fixtures",
                    "Ranked by how much the upcoming opponents give up (their xG conceded per game), adjusted for home/away."))


def finishing_section(players):
    outfield = [p for p in players.values() if p["pos"] != "GK" and p["mins"] >= 180]
    cols = [("Player", who), ("Pos", lambda p: p["pos"]), ("Team", lambda p: esc(p["team"])),
            ("G", lambda p: p["g"]), ("xG", lambda p: f"{p['xg']:.2f}"), ("G−xG", lambda p: diff_cell(p["g_diff"])),
            ("A", lambda p: p["a"]), ("xA", lambda p: f"{p['xa']:.2f}"), ("A−xA", lambda p: diff_cell(p["a_diff"])),
            ("xGI/90", lambda p: f"{p['raw_xgi90']:.2f}"), ("Rank", rank_str),
            ("Verdict", lambda p: esc(finishing_verdict(p))), ("Owner", owner_cell)]

    mine = sorted((p for p in outfield if p["mine"]), key=lambda p: p["gi_diff"])
    due = sorted((p for p in outfield if not p["mine"] and p["xgi"] >= 1.2 and p["start_rate"] >= 0.6),
                 key=lambda p: p["gi_diff"])[:12]
    hot = sorted((p for p in outfield if p["xgi"] >= 0.5), key=lambda p: p["gi_diff"], reverse=True)[:10]

    return ("<h3>My squad</h3>" + table(mine, cols)
            + "<h3>Buy-low targets: biggest underperformers</h3>"
              "<p class='note'>Getting the chances but not the returns yet. Over a season, finishing tends to drift back toward xG.</p>"
            + table(due, cols)
            + "<h3>Running hot: biggest overperformers</h3>"
              "<p class='note'>Scoring more than their chances suggest. Expect some cooling, but only toward their xG. "
              "\"Elite and clinical\" = still a top-25% player at his position even after cooling, so keep. "
              "\"Running hot\" = the underlying numbers are ordinary, so the points may not last.</p>"
            + table(hot, cols))


def defense_section(players, teams):
    def cols_for(pos):
        thr = DC_THRESHOLD[pos]
        return [
            ("Player", who), ("Team", lambda p: esc(p["team"])), ("Owner", owner_cell),
            ("Starts", lambda p: f"{p['starts']}/{p['gp']}"),
            ("Full 90s", lambda p: f"{p['full90_rate']:.0%}"),
            (f"DC/90 (need {thr})", lambda p: f"{p['dc90']:.1f}"),
            ("DC hit %", lambda p: f"{p['dc_hit']:.0%}" + ("*" if p["dc_source"] == "estimate" else "")),
            ("CS next", lambda p: f"{p.get('cs_next', 0):.0%}"),
            ("Fixtures", lambda p: pill(ease_label(teams[p['tid']]['def_ease']))),
            (f"Proj next {HORIZON}", lambda p: f"<b>{p['proj_run']}</b>"),
        ]

    defs = sorted((p for p in players.values() if p["pos"] == "DEF" and p["mins"] >= 180
                   and p["status"] == "a"),
                  key=lambda p: (p["start_rate"] >= 0.8, p["proj_run"]), reverse=True)
    my_defs = sorted((p for p in players.values() if p["pos"] == "DEF" and p["mine"]),
                     key=lambda p: p["proj_run"], reverse=True)
    fa_defs = [p for p in defs if not p["owner"]][:8]
    mids = sorted((p for p in players.values() if p["pos"] == "MID" and p["mins"] >= 180 and p["status"] == "a"),
                  key=lambda p: p["dc_hit"], reverse=True)[:10]

    return ("<p class='note'>Defensive contribution = +2 pts when a defender makes 10+ clearances, blocks, interceptions "
            "and tackles in a match (12+ including recoveries for mids and forwards). It only pays if they are on the "
            "pitch, so starts and full 90s matter as much as the per-90 rate. "
            "DC hit % = share of their appearances where they reached the threshold. "
            "* = estimated from season totals.</p>"
            + "<h3>My defenders</h3>" + table(my_defs, cols_for("DEF"))
            + "<h3>Most reliable defenders in the league</h3>" + table(defs[:15], cols_for("DEF"))
            + "<h3>Best free agent defenders</h3>" + table(fa_defs, cols_for("DEF"))
            + "<h3>Midfielders who bank DC points</h3>"
              "<p class='note'>Defensive mids rarely haul, but a 60%+ hit rate is close to a guaranteed extra point a week.</p>"
            + table(mids, cols_for("MID")))


def squad_section(squad, teams):
    cols = [("Slot", lambda p: p["slot"]), ("Player", who), ("Pos", lambda p: p["pos"]),
            ("Team", lambda p: esc(p["team"])),
            ("Pts", lambda p: p["total"]), ("Rank", rank_str),
            ("Starts", lambda p: f"{p['starts']}/{p['gp']}"),
            ("G+A / xGI", lambda p: f"{p['g'] + p['a']} / {p['xgi']:.1f}"),
            ("DC hit", lambda p: f"{p['dc_hit']:.0%}" if p["pos"] != "GK" else "-"),
            ("Fixtures", lambda p: pill(ease_label(teams[p['tid']]['def_ease' if p['pos'] in ('GK', 'DEF') else 'att_ease']))),
            ("Next GW", lambda p: f"<b>{p['proj_next']}</b>"),
            (f"Next {HORIZON}", lambda p: p["proj_run"]),
            ("Flags", lambda p: esc("; ".join(flags(p, teams)) or "OK"))]
    return table(squad, cols, False)


def market_section(players, teams):
    out = ""
    for pos in ("GK", "DEF", "MID", "FWD"):
        cols = [("Player", who), ("Team", lambda p: esc(p["team"])),
                ("Starts", lambda p: f"{p['starts']}/{p['gp']}"),
                ("G+A / xGI", lambda p: f"{p['g'] + p['a']} / {p['xgi']:.1f}"),
                ("Fixtures", lambda p, pos=pos: pill(ease_label(teams[p['tid']]['def_ease' if pos in ('GK', 'DEF') else 'att_ease']))),
                (f"Proj next {HORIZON}", lambda p: f"<b>{p['proj_run']}</b>")]
        if pos in ("DEF", "MID"):
            cols.insert(4, ("DC hit", lambda p: f"{p['dc_hit']:.0%}"))
        fa = sorted((p for p in players.values() if p["pos"] == pos and not p["owner"] and p["proj_run"] > 0),
                    key=lambda p: p["proj_run"], reverse=True)[:5]
        out += f"<h3>{pos}</h3>" + table(fa, cols)
    return out


def trade_section(players):
    cols = [("Player", who), ("Pos", lambda p: p["pos"]), ("Team", lambda p: esc(p["team"])),
            ("Owner", owner_cell), ("Rank", rank_str),
            ("G+A / xGI", lambda p: f"{p['g'] + p['a']} / {p['xgi']:.1f}"),
            ("Verdict", lambda p: esc(finishing_verdict(p))),
            (f"Proj next {HORIZON}", lambda p: f"<b>{p['proj_run']}</b>")]
    rows = sorted((p for p in players.values() if p["owned_by_other"] and p["proj_run"] > 0),
                  key=lambda p: p["proj_run"], reverse=True)[:15]
    return table(rows, cols, False)


CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1b1b1f;--muted:#6b6b76;--line:#e6e4dd;--brand:#37003c;
--pos:#0a7d43;--neg:#b42318;--mine:#fff6d6;--e1:#0a7d43;--e2:#5aa469;--e3:#9a9aa3;--e4:#d9822b;--e5:#b42318}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--card:#1b1b1f;--ink:#ececf1;--muted:#9a9aa3;
--line:#2c2c33;--brand:#c58bd0;--mine:#3a3320;--pos:#4cc38a;--neg:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:0 auto;padding:20px 16px 60px}
h1{margin:0 0 2px;font-size:26px}h2{margin:36px 0 6px;font-size:20px;color:var(--brand)}
h3{margin:22px 0 6px;font-size:16px}.sub,.note{color:var(--muted);margin:2px 0 8px;font-size:13px}
nav{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}nav a{color:var(--brand);text-decoration:none;
border:1px solid var(--line);background:var(--card);padding:4px 10px;border-radius:999px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card li{margin:5px 0}.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:6px 9px;text-align:left;
border-bottom:1px solid var(--line);white-space:nowrap}th{background:var(--brand);color:#fff;font-weight:600}
tr:last-child td{border-bottom:0}tr.mine td{background:var(--mine)}.why{white-space:normal;color:var(--muted)}
.pos{color:var(--pos);font-weight:600}.neg{color:var(--neg);font-weight:600}.fa{color:var(--pos);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;color:#fff;font-size:12px;background:var(--e3)}
.e1{background:var(--e1)}.e2{background:var(--e2)}.e3{background:var(--e3)}.e4{background:var(--e4)}.e5{background:var(--e5)}
"""


def render(d, players, teams):
    squad = [players[pk["element"]] for pk in sorted(d["picks"], key=lambda x: x["position"])]
    for i, p in enumerate(squad):
        p["slot"] = "XI" if i < 11 else f"Bench {i - 10}"

    injuries = [p for p in squad if p["status"] != "a" or (p["chance"] is not None and p["chance"] < 100)]
    inj = "".join(f"<li><b>{esc(p['name'])}</b>: {esc(p['news'] or STATUS.get(p['status'], ''))} "
                  f"(chance next round: {p['chance'] if p['chance'] is not None else '?'}%)</li>"
                  for p in injuries) or "<li>No injury news on your squad.</li>"
    tips = "".join(f"<li>{esc(t)}</li>" for t in advice(squad, players, teams))
    e = d["entry"]
    gw_range = f"GW{d['next']}–{d['next'] + HORIZON - 1}"

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Boofla Report</title><style>{CSS}</style></head><body><main>
<h1>{esc(e['name'])}: GW{d['next']} report</h1>
<p class="sub">{e.get('overall_points', '?')} total pts · {e.get('event_points', '?')} last GW ·
fixtures cover {gw_range} · updated {datetime.datetime.now(datetime.timezone.utc):%a %b %d, %H:%M} UTC</p>
<nav><a href="#advice">Advice</a><a href="#fixtures">Fixtures</a><a href="#finishing">xG vs actual</a>
<a href="#defense">Defensive contribution</a><a href="#squad">My squad</a><a href="#market">Free agents</a>
<a href="#trades">Trade targets</a></nav>

<h2 id="advice">Advice</h2><div class="card"><ul>{tips}</ul></div>
<h3>Injuries and news</h3><div class="card"><ul>{inj}</ul></div>

<h2 id="fixtures">Fixture outlook, {gw_range}</h2>
{fixture_section(teams)}

<h2 id="finishing">xG and xA vs actual returns</h2>
<p class="note">G−xG and A−xA: positive = scoring more than chances suggest, negative = due for more.
★ = your player. Highlighted rows are yours.</p>
{finishing_section(players)}

<h2 id="defense">Defensive contribution and 90-minute defenders</h2>
{defense_section(players, teams)}

<h2 id="squad">My squad</h2>
<p class="note">Projected points combine: chance of starting, xG and xA per 90 adjusted for opponent,
clean sheet odds from both teams' xG, defensive contribution hit rate, saves and bonus.</p>
{squad_section(squad, teams)}

<h2 id="market">Best free agents by projected points</h2>
{market_section(players, teams)}

<h2 id="trades">Trade targets on other teams</h2>
{trade_section(players)}

<p class="note" style="margin-top:30px">Fixture ease: 1.00 = average run, higher = easier. Team xG = sum of its players' xG;
team xGC = its main keeper's xG conceded per 90. Data: FPL Draft + FPL APIs.</p>
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
