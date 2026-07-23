"""
Bot Cooks EV add-on -- FULLY SELF-CONTAINED module.

Drop this single file into the Make a Parlay repo and add THREE lines to
bot.py (see instructions delivered with this file). It never touches or
imports any existing parlay code: it has its own Odds API calls, its own
grading, its own sqlite database file, its own channel config, and
collision-proof command names (/setevchannel, /topev, /evcheck,
/evrecord, /postevpick).

What it does:
- 5pm ET nightly: scans every game across 6 prop markets x 4 books,
  posts the single highest no-vig-consensus +EV play as a tracked 1U pick
- 1am ET nightly: grades pending picks against real MLB box scores,
  posts a recap with the running season unit ledger
- /topev: live top 5 EV plays on demand
- /evcheck <player> [market]: live EV lookup for one player's props
- /evrecord: season W-L-P + net units
- /postevpick: admin manual trigger of tonight's pick

EV methodology: consensus no-vig fair probability averaged across every
book offering BOTH sides of the same line; EV% for each book's actual
price against that consensus. Market-consensus edge -- NOT a projection
model; no invented numbers, real prices only.

HONESTY FLAGS carried over from the standalone build:
- mlb boxscore stat field names follow the structure already working in
  the project's other bots but are unverified against a live completed
  game; if the 1am grading errors or grades wrong, a field name in
  _STAT_EXTRACTORS is the first place to look.
- API key is read from ODDS_API_KEY, THE_ODDS_API_KEY, or ODDSAPI_KEY
  (first one set wins) so whatever the parlay bot already calls its key
  is likely picked up automatically.
"""
import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone, time as dtime

import requests
import discord
from discord import app_commands
from discord.ext import tasks

log = logging.getLogger("ev_features")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SPORT_KEY = "baseball_mlb"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Sharp 4 (consensus anchors) + soft US books (EV targets). Up to 10
# bookmakers bill as ONE unit at The Odds API, so widening costs nothing.
BOOKMAKERS = ["fanduel", "draftkings", "betmgm", "williamhill_us",
              "betrivers", "espnbet", "fanatics", "hardrockbet", "ballybet"]
# Fair value comes ONLY from these (by API response title). Soft books get
# EV-checked against the sharp consensus but never pollute it -- a stale
# BetRivers line can be a betting target, never a fair-value input.
SHARP_BOOK_TITLES = {"FanDuel", "DraftKings", "BetMGM", "Caesars"}

MARKETS = [
    {"key": "batter_hits", "label": "Hits"},
    {"key": "batter_total_bases", "label": "Total Bases"},
    {"key": "batter_hits_runs_rbis", "label": "H+R+RBI"},
    {"key": "pitcher_strikeouts", "label": "Strikeouts"},
    {"key": "pitcher_hits_allowed", "label": "Hits Allowed"},
    {"key": "pitcher_outs", "label": "Outs"},
    # Game-level markets (July 23): any MLB play qualifies, not just props.
    {"key": "h2h", "label": "Moneyline"},
    {"key": "spreads", "label": "Run Line"},
    {"key": "totals", "label": "Game Total"},
]
MARKET_LABEL = {m["key"]: m["label"] for m in MARKETS}
PROP_MARKET_KEYS = {"batter_hits", "batter_total_bases", "batter_hits_runs_rbis",
                     "pitcher_strikeouts", "pitcher_hits_allowed", "pitcher_outs"}
GAME_MARKET_KEYS = {"h2h", "spreads", "totals"}

# Scheduled picks only post plays clearing this bar; below it = "no bet".
EV_MIN_PCT = float(os.getenv("EV_MIN_PCT", "5.0"))
# Strong-edge alert: any play at/above this EV posts immediately when found.
EV_ALERT_MIN_PCT = float(os.getenv("EV_ALERT_MIN_PCT", "5.0"))
# Dedicated alert scan cadence. Cost-aware default: each scan ~9 credits x
# ~15 events ~ 135 credits; every 120 min ~ 12 scans/day ~ 1.6K credits/day
# (~49K/month) on top of the two pick scans -- fits a 100K/month plan with
# room for the site's other usage. Tighten at your own credit budget.
EV_ALERT_POLL_MINUTES = int(os.getenv("EV_ALERT_POLL_MINUTES", "120"))
# Consensus reliability (July 23, after the Bogaerts false alert): a line
# only counts as having a real consensus when >=2 books quote BOTH sides
# AND their de-vigged fair values agree within this many percentage
# points. One stale book among the both-sided quotes can otherwise
# manufacture a fake 20%+ "edge" on prices that are just the market.
EV_MIN_CONSENSUS_BOOKS = int(os.getenv("EV_MIN_CONSENSUS_BOOKS", "2"))
EV_MAX_FAIR_SPREAD = float(os.getenv("EV_MAX_FAIR_SPREAD", "6.0"))

def _parse_pick_times() -> list[dtime]:
    """EV_PICK_TIMES_UTC: comma-separated HH:MM UTC times. Default
    15:30,21:00 = 11:30 AM + 5:00 PM ET during EDT (UTC-4)."""
    raw = os.getenv("EV_PICK_TIMES_UTC", "15:30,21:00")
    times = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(":")
        times.append(dtime(hour=int(hh), minute=int(mm or 0)))
    return times or [dtime(hour=21, minute=0)]


EV_PICK_TIMES = _parse_pick_times()
NIGHTLY_RECAP_HOUR_UTC = int(os.getenv("NIGHTLY_RECAP_HOUR_UTC", "5"))  # 1am ET (EDT)


def _play_desc(r: dict) -> str:
    """Human bet string per market type: 'Over 1.5' (props), 'Over 8.5'
    (total), 'Yankees -1.5' (run line -- side already carries the number),
    'Yankees ML' (moneyline)."""
    mk = r["market_key"]
    if mk == "h2h":
        return f"{r['side']} ML"
    if mk == "spreads":
        return r["side"]
    return f"{r['side']} {r['point']}"


def _api_key() -> str | None:
    for name in ("ODDS_API_KEY", "THE_ODDS_API_KEY", "ODDSAPI_KEY"):
        v = os.getenv(name)
        if v:
            return v
    return None


def _et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Storage -- OWN sqlite file, never the parlay bot's database
# ---------------------------------------------------------------------------
import sqlite3
from contextlib import contextmanager


def _default_db_path() -> str:
    explicit = os.getenv("EV_DB_PATH")
    if explicit:
        return explicit
    base = os.getenv("DB_PATH")  # ride alongside the parlay DB on the same volume
    if base:
        d = os.path.dirname(base)
        if d:
            return os.path.join(d, "ev_picks.db")
    return "ev_picks.db"


EV_DB_PATH = _default_db_path()


@contextmanager
def _conn():
    conn = sqlite3.connect(EV_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ev_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ev_picks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_date TEXT,
                market_key TEXT,
                market_label TEXT,
                matchup TEXT,
                player TEXT,
                point REAL,
                side TEXT,
                book TEXT,
                price INTEGER,
                fair_prob_pct REAL,
                ev_pct REAL,
                result TEXT DEFAULT 'pending',
                actual_value REAL,
                profit_units REAL,
                message_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ev_alerts (
                alert_date TEXT,
                market_key TEXT,
                player TEXT,
                point REAL,
                side TEXT,
                book TEXT,
                PRIMARY KEY (alert_date, market_key, player, point, side, book)
            )
        """)


def _set_config(key: str, value: str):
    with _conn() as c:
        c.execute(
            "INSERT INTO ev_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _get_config(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM ev_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def _save_pick(pick_date: str, row: dict, message_id: str = None) -> int:
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO ev_picks (pick_date, market_key, market_label, matchup, player, point,
                                   side, book, price, fair_prob_pct, ev_pct, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pick_date, row["market_key"], row["market_label"], row["matchup"], row["player"],
             row["point"], row["side"], row["book"], row["price"], row["fair_prob_pct"],
             row["ev_pct"], message_id),
        )
        return cur.lastrowid


def _alert_already_sent(alert_date, market_key, player, point, side, book) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM ev_alerts WHERE alert_date=? AND market_key=? AND player=? "
            "AND point IS ? AND side=? AND book=?",
            (alert_date, market_key, player, point, side, book),
        ).fetchone()
        return row is not None


def _mark_alert_sent(alert_date, market_key, player, point, side, book):
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO ev_alerts (alert_date, market_key, player, point, side, book) "
            "VALUES (?,?,?,?,?,?)",
            (alert_date, market_key, player, point, side, book),
        )


def _get_todays_picked_keys(pick_date: str) -> set:
    """(player, market, point, side) combos already picked today -- the
    second daily post skips these so one bet never books twice in the
    ledger; it takes the best REMAINING edge instead."""
    with _conn() as c:
        rows = c.execute(
            "SELECT player, market_key, point, side FROM ev_picks WHERE pick_date = ?",
            (pick_date,),
        ).fetchall()
    return {(r["player"], r["market_key"], r["point"], r["side"]) for r in rows}


def _get_pending_picks(before_date: str = None) -> list[dict]:
    with _conn() as c:
        if before_date:
            rows = c.execute(
                "SELECT * FROM ev_picks WHERE result = 'pending' AND pick_date <= ?", (before_date,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM ev_picks WHERE result = 'pending'").fetchall()
    return [dict(r) for r in rows]


def _grade_pick_row(pick_id: int, result: str, actual_value, profit_units: float):
    with _conn() as c:
        c.execute(
            "UPDATE ev_picks SET result = ?, actual_value = ?, profit_units = ? WHERE id = ?",
            (result, actual_value, profit_units, pick_id),
        )


def _get_season_record() -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT result, profit_units FROM ev_picks WHERE result IN ('win','loss','push')"
        ).fetchall()
    wins = sum(1 for r in rows if r["result"] == "win")
    losses = sum(1 for r in rows if r["result"] == "loss")
    pushes = sum(1 for r in rows if r["result"] == "push")
    net_units = sum(r["profit_units"] or 0 for r in rows)
    return {"wins": wins, "losses": losses, "pushes": pushes, "net_units": round(net_units, 2)}


# ---------------------------------------------------------------------------
# EV math (verified: zero EV at fair price, positive for softer lines)
# ---------------------------------------------------------------------------
def _implied_prob(price: float) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def _profit_per_unit(price: float) -> float:
    return price / 100 if price > 0 else 100 / -price


def _ev_percent(fair_prob: float, price: float) -> float:
    return (fair_prob * _profit_per_unit(price) - (1 - fair_prob)) * 100


# ---------------------------------------------------------------------------
# Odds API
# ---------------------------------------------------------------------------
def _fetch_todays_events(api_key: str) -> list[dict]:
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events",
        params={"apiKey": api_key}, timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_event_odds(api_key: str, event_id: str) -> dict:
    resp = requests.get(
        f"{ODDS_API_BASE}/sports/{SPORT_KEY}/events/{event_id}/odds",
        params={
            "apiKey": api_key,
            "bookmakers": ",".join(BOOKMAKERS),
            "oddsFormat": "american",
            "markets": ",".join(m["key"] for m in MARKETS),
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _extract_priced_lines(event_data: dict, matchup: str) -> list[dict]:
    """Groups every two-sided line so no-vig consensus can be computed:
    - props: keyed (market, player, point), sides Over/Under
    - totals: keyed (market, point), sides Over/Under
    - spreads: keyed (market, |point|), sides like 'Yankees -1.5' (label
      carries the signed number so it stays consistent across books)
    - h2h: keyed (market,), sides = the two team names"""
    groups: dict[tuple, dict] = {}
    for book in event_data.get("bookmakers", []):
        for market in book.get("markets", []):
            mk = market["key"]
            label = MARKET_LABEL.get(mk)
            if not label:
                continue
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                name = outcome.get("name")
                if price is None or not name:
                    continue
                point = outcome.get("point")

                if mk in PROP_MARKET_KEYS:
                    player = outcome.get("description", "Unknown")
                    if point is None:
                        continue
                    key = (mk, player, point)
                    side = name
                    row_point = point
                elif mk == "totals":
                    if point is None:
                        continue
                    key = (mk, "", point)
                    side = name  # Over/Under
                    row_point = point
                    player = ""
                elif mk == "spreads":
                    if point is None:
                        continue
                    key = (mk, "", abs(point))
                    side = f"{name} {point:+g}"
                    row_point = point
                    player = ""
                else:  # h2h
                    key = (mk, "", None)
                    side = name  # team name
                    row_point = None
                    player = ""

                if key not in groups:
                    groups[key] = {
                        "market_key": mk, "market_label": label,
                        "player": player, "matchup": matchup, "books": {},
                        "side_points": {},
                    }
                groups[key]["books"].setdefault(book["title"], {})[side] = price
                groups[key]["side_points"][side] = row_point
    return list(groups.values())


def _compute_ev_rows(groups: list[dict]) -> list[dict]:
    """Per-book no-vig fair from any book carrying BOTH sides of the same
    line (Over/Under, both teams, both run-line sides); consensus = average
    across those books; EV per individual book price vs consensus."""
    rows = []
    for g in groups:
        books = g["books"]
        per_book_fair: dict[str, dict[str, float]] = {}
        side_names = set()
        for prices in books.values():
            side_names.update(prices.keys())
        side_names = sorted(side_names)
        if len(side_names) != 2:
            continue  # need exactly a two-sided market to de-vig
        s1, s2 = side_names

        for book_name, prices in books.items():
            if book_name not in SHARP_BOOK_TITLES:
                continue  # soft books are EV targets, never consensus inputs
            if s1 not in prices or s2 not in prices:
                continue
            i1, i2 = _implied_prob(prices[s1]), _implied_prob(prices[s2])
            total = i1 + i2
            if total <= 0:
                continue
            per_book_fair[book_name] = {s1: i1 / total, s2: i2 / total}

        if not per_book_fair:
            continue
        consensus = {
            s: sum(f[s] for f in per_book_fair.values()) / len(per_book_fair)
            for s in (s1, s2)
        }
        fairs_s1 = [f[s1] for f in per_book_fair.values()]
        fair_spread = round((max(fairs_s1) - min(fairs_s1)) * 100, 2)
        n_consensus = len(per_book_fair)

        for book_name, prices in books.items():
            for side, price in prices.items():
                fair_prob = consensus.get(side)
                if fair_prob is None:
                    continue
                rows.append({
                    "market_key": g["market_key"], "market_label": g["market_label"],
                    "matchup": g["matchup"], "player": g["player"],
                    "point": g["side_points"].get(side),
                    "side": side, "book": book_name, "price": price,
                    "fair_prob_pct": round(fair_prob * 100, 2),
                    "ev_pct": round(_ev_percent(fair_prob, price), 2),
                    "consensus_books": n_consensus,
                    "fair_spread": fair_spread,
                })
    rows.sort(key=lambda r: -r["ev_pct"])
    return rows


async def _scan_all_ev(status_cb=None) -> list[dict]:
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("No Odds API key found (checked ODDS_API_KEY / THE_ODDS_API_KEY / ODDSAPI_KEY).")

    events = await asyncio.to_thread(_fetch_todays_events, api_key)
    if not events:
        return []

    all_rows = []
    for i, ev in enumerate(events):
        matchup = f"{ev['away_team']} @ {ev['home_team']}"
        if status_cb:
            await status_cb(f"Checking {i+1}/{len(events)}: {matchup}...")
        try:
            event_data = await asyncio.to_thread(_fetch_event_odds, api_key, ev["id"])
        except Exception as e:
            log.warning("Odds fetch failed for %s: %s", matchup, e)
            continue
        all_rows.extend(_compute_ev_rows(_extract_priced_lines(event_data, matchup)))
        await asyncio.sleep(0.15)

    all_rows.sort(key=lambda r: -r["ev_pct"])
    return all_rows


# ---------------------------------------------------------------------------
# Grading vs real MLB box scores
# ---------------------------------------------------------------------------
def _outs_from_ip(ip_str) -> int:
    if not ip_str:
        return 0
    try:
        whole, _, partial = str(ip_str).partition(".")
        return int(whole) * 3 + int(partial or 0)
    except (ValueError, TypeError):
        return 0


_STAT_EXTRACTORS = {
    "batter_hits": ("batting", lambda s: s.get("hits", 0)),
    "batter_total_bases": ("batting", lambda s: s.get("totalBases", 0)),
    "batter_hits_runs_rbis": ("batting", lambda s: s.get("hits", 0) + s.get("runs", 0) + s.get("rbi", 0)),
    "pitcher_strikeouts": ("pitching", lambda s: s.get("strikeOuts", 0)),
    "pitcher_hits_allowed": ("pitching", lambda s: s.get("hits", 0)),
    "pitcher_outs": ("pitching", lambda s: s.get("outs", 0) if "outs" in s else _outs_from_ip(s.get("inningsPitched"))),
}


def _get_mlb_games(date_str: str) -> list[dict]:
    resp = requests.get(f"{MLB_BASE}/schedule", params={"sportId": 1, "date": date_str}, timeout=15)
    resp.raise_for_status()
    games = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "status": (g.get("status") or {}).get("abstractGameState"),
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
                "home_score": (g["teams"]["home"] or {}).get("score"),
                "away_score": (g["teams"]["away"] or {}).get("score"),
            })
    return games


def _grade_game_market(pick: dict, game: dict):
    """Grades h2h/spreads/totals from the final score. Returns
    (result, actual_value) or None if it can't be determined."""
    hs, as_ = game.get("home_score"), game.get("away_score")
    if hs is None or as_ is None:
        return None
    home, away = game["home_team"], game["away_team"]
    mk, side = pick["market_key"], pick["side"]

    if mk == "totals":
        total = hs + as_
        return _grade(pick["point"], side, total), float(total)

    if mk == "h2h":
        picked = side
        if picked not in (home, away):
            return None
        margin = (hs - as_) if picked == home else (as_ - hs)
        return ("win" if margin > 0 else "loss"), float(margin)

    if mk == "spreads":
        picked = side.rsplit(" ", 1)[0]  # 'New York Yankees -1.5' -> team
        if picked not in (home, away):
            return None
        margin = (hs - as_) if picked == home else (as_ - hs)
        adjusted = margin + (pick["point"] or 0)
        if adjusted > 0:
            return "win", float(margin)
        if adjusted == 0:
            return "push", float(margin)
        return "loss", float(margin)
    return None


def _get_boxscore(game_pk: int) -> dict:
    resp = requests.get(f"{MLB_BASE}/game/{game_pk}/boxscore", timeout=15)
    resp.raise_for_status()
    return resp.json()


def _find_player_stat(boxscore: dict, player_name: str, market_key: str):
    extractor = _STAT_EXTRACTORS.get(market_key)
    if not extractor:
        return None
    stat_group, extract_fn = extractor
    for side in ("home", "away"):
        players = (boxscore.get("teams", {}).get(side, {}) or {}).get("players", {})
        for p in players.values():
            if (p.get("person", {}) or {}).get("fullName") != player_name:
                continue
            stats = (p.get("stats", {}) or {}).get(stat_group, {})
            if not stats:
                continue
            return extract_fn(stats)
    return None


def _grade(point: float, side: str, actual: float) -> str:
    if actual == point:
        return "push"
    if side == "Over":
        return "win" if actual > point else "loss"
    return "win" if actual < point else "loss"


# ---------------------------------------------------------------------------
# Discord surface: embeds, commands, tasks
# ---------------------------------------------------------------------------
_client: discord.Client | None = None


def _build_pick_embed(row: dict) -> discord.Embed:
    embed = discord.Embed(title="🎯 Tonight's Most +EV Play", color=discord.Color.gold())
    embed.add_field(name="Player", value=row["player"] or row["matchup"], inline=True)
    embed.add_field(name="Market", value=row["market_label"], inline=True)
    embed.add_field(name="Matchup", value=row["matchup"], inline=False)
    embed.add_field(name="The Play", value=f"{_play_desc(row)} @ {row['book']} ({row['price']:+d})", inline=False)
    embed.add_field(name="Consensus Fair Price", value=f"{row['fair_prob_pct']}% implied", inline=True)
    embed.add_field(name="EV", value=f"{row['ev_pct']:+.2f}%", inline=True)
    embed.add_field(name="Stake", value="1U", inline=True)
    embed.set_footer(text="EV = this book vs. consensus no-vig fair price (FD/DK/MGM/Caesars). Real market prices only — not a projection model.")
    return embed


async def _post_nightly_pick() -> str:
    channel_id = _get_config("ev_channel_id")
    if not channel_id:
        return "No EV channel set -- run /setevchannel first."
    channel = _client.get_channel(int(channel_id))
    if channel is None:
        return "Configured EV channel not found."

    try:
        rows = await _scan_all_ev()
    except Exception as e:
        log.error("Nightly EV scan failed: %s", e)
        return f"Scan failed: {e}"

    if not rows:
        await channel.send("No props available across tonight's slate to pick from.")
        return "No rows found."

    # Strong-edge alerts ride this scan for free before any filtering.
    try:
        await _check_and_post_alerts(rows, channel)
    except Exception as e:
        log.error("Alert check during pick scan failed: %s", e)

    already = _get_todays_picked_keys(_et_date_str(0))
    rows = [r for r in rows if (r["player"], r["market_key"], r["point"], r["side"]) not in already]

    # The honest-tracker gate: no play clearing EV_MIN_PCT means NO BET --
    # never force a vig-priced ticket into the ledger just to post something.
    rows = [r for r in rows if r["ev_pct"] >= EV_MIN_PCT and _consensus_reliable(r)]
    if not rows:
        await channel.send(
            f"📭 No play clears the **+{EV_MIN_PCT:g}% EV** bar this scan — no bet. "
            f"The ledger only takes real edges."
        )
        return "No play cleared the EV threshold."

    best = rows[0]
    try:
        message = await channel.send(embed=_build_pick_embed(best))
    except Exception as e:
        log.error("Failed to post nightly pick: %s", e)
        return f"Post failed: {e}"

    _save_pick(_et_date_str(0), best, message_id=str(message.id))
    log.info("Posted nightly EV pick: %s %s %s (EV %.2f%%)", best["player"], best["side"], best["point"], best["ev_pct"])
    return f"Posted: {best['player']} {best['side']} {best['point']} (EV {best['ev_pct']:+.2f}%)"


async def _grade_pending():
    channel_id = _get_config("ev_channel_id")
    channel = _client.get_channel(int(channel_id)) if channel_id else None

    pending = _get_pending_picks(before_date=_et_date_str(0))
    if not pending:
        return

    games_cache: dict[str, list[dict]] = {}
    graded = []

    for pick in pending:
        if pick["pick_date"] not in games_cache:
            try:
                games_cache[pick["pick_date"]] = await asyncio.to_thread(_get_mlb_games, pick["pick_date"])
            except Exception as e:
                log.error("Couldn't fetch MLB games for %s: %s", pick["pick_date"], e)
                games_cache[pick["pick_date"]] = []

        away, home = pick["matchup"].split(" @ ")
        game = next(
            (g for g in games_cache[pick["pick_date"]]
             if g["away_team"] == away and g["home_team"] == home),
            None,
        )
        if game is None:
            log.warning("Couldn't match game for pick %s (%s) -- leaving pending", pick["id"], pick["matchup"])
            continue
        if game.get("status") != "Final":
            log.info("Game not Final yet for pick %s (%s) -- leaving pending", pick["id"], pick["matchup"])
            continue

        if pick["market_key"] in GAME_MARKET_KEYS:
            graded_game = _grade_game_market(pick, game)
            if graded_game is None:
                log.warning("Couldn't grade game-market pick %s -- leaving pending", pick["id"])
                continue
            result, actual = graded_game
        else:
            try:
                boxscore = await asyncio.to_thread(_get_boxscore, game["game_pk"])
                actual = _find_player_stat(boxscore, pick["player"], pick["market_key"])
            except Exception as e:
                log.error("Grading fetch failed for pick %s: %s", pick["id"], e)
                continue
            if actual is None:
                log.warning("No stat for %s (%s) in game %s -- leaving pending", pick["player"], pick["market_key"], game["game_pk"])
                continue
            result = _grade(pick["point"], pick["side"], actual)
        profit = _profit_per_unit(pick["price"]) if result == "win" else (-1.0 if result == "loss" else 0.0)
        _grade_pick_row(pick["id"], result, actual, profit)
        graded.append((pick, result, actual, profit))
        log.info("Graded EV pick %s: %s (actual=%s) %.2fU", pick["id"], result, actual, profit)

    if channel and graded:
        lines = []
        for pick, result, actual, profit in graded:
            emoji = {"win": "✅", "loss": "❌", "push": "➖"}[result]
            lines.append(
                f"{emoji} **{pick['player'] or pick['matchup']}** {_play_desc(pick)} {pick['market_label']} "
                f"(actual: {actual:g}) — {profit:+.2f}U"
            )
        record = _get_season_record()
        embed = discord.Embed(
            title="🌙 EV Pick — Overnight Recap",
            description="\n".join(lines) + f"\n\nSeason: **{record['wins']}-{record['losses']}-{record['pushes']}** ({record['net_units']:+.2f}U)",
            color=discord.Color.dark_gold(),
        )
        try:
            await channel.send(embed=embed)
        except Exception as e:
            log.error("Failed to post EV recap: %s", e)


def _consensus_reliable(r: dict) -> bool:
    return (r.get("consensus_books", 0) >= EV_MIN_CONSENSUS_BOOKS
            and r.get("fair_spread", 999) <= EV_MAX_FAIR_SPREAD)


async def _check_and_post_alerts(rows: list[dict], channel):
    """ONE alert per line (not per book): fires when the best book on a
    line clears EV_ALERT_MIN_PCT, and shows the FULL board across books so
    a split market / stale line is visible at a glance. Deduped per
    (day, line) with a '*' book sentinel."""
    if channel is None:
        return
    today = _et_date_str(0)
    board: dict[tuple, list] = {}
    for r in rows:
        board.setdefault((r["market_key"], r["player"], r["point"], r["side"]), []).append(r)

    for (mk, player, point, side), group in board.items():
        best = max(group, key=lambda r: r["ev_pct"])
        if best["ev_pct"] < EV_ALERT_MIN_PCT:
            continue
        if not _consensus_reliable(best):
            log.info("Skipping alert on disputed line %s %s (books=%s, fair spread=%.1f pts)",
                     best["player"] or best["matchup"], _play_desc(best),
                     best.get("consensus_books"), best.get("fair_spread", -1))
            continue
        if _alert_already_sent(today, mk, player, point, side, "*"):
            continue
        prices_line = " • ".join(
            f"{g['book']} {g['price']:+d}" for g in sorted(group, key=lambda x: -x["ev_pct"])
        )
        embed = discord.Embed(
            title=f"🚨 {best['ev_pct']:+.1f}% EV ALERT",
            color=discord.Color.red(),
        )
        embed.add_field(name="Play", value=f"{_play_desc(best)} — {best['player'] or best['matchup']}", inline=False)
        embed.add_field(name="Market", value=best["market_label"], inline=True)
        embed.add_field(name="Best Price", value=f"{best['book']} ({best['price']:+d})", inline=True)
        embed.add_field(name="Consensus Fair", value=f"{best['fair_prob_pct']}%", inline=True)
        embed.add_field(name="Full Board", value=prices_line, inline=False)
        embed.set_footer(text=f"Edges this large usually mean the market is repricing (lineup news) or a line is stale/suspended — check the board split and verify it's still live. Alert bar: {EV_ALERT_MIN_PCT:g}%+")
        try:
            await channel.send(embed=embed)
            _mark_alert_sent(today, mk, player, point, side, "*")
            log.info("EV alert posted: %s %s best @ %s (%.2f%%)", best["player"] or best["matchup"], _play_desc(best), best["book"], best["ev_pct"])
        except Exception as e:
            log.error("Failed to post EV alert: %s", e)


async def _run_alert_scan():
    channel_id = _get_config("ev_channel_id")
    if not channel_id:
        return
    channel = _client.get_channel(int(channel_id))
    if channel is None:
        return
    try:
        rows = await _scan_all_ev()
    except Exception as e:
        log.error("Alert scan failed: %s", e)
        return
    await _check_and_post_alerts(rows, channel)


@tasks.loop(minutes=EV_ALERT_POLL_MINUTES)
async def _ev_alert_task():
    try:
        await _run_alert_scan()
    except Exception as e:
        log.error("EV alert task failed, will retry next cycle: %s", e)


@_ev_alert_task.before_loop
async def _before_alerts():
    await _client.wait_until_ready()


@tasks.loop(time=EV_PICK_TIMES)
async def _nightly_pick_task():
    try:
        await _post_nightly_pick()
    except Exception as e:
        log.error("nightly EV pick task failed, will retry tomorrow: %s", e)


@_nightly_pick_task.before_loop
async def _before_pick():
    await _client.wait_until_ready()


@tasks.loop(time=dtime(hour=NIGHTLY_RECAP_HOUR_UTC, minute=0))
async def _nightly_recap_task():
    try:
        await _grade_pending()
    except Exception as e:
        log.error("nightly EV recap task failed, will retry tomorrow: %s", e)


@_nightly_recap_task.before_loop
async def _before_recap():
    await _client.wait_until_ready()


# ---------------------------------------------------------------------------
# Public wiring surface -- the ONLY two functions bot.py needs to call
# ---------------------------------------------------------------------------
def register_commands(tree: app_commands.CommandTree):
    """Call once, anywhere before your existing tree.sync()."""
    _init_db()

    async def _topev(interaction: discord.Interaction):
        await interaction.response.defer()
        msg = await interaction.followup.send("Scanning tonight's slate across 6 markets, 4 books...")

        async def status(text):
            try:
                await msg.edit(content=text)
            except Exception:
                pass

        try:
            rows = await _scan_all_ev(status_cb=status)
        except Exception as e:
            await msg.edit(content=f"Couldn't scan odds: {e}")
            return
        if not rows:
            await msg.edit(content="No props found for today's slate yet -- try closer to game time.")
            return
        lines = [
            f"**{i+1}. {r['player'] or r['matchup']}** ({r['market_label']}) — {_play_desc(r)} @ {r['book']} "
            f"({r['price']:+d}) — EV **{r['ev_pct']:+.2f}%**"
            + ("" if _consensus_reliable(r) else " ⚠️ *disputed line — books disagree on fair value*")
            for i, r in enumerate(rows[:5])
        ]
        embed = discord.Embed(title="🔝 Top 5 EV Plays Right Now", description="\n".join(lines), color=discord.Color.purple())
        embed.set_footer(text="Sharp-book consensus (FD/DK/MGM/Caesars) • soft books scanned as targets.")
        await msg.edit(content=None, embed=embed)

    async def _evcheck(interaction: discord.Interaction, player_name: str):
        await interaction.response.defer()
        msg = await interaction.followup.send(f"Scanning odds for {player_name}...")

        async def status(text):
            try:
                await msg.edit(content=text)
            except Exception:
                pass

        try:
            rows = await _scan_all_ev(status_cb=status)
        except Exception as e:
            await msg.edit(content=f"Couldn't scan odds: {e}")
            return
        matches = [r for r in rows if player_name.lower() in r["player"].lower()]
        if not matches:
            await msg.edit(content=f"No current props found for '{player_name}'.")
            return
        lines = [
            f"**{r['market_label']}**: {_play_desc(r)} @ {r['book']} ({r['price']:+d}) — EV **{r['ev_pct']:+.2f}%**"
            + ("" if _consensus_reliable(r) else " ⚠️ *disputed*")
            for r in matches[:10]
        ]
        embed = discord.Embed(title=f"{matches[0]['player']} — Current Prop EV", description="\n".join(lines), color=discord.Color.blue())
        embed.set_footer(text="Sharp-book consensus (FD/DK/MGM/Caesars) • soft books scanned as targets.")
        await msg.edit(content=None, embed=embed)

    tree.add_command(app_commands.Command(
        name="topev", description="Show the top 5 highest-EV plays right now, live",
        callback=_topev))
    tree.add_command(app_commands.Command(
        name="evcheck", description="Check current EV on a specific player's props",
        callback=_evcheck))


def start_tasks(client: discord.Client):
    """July 23: Mike disabled ALL auto-posting -- no scheduled picks, no
    alert loop, no recap. EV is on-demand only via /topev and /evcheck.
    This stays a valid no-op so bot.py's wiring lines don't change."""
    global _client
    _client = client
    log.info("EV features: ON-DEMAND ONLY (auto-posts disabled) -- /topev and /evcheck active")
