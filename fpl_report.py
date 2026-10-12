#!/usr/bin/env python3
"""
FPL Draft weekly report for Boofla (Hope Bros league).

Run:  pip install requests
      python fpl_report.py
Output: fpl_report.html in the same folder (open it in your browser).

Data comes from the unofficial FPL Draft and FPL APIs. If the Premier League
changes them, the fix will almost always be in get() or load_data().
"""
import datetime
import html
import os
import sys
from pathlib import Path

import requests

ENTRY_ID = 275109  # your team ID

DRAFT = "https://draft.premierleague.com/api"
CLASSIC = "https://fantasy.premierleague.com/api"
POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
STATUS = {"a": "Available", "d": "Doubtful", "i": "Injured",
          "s": "Suspended", "u": "Unavailable", "n": "Not in squad"}

# ---------- data loading ----------

def get(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
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
    nxt = events.get("next") if isinstance(events, dict) else None
    nxt = nxt or current + 1

    picks = get(f"{DRAFT}/entry/{ENTRY_ID}/event/{current}")["picks"]
    status = get(f"{DRAFT}/league/{league_id}/element-status")["element_status"]
    details = get(f"{DRAFT}/league/{league_id}/details")
    fixtures = get(f"{CLASSIC}/fixtures/")  # team ids match the Draft game

    return dict(boot=boot, entry=entry, league_id=league_id, current=current,
                next=nxt, picks=picks, status=status, details=details,
                fixtures=fixtures)

# ---------- analysis ----------

def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_players(d):
    teams = {t["id"]: t.get("short_name", t["name"]) for t in d["boot"]["teams"]}

    # owner lookup: element -> manager name
    names = {}
    for e in d["details"].get("league_entries", []):
        label = e.get("entry_name") or e.get("player_first_name", "?")
        names[e.get("entry_id")] = label
        names[e.get("id")] = label
    owner = {s["element"]: names.get(s.get("owner")) for s in d["status"]
             if s.get("owner")}

    # next 3 fixtures per team: list of (opponent, home/away, difficulty)
    upcoming = {t: [] for t in teams}
    for f in sorted(d["fixtures"], key=lambda f: (f.get("event") or 99)):
        ev = f.get("event")
        if not ev or ev < d["next"] or f.get("finished"):
            continue
        h, a = f["team_h"], f["team_a"]
        if len(upcoming[h]) < 3:
            upcoming[h].append((teams[a], "H", f["team_h_difficulty"]))
        if len(upcoming[a]) < 3:
            upcoming[a].append((teams[h], "A", f["team_a_difficulty"]))

    gws_played = max(d["current"], 1)
    players = {}
    for el in d["boot"]["elements"]:
        mins = el.get("minutes", 0)
        xgi = num(el.get("expected_goal_involvements"))
        fx = upcoming.get(el["team"], [])
        p = dict(
            id=el["id"], name=el["web_name"], pos=POS[el["element_type"]],
            team=teams.get(el["team"], "?"), status=el.get("status", "a"),
            news=el.get("news", ""), chance=el.get("chance_of_playing_next_round"),
            form=num(el.get("form")), ppg=num(el.get("points_per_game")),
            total=el.get("total_points", 0), mins=mins,
            mins_share=mins / (gws_played * 90),
            goals=el.get("goals_scored", 0), assists=el.get("assists", 0),
            xgi=xgi, xgi90=(xgi / mins * 90) if mins >= 90 else 0.0,
            fixtures=fx,
            fdr=sum(f[2] for f in fx) / len(fx) if fx else 3.0,
            owner=owner.get(el["id"]),
        )
        p["score"] = score(p)
        players[el["id"]] = p
    return players


def score(p):
    """Simple 'who do I want next week' score. Tweak weights to taste."""
    if p["status"] in ("i", "s", "u", "n"):
        return 0.0
    s = (p["form"] * 1.0             # recent points
         + p["ppg"] * 0.5            # season points per game
         + p["xgi90"] * 4.0          # underlying attacking threat
         + (3.0 - p["fdr"]) * 0.75   # easier fixtures add, harder subtract
         + min(p["mins_share"], 1.0) * 2.0)  # nailed starters
    if p["chance"] is not None and p["chance"] < 100:
        s *= p["chance"] / 100
    return round(s, 2)


def flags(p):
    out = []
    if p["status"] != "a" or (p["chance"] is not None and p["chance"] < 100):
        out.append(f"{STATUS.get(p['status'], p['status'])}: {p['news'] or 'no detail'}")
    if p["mins_share"] < 0.5 and p["pos"] != "GK":
        out.append(f"Low minutes ({p['mins_share']:.0%} of possible)")
    if p["fdr"] >= 3.67:
        out.append("Tough fixtures ahead")
    if p["form"] < 2 and p["mins_share"] >= 0.5:
        out.append("Cold form")
    if p["pos"] in ("MID", "FWD") and p["xgi"] - (p["goals"] + p["assists"]) >= 1.5:
        out.append("Underperforming xGI, points likely coming")
    return out


def advice(squad, players):
    tips = []
    xi, bench = squad[:11], squad[11:]

    for p in xi:
        if p["status"] in ("i", "s", "u", "n") or (p["chance"] is not None and p["chance"] <= 25):
            better = [b for b in bench if b["pos"] == p["pos"] and b["score"] > 0]
            alt = f" Bench option: {better[0]['name']}." if better else ""
            why = p["news"] or (f"{p['chance']}% chance of playing" if p["chance"] is not None
                                else STATUS.get(p["status"]))
            tips.append(f"Bench {p['name']} ({why}).{alt}")

    for b in bench:
        worst = min((p for p in xi if p["pos"] == b["pos"]), key=lambda p: p["score"], default=None)
        if worst and b["score"] > worst["score"] + 1.5:
            tips.append(f"Consider starting {b['name']} over {worst['name']} "
                        f"(score {b['score']} vs {worst['score']}).")

    free = [p for p in players.values() if not p["owner"] and p["score"] > 0]
    for pos in ("GK", "DEF", "MID", "FWD"):
        mine = [p for p in squad if p["pos"] == pos]
        if not mine:
            continue
        weakest = min(mine, key=lambda p: p["score"])
        best_fa = max((p for p in free if p["pos"] == pos), key=lambda p: p["score"], default=None)
        if best_fa and best_fa["score"] > weakest["score"] + 2:
            tips.append(f"Waiver idea: drop {weakest['name']} ({weakest['score']}) "
                        f"for {best_fa['name']} ({best_fa['team']}, {best_fa['score']}).")

    return tips or ["No obvious moves this week. Squad looks set."]

# ---------- report ----------

def fx_str(p):
    return ", ".join(f"{o} ({ha}) {d}" for o, ha, d in p["fixtures"]) or "-"


def table(rows, cols):
    head = "".join(f"<th>{c}</th>" for c, _ in cols)
    body = ""
    for r in rows:
        body += "<tr>" + "".join(f"<td>{html.escape(str(fn(r)))}</td>" for _, fn in cols) + "</tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


BASE_COLS = [
    ("Player", lambda p: p["name"]), ("Pos", lambda p: p["pos"]),
    ("Team", lambda p: p["team"]), ("Form", lambda p: p["form"]),
    ("Pts", lambda p: p["total"]), ("Min %", lambda p: f"{p['mins_share']:.0%}"),
    ("xGI/90", lambda p: f"{p['xgi90']:.2f}"), ("Next 3 (FDR)", fx_str),
    ("Score", lambda p: p["score"]),
]


def render(d, players):
    squad = [players[pk["element"]] for pk in sorted(d["picks"], key=lambda x: x["position"])]
    for i, p in enumerate(squad):
        p["slot"] = "XI" if i < 11 else f"Bench {i - 10}"

    injuries = [p for p in squad if p["status"] != "a" or (p["chance"] is not None and p["chance"] < 100)]
    free = sorted((p for p in players.values() if not p["owner"] and p["score"] > 0),
                  key=lambda p: p["score"], reverse=True)
    risers = sorted((p for p in players.values() if p["owner"] and p["owner"] != d["entry"]["name"]
                     and p["score"] > 0), key=lambda p: p["score"], reverse=True)[:12]

    squad_cols = [("Slot", lambda p: p["slot"])] + BASE_COLS + [
        ("Flags", lambda p: "; ".join(flags(p)) or "OK")]

    fa_html = ""
    for pos in ("GK", "DEF", "MID", "FWD"):
        top = [p for p in free if p["pos"] == pos][:5]
        fa_html += f"<h3>{pos}</h3>" + table(top, BASE_COLS)

    tips = "".join(f"<li>{html.escape(t)}</li>" for t in advice(squad, players))
    inj = "".join(f"<li><b>{html.escape(p['name'])}</b>: {html.escape(p['news'] or STATUS.get(p['status'], ''))}"
                  f" (chance next round: {p['chance'] if p['chance'] is not None else '?'}%)</li>"
                  for p in injuries) or "<li>No injury news on your squad.</li>"

    e = d["entry"]
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Boofla Report GW{d['next']}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;color:#1a1a1a;background:#fafaf7}}
h1{{margin-bottom:4px}} .sub{{color:#666;margin-top:0}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px;font-size:14px}}
th,td{{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}}
th{{background:#37003c;color:#fff}} tr:hover td{{background:#f0eef5}}
section{{overflow-x:auto}} li{{margin:4px 0}}
</style></head><body>
<h1>{html.escape(e['name'])}: GW{d['next']} report</h1>
<p class="sub">{e.get('overall_points', '?')} total pts, {e.get('event_points', '?')} last GW.
Generated {datetime.datetime.now():%a %b %d, %I:%M %p}.</p>
<section><h2>Advice</h2><ul>{tips}</ul></section>
<section><h2>Injuries and news</h2><ul>{inj}</ul></section>
<section><h2>My squad</h2>{table(squad, squad_cols)}</section>
<section><h2>Best free agents</h2>{fa_html}</section>
<section><h2>Trade targets (owned by others)</h2>
{table(risers, BASE_COLS + [("Owner", lambda p: p["owner"])])}</section>
<p class="sub">Score = form + 0.5 x PPG + 4 x xGI/90 + fixture adjustment + minutes security,
scaled down by injury doubt. FDR 1 = easiest, 5 = hardest.</p>
</body></html>"""


def main():
    try:
        d = load_data()
    except requests.RequestException as err:
        sys.exit(f"Could not reach the FPL API: {err}")
    players = build_players(d)
    out = Path(os.environ.get("REPORT_PATH") or Path(__file__).with_name("fpl_report.html"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d, players), encoding="utf-8")
    print(f"Report written to {out}")


if __name__ == "__main__":
    main()
