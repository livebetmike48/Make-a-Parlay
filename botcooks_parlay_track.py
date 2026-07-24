"""
Parlay tracking -- every parlay the bot posts gets recorded, graded from
real MLB boxscores, and recapped BY CATEGORY at a flat 1U.

Self-contained like ev_features: own sqlite, own MLB calls, own commands.
Wiring in bot.py is two lines (register_commands, start_tasks) plus one
record() call per command.

House rules:
  - Flat 1U per parlay, straight from the price the bot actually showed.
    No stake models, no invented sizing.
  - A parlay is graded ONLY when every leg's game is Final. Anything
    ungraded stays pending forever rather than guessing.
  - Legs are graded from the real boxscore (hits, HR, strikeouts, runs).
    A leg the boxscore can't resolve leaves the whole parlay pending and
    says so -- never a silent win.
  - Recaps report what happened. They are not picks.

Railway (parlay service):
  PARLAY_DB_PATH        -- sqlite path; put it on the volume (e.g.
                           /data/parlay_track.db) or the ledger resets
  PARLAY_RECAP_TIME_UTC -- daily recap post time, default 13:30 (9:30a ET)
"""
import os
import time
import sqlite3
import logging
import asyncio
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
import discord
from discord import app_commands

log = logging.getLogger("parlay_track")

DB_PATH = os.getenv("PARLAY_DB_PATH", "parlay_track.db")
RECAP_TIME_UTC = os.getenv("PARLAY_RECAP_TIME_UTC", "13:30")
MLB = "https://statsapi.mlb.com/api/v1"

CATEGORIES = {
    "hit": "🎯 Hit parlays",
    "hr": "💣 HR parlays",
    "streak": "🔥 Streak parlays",
    "k": "⚾ Strikeout parlays",
    "sgp": "🎰 Same-game parlays",
    "moneyline": "💰 Moneyline parlays",
    "totals": "📊 Totals parlays",
}


# ---------- storage ----------

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute("""CREATE TABLE IF NOT EXISTS parlays (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, date_et TEXT,
        category TEXT, requested_by TEXT, book TEXT, price INTEGER,
        n_legs INTEGER, graded INTEGER DEFAULT 0, won INTEGER, units REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS parlay_legs (
        parlay_id INTEGER, idx INTEGER, kind TEXT, name TEXT, team TEXT,
        game_pk INTEGER, point REAL, side TEXT, price INTEGER, book TEXT,
        result TEXT)""")
    return c


def et_date(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)
            + timedelta(days=offset_days)).strftime("%Y-%m-%d")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def record(category: str, legs: list[dict], price: int | None,
           book: str | None, requested_by: str | None = None) -> int | None:
    """Record one posted parlay. legs: [{kind, name, team, game_pk, point,
    side, price, book}] where kind is batter_hit | batter_hr | pitcher_k |
    moneyline | total. Returns the parlay id, or None if it can't be
    graded later (no game_pk on a leg = never record a phantom)."""
    if not legs or price is None:
        return None
    if any(not l.get("game_pk") or not l.get("kind") for l in legs):
        log.info("parlay_track: %s parlay not recorded (leg missing game_pk/kind)", category)
        return None
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO parlays (ts, date_et, category, requested_by, book, price,"
                " n_legs, graded, won, units) VALUES (?,?,?,?,?,?,?,0,NULL,NULL)",
                (int(time.time()), et_date(), category, requested_by, book, int(price), len(legs)))
            pid = cur.lastrowid
            c.executemany(
                "INSERT INTO parlay_legs (parlay_id, idx, kind, name, team, game_pk,"
                " point, side, price, book, result) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                [(pid, i, l["kind"], l.get("name"), l.get("team"), l["game_pk"],
                  l.get("point"), l.get("side"), l.get("price"), l.get("book"))
                 for i, l in enumerate(legs)])
        log.info("parlay_track: recorded %s parlay #%d (%d legs @ %+d)",
                 category, pid, len(legs), price)
        return pid
    except Exception as e:
        log.warning("parlay_track: record failed: %s", e)
        return None


# ---------- grading ----------

def _final_games(dates: set) -> dict:
    """{game_pk: True} for games that are Final on those ET dates."""
    finals = {}
    for d in dates:
        try:
            sched = requests.get(f"{MLB}/schedule", params={"sportId": 1, "date": d},
                                 timeout=20).json()
            for day in sched.get("dates", []):
                for g in day.get("games", []):
                    if (g.get("status") or {}).get("codedGameState") == "F":
                        finals[g["gamePk"]] = True
        except Exception as e:
            log.warning("parlay_track: schedule %s failed: %s", d, e)
    return finals


def _boxscore(game_pk: int) -> dict | None:
    try:
        return requests.get(f"{MLB}/game/{game_pk}/boxscore", timeout=20).json()
    except Exception as e:
        log.warning("parlay_track: boxscore %s failed: %s", game_pk, e)
        return None


def _find_player(box: dict, name: str) -> dict | None:
    target = _fold(name)
    last = target.split()[-1] if target else ""
    for side in ("home", "away"):
        players = (((box.get("teams") or {}).get(side) or {}).get("players") or {})
        for p in players.values():
            full = _fold(((p.get("person") or {}).get("fullName")))
            if full and (full == target or (last and full.endswith(last) and
                                            full.split()[0][:1] == target.split()[0][:1])):
                return p
    return None


def _team_runs(box: dict) -> dict:
    out = {}
    for side in ("home", "away"):
        t = ((box.get("teams") or {}).get(side)) or {}
        runs = ((((t.get("teamStats") or {}).get("batting")) or {}).get("runs"))
        out[side] = runs
        out[_fold((t.get("team") or {}).get("abbreviation"))] = runs
        out[_fold((t.get("team") or {}).get("name"))] = runs
    return out


def _grade_leg(leg: sqlite3.Row | tuple, box: dict) -> str | None:
    """'win' / 'loss' / None when the boxscore can't resolve it."""
    _, _, kind, name, team, _, point, side, _, _, _ = leg
    pt = point if point is not None else 0.5
    if kind in ("batter_hit", "batter_hr", "pitcher_k"):
        p = _find_player(box, name or "")
        if not p:
            return None
        stats = p.get("stats") or {}
        if kind == "pitcher_k":
            v = ((stats.get("pitching")) or {}).get("strikeOuts")
        else:
            bat = (stats.get("batting")) or {}
            v = bat.get("homeRuns") if kind == "batter_hr" else bat.get("hits")
        if v is None:
            return None
        return "win" if v > pt else "loss"
    runs = _team_runs(box)
    if kind == "moneyline":
        mine = runs.get(_fold(team or name or ""))
        theirs = [runs.get("home"), runs.get("away")]
        if mine is None or any(r is None for r in theirs):
            return None
        other = theirs[0] if theirs[1] == mine else theirs[1]
        if theirs[0] == theirs[1]:
            other = mine  # can't tell sides apart on equal scores
        return "win" if mine > other else "loss"
    if kind == "total":
        h, a = runs.get("home"), runs.get("away")
        if h is None or a is None:
            return None
        total = h + a
        if total == pt:
            return None  # push -- leave pending rather than fake a result
        over = total > pt
        return "win" if (over if (side or "over").lower() == "over" else not over) else "loss"
    return None


def american_to_decimal(price: int) -> float:
    return 1 + (price / 100 if price > 0 else 100 / abs(price))


def grade_pending(today: str | None = None) -> int:
    """Grade every recorded parlay from a past ET date whose games are all
    Final. Returns how many got graded."""
    today = today or et_date()
    with _conn() as c:
        rows = c.execute("SELECT id, price FROM parlays WHERE graded=0 AND date_et < ?",
                         (today,)).fetchall()
        if not rows:
            return 0
        legs_by_parlay = {}
        for pid, _ in rows:
            legs_by_parlay[pid] = c.execute(
                "SELECT parlay_id, idx, kind, name, team, game_pk, point, side, price, book,"
                " result FROM parlay_legs WHERE parlay_id=? ORDER BY idx", (pid,)).fetchall()
        dates = {r[0] for r in c.execute(
            "SELECT DISTINCT date_et FROM parlays WHERE graded=0 AND date_et < ?",
            (today,)).fetchall()}
    finals = _final_games(dates)
    boxes: dict = {}
    graded = 0
    for pid, price in rows:
        legs = legs_by_parlay[pid]
        if not all(finals.get(l[5]) for l in legs):
            continue
        results = []
        for l in legs:
            gpk = l[5]
            if gpk not in boxes:
                boxes[gpk] = _boxscore(gpk)
            box = boxes[gpk]
            results.append(_grade_leg(l, box) if box else None)
        if any(r is None for r in results):
            log.info("parlay_track: parlay #%d stays pending (a leg didn't resolve)", pid)
            continue
        won = all(r == "win" for r in results)
        units = round(american_to_decimal(price) - 1, 2) if won else -1.0
        with _conn() as c:
            for l, r in zip(legs, results):
                c.execute("UPDATE parlay_legs SET result=? WHERE parlay_id=? AND idx=?",
                          (r, pid, l[1]))
            c.execute("UPDATE parlays SET graded=1, won=?, units=? WHERE id=?",
                      (1 if won else 0, units, pid))
        graded += 1
    if graded:
        log.info("parlay_track: graded %d parlays", graded)
    return graded


# ---------- reporting ----------

def summary(days: int = 1, category: str | None = None) -> dict:
    """Per-category W-L and units over the window (graded parlays only)."""
    cutoff = et_date(-days)
    today = et_date()
    q = ("SELECT category, won, units, price, n_legs, id FROM parlays "
         "WHERE graded=1 AND date_et >= ? AND date_et < ?")
    args = [cutoff, today]
    if category:
        q += " AND category=?"
        args.append(category)
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
        pend = c.execute("SELECT COUNT(*) FROM parlays WHERE graded=0 AND date_et < ?",
                         (today,)).fetchone()[0]
    by_cat = {}
    for cat, won, units, price, n_legs, pid in rows:
        b = by_cat.setdefault(cat, {"w": 0, "l": 0, "units": 0.0, "n": 0, "best": None})
        b["n"] += 1
        b["units"] += units or 0
        if won:
            b["w"] += 1
            if b["best"] is None or price > b["best"][0]:
                b["best"] = (price, n_legs)
        else:
            b["l"] += 1
    for b in by_cat.values():
        b["units"] = round(b["units"], 2)
    return {"days": days, "by_category": by_cat, "pending": pend,
            "total_units": round(sum(b["units"] for b in by_cat.values()), 2),
            "total_n": sum(b["n"] for b in by_cat.values())}


def _recap_embed(s: dict, title_days: str) -> discord.Embed:
    total = s["total_units"]
    emb = discord.Embed(
        title=f"Parlay recap — {title_days}",
        color=0x4caf7d if total > 0 else (0xd7483a if total < 0 else 0x2f6fed))
    if not s["by_category"]:
        emb.description = ("Nothing graded in this window."
                           + (f" {s['pending']} parlay(s) waiting on final scores."
                              if s["pending"] else ""))
        return emb
    emb.description = (f"**{s['total_n']} parlays graded · {total:+g}u** (flat 1U each)"
                       + (f" · {s['pending']} pending" if s["pending"] else ""))
    for cat, label in CATEGORIES.items():
        b = s["by_category"].get(cat)
        if not b:
            continue
        line = f"**{b['w']}-{b['l']} · {b['units']:+g}u**"
        if b["best"]:
            line += f" · best hit {b['best'][0]:+d} ({b['best'][1]} legs)"
        emb.add_field(name=label, value=line, inline=False)
    emb.set_footer(text="Every parlay the bot posts is tracked at 1U and graded from real "
                        "box scores — wins and losses both. Research, not advice.")
    return emb


# ---------- commands ----------

def register_commands(tree: app_commands.CommandTree):
    @tree.command(name="parlayrecap",
                  description="How the bot's posted parlays did, by category")
    @app_commands.describe(days="How many days back (default 1 = yesterday)",
                           category="Limit to one category")
    @app_commands.choices(category=[
        app_commands.Choice(name=v, value=k) for k, v in CATEGORIES.items()])
    async def parlayrecap(interaction: discord.Interaction, days: int | None = None,
                          category: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        d = max(1, min(400, days or 1))
        await asyncio.to_thread(grade_pending)
        s = await asyncio.to_thread(summary, d, category.value if category else None)
        label = "yesterday" if d == 1 else f"last {d} days"
        await interaction.followup.send(embed=_recap_embed(s, label))

    @tree.command(name="parlayrecord",
                  description="Season record for the bot's posted parlays")
    async def parlayrecord(interaction: discord.Interaction):
        await interaction.response.defer()
        await asyncio.to_thread(grade_pending)
        s = await asyncio.to_thread(summary, 400, None)
        await interaction.followup.send(embed=_recap_embed(s, "season"))

    @tree.command(name="setrecapchannel",
                  description="Admin: post the daily parlay recap in this channel")
    async def setrecapchannel(interaction: discord.Interaction):
        if not getattr(interaction.user, "guild_permissions", None) or \
                not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return
        with _conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
            c.execute("INSERT OR REPLACE INTO settings VALUES ('recap_channel', ?)",
                      (str(interaction.channel_id),))
        await interaction.response.send_message(
            f"Daily parlay recap will post here at {RECAP_TIME_UTC} UTC.", ephemeral=True)


def _recap_channel_id() -> int | None:
    try:
        with _conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
            row = c.execute("SELECT v FROM settings WHERE k='recap_channel'").fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def start_tasks(client: discord.Client):
    async def _loop():
        try:
            hh, mm = (int(x) for x in RECAP_TIME_UTC.split(":"))
        except Exception:
            hh, mm = 13, 30
        log.info("parlay_track: daily recap task up (%02d:%02d UTC)", hh, mm)
        while True:
            now = datetime.now(timezone.utc)
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep((nxt - now).total_seconds())
            try:
                await asyncio.to_thread(grade_pending)
                cid = _recap_channel_id()
                if not cid:
                    log.info("parlay_track: no recap channel set -- skipping")
                    continue
                s = await asyncio.to_thread(summary, 1, None)
                if not s["by_category"]:
                    log.info("parlay_track: nothing graded yesterday -- staying quiet")
                    continue
                ch = client.get_channel(cid)
                if ch:
                    await ch.send(embed=_recap_embed(s, "yesterday"))
                    log.info("parlay_track: recap posted")
            except Exception as e:
                log.error("parlay_track: recap loop error: %s", e)

    client.loop.create_task(_loop())
