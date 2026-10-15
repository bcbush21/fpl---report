"""
What the top 1,000 managers in the main FPL game own.

Reads the overall leaderboard (league 314, 50 managers a page) and each manager's squad for the current
gameweek and the one before, then counts how many of them own and captain each player. Squads only change
at the deadline, so this runs once per gameweek and is saved in elite.json; later builds that week reuse it.

These are budget-game squads (a £100m budget, captains, everyone can own the same player), not Draft teams,
so read them as "who the best managers rate", not as a Draft ranking.
"""
import datetime as dt
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

CLASSIC = "https://fantasy.premierleague.com/api"
OVERALL = 314
TOP_N = 1000
CACHE = Path(os.environ.get("ELITE_CACHE") or Path(__file__).with_name("elite.json"))
WORKERS = 6

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0"


def _get(url, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.RequestException(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == tries - 1:
                return None
            time.sleep(2 * (i + 1))
    return None


def _load():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        return {}


def _count(entries, gw):
    """Ownership and captaincy counts, by player code, for one gameweek's squads."""
    def picks(eid):
        j = _get(f"{CLASSIC}/entry/{eid}/event/{gw}/picks/")
        return None if j is None else (j.get("active_chip"), j.get("picks") or [])

    own, cap, n, freehit, failed = {}, {}, 0, 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for res in pool.map(picks, entries):
            if res is None:
                failed += 1
                continue
            chip, ps = res
            if chip == "freehit":   # a one-week squad that says nothing about who they rate
                freehit += 1
                continue
            n += 1
            for pk in ps:
                el = pk["element"]
                own[el] = own.get(el, 0) + 1
                if pk.get("is_captain"):
                    cap[el] = cap.get(el, 0) + 1
    return dict(n=n, own=own, cap=cap, freehit=freehit, failed=failed)


def fetch(classic_boot):
    """Bring elite.json up to date for the current gameweek. Returns the cache."""
    cache = _load()
    if not classic_boot:
        return cache
    evs = classic_boot.get("events") or []
    cur = next((e["id"] for e in evs if e.get("is_current")), None)
    if not cur:
        return cache
    code = {e["id"]: e["code"] for e in classic_boot.get("elements", [])}
    gws = cache.setdefault("gws", {})
    have = gws.get(str(cur))
    if have and not have.get("partial"):
        return cache

    # the top 1,000 as of now
    entries, page = [], 1
    while len(entries) < TOP_N:
        j = _get(f"{CLASSIC}/leagues-classic/{OVERALL}/standings/?page_standings={page}")
        if not j:
            break
        rows = j["standings"]["results"]
        entries += [(r["entry"], r["total"]) for r in rows]
        if not j["standings"].get("has_next"):
            break
        page += 1
    entries = entries[:TOP_N]
    if len(entries) < 100:
        print(f"Top managers: leaderboard unavailable ({len(entries)} rows)")
        return cache
    ids = [e for e, _ in entries]

    def to_codes(c):
        return {str(code[k]): v for k, v in c.items() if k in code}

    # both weeks for the same managers, so the change is what today's top 1,000 did, not a change of who's in it
    for gw in ([cur - 1] if cur > 1 else []) + [cur]:
        c = _count(ids, gw)
        gws[str(gw)] = dict(n=c["n"], own=to_codes(c["own"]), cap=to_codes(c["cap"]), freehit=c["freehit"],
                            failed=c["failed"], cutoff=entries[-1][1], top=entries[0][1],
                            at=dt.datetime.now(dt.timezone.utc).isoformat(),
                            partial=c["failed"] > len(ids) * 0.1,
                            # the earlier week is the same managers' squads then, for the week-on-week change
                            same_managers_as=cur if gw != cur else None)
        print(f"Top managers GW{gw}: {c['n']} squads, {c['freehit']} free hits left out, {c['failed']} failed")
    # keep the last 8 gameweeks
    for k in sorted(gws, key=int)[:-8]:
        gws.pop(k)
    CACHE.write_text(json.dumps(cache, separators=(",", ":"), sort_keys=True))
    return cache


def summary(cache, classic_boot):
    """{player code: dict(own %, change in points, captain %, overall %)} for the latest gameweek, plus meta."""
    gws = (cache or {}).get("gws") or {}
    if not gws:
        return {}, None
    cur = max(gws, key=int)
    now, prev = gws[cur], gws.get(str(int(cur) - 1))
    n = max(now["n"], 1)
    pn = max(prev["n"], 1) if prev else None
    overall = {str(e["code"]): float(e.get("selected_by_percent") or 0) for e in (classic_boot or {}).get("elements", [])}
    out = {}
    for c in set(now["own"]) | set(prev["own"] if prev else {}):
        o = now["own"].get(c, 0) / n * 100
        out[c] = dict(own=round(o, 1),
                      chg=round(o - prev["own"].get(c, 0) / pn * 100, 1) if prev else None,
                      cap=round(now["cap"].get(c, 0) / n * 100, 1),
                      all=overall.get(c))
    meta = dict(gw=int(cur), n=now["n"], cutoff=now.get("cutoff"), top=now.get("top"), has_prev=bool(prev),
                partial=now.get("partial"), at=now.get("at"))
    return out, meta
