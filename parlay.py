"""
Parlay engine. Reuses the Statcast bot's VALIDATED modules verbatim
(statcast_api.py, leaderboard.py -- copy them from that repo unchanged; all
the classification rules validated against Savant live in there). This file
adds: today's slate, candidate shortlisting via Savant's own percentile
scores, and per-market leg evaluation from real pitch-level data.

No invented composite scores anywhere -- every leg is ranked by a real,
named metric and displays its supporting numbers.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import requests

import statcast_api
import leaderboard

MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Caches (reset naturally on redeploy; keyed by date so they roll daily)
_player_rows_cache: dict = {}
_starter_info_cache: dict = {}


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


def get_today_slate() -> list[dict]:
    """Today's games with probable starters and team abbreviations."""
    today = et_date_str(0)
    resp = requests.get(
        f"{MLB_BASE}/schedule",
        params={"sportId": 1, "date": today, "hydrate": "probablePitcher,team"},
        timeout=15,
    )
    resp.raise_for_status()
    slate = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                continue  # game already over -- no bets to make
            entry = {"game_pk": g["gamePk"], "teams": {}}
            for side in ("home", "away"):
                team = g["teams"][side]
                probable = team.get("probablePitcher") or {}
                entry["teams"][side] = {
                    "abbrev": (team.get("team") or {}).get("abbreviation", "?"),
                    "name": (team.get("team") or {}).get("name", "?"),
                    "starter_id": probable.get("id"),
                    "starter_name": probable.get("fullName"),
                }
            slate.append(entry)
    return slate


def get_starter_hand(pitcher_id: int) -> str | None:
    key = (pitcher_id, et_date_str(0))
    if key in _starter_info_cache:
        return _starter_info_cache[key]
    resp = requests.get(f"{MLB_BASE}/people/{pitcher_id}", timeout=15)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    hand = (people[0].get("pitchHand") or {}).get("code") if people else None
    _starter_info_cache[key] = hand
    return hand


def get_player_season_rows(player_id: int, is_pitcher: bool) -> list[dict]:
    """Season pitch-level rows, cached per player per day."""
    today = et_date_str(0)
    key = (player_id, is_pitcher, today)
    if key in _player_rows_cache:
        return _player_rows_cache[key]
    rows = statcast_api.fetch_statcast(player_id, is_pitcher, f"{today[:4]}-01-01", today)
    _player_rows_cache[key] = rows
    return rows


def shortlist_hitters(team_abbrevs: list[str], percentile_column: str, per_team: int = 2) -> list[dict]:
    """Top hitters on today's teams by one of Savant's own percentile
    scores (confirmed pre-computed 0-100). Returns [{name, team, pct}]."""
    out = []
    for abbrev in team_abbrevs:
        try:
            rows = leaderboard.fetch_leaderboard("batter", 2026, abbrev)
        except Exception:
            continue
        scored = []
        for r in rows:
            raw = r.get(percentile_column)
            if not raw:
                continue
            try:
                pct = float(raw)
            except ValueError:
                continue
            csv_name = r.get("player_name", "")
            parts = [p.strip() for p in csv_name.split(",")]
            display = f"{parts[1]} {parts[0]}" if len(parts) == 2 else csv_name
            scored.append({"name": display, "player_id": int(r["player_id"]), "team": abbrev, "pct": pct})
        scored.sort(key=lambda x: -x["pct"])
        out.extend(scored[:per_team])
    return out


# ---------- per-market real-stat computations (operate on raw rows) ----------

def _games_played(rows: list[dict]) -> set:
    return set(r.get("game_pk") for r in rows if r.get("game_pk"))

HIT_EVENTS = {"single", "double", "triple", "home_run"}


def hit_game_rate(rows: list[dict]) -> tuple[float, int]:
    """(share of games with >=1 hit, games played) -- a real frequency."""
    games = {}
    for r in rows:
        gpk = r.get("game_pk")
        if not gpk:
            continue
        games.setdefault(gpk, False)
        if r.get("events") in HIT_EVENTS:
            games[gpk] = True
    if not games:
        return 0.0, 0
    return sum(games.values()) / len(games), len(games)


def event_count_vs_hand(rows: list[dict], hand: str, events: set) -> int:
    return sum(1 for r in rows if r.get("p_throws") == hand and r.get("events") in events)


def top_pitch_matchup_line(batter_rows: list[dict], starter_rows: list[dict],
                            batter_side: str, starter_hand: str) -> str | None:
    """'Starter throws FF 45% to L -- batter xBA .335 vs FF from RHP'. Uses
    the same validated per-pitch machinery from the Statcast bot."""
    vs_side = [r for r in starter_rows if r.get("stand") == batter_side]
    mix = statcast_api.pitch_mix_breakdown(vs_side)
    if not mix:
        return None
    top_pitch = next(iter(mix))
    usage = mix[top_pitch]["usage_pct"]

    batter_vs_hand = [r for r in batter_rows if r.get("p_throws") == starter_hand]
    vs_pitch = statcast_api.vs_pitch_type_stats(batter_vs_hand, top_pitch)
    if not vs_pitch or "xba" not in vs_pitch:
        return f"Starter's top pitch to {batter_side}HB: {top_pitch} ({usage}%)"
    return (f"Starter throws {top_pitch} {usage}% to {batter_side}HB — "
            f"batter xBA {vs_pitch['xba']} vs {top_pitch} from {starter_hand}HP")


def evaluate_hit_leg(batter: dict, starter_id: int, starter_name: str, starter_hand: str,
                      market: str) -> dict | None:
    """One candidate leg with its real supporting stats. market: 'hit',
    'single', or 'hr' -- decides the frequency stat and the ranking metric."""
    try:
        batter_info = requests.get(f"{MLB_BASE}/people/{batter['player_id']}", timeout=15).json()
        bat_side_raw = ((batter_info.get("people") or [{}])[0].get("batSide") or {}).get("code", "R")
    except Exception:
        bat_side_raw = "R"
    batter_side = statcast_api.effective_bat_side(bat_side_raw, starter_hand)

    try:
        rows = get_player_season_rows(batter["player_id"], False)
    except Exception:
        return None
    if not rows:
        return None

    vs_hand = statcast_api.vs_handedness_stats(rows, "p_throws", starter_hand)
    if not vs_hand or vs_hand.get("pa", 0) < 40:
        return None  # too small a sample vs this hand to lean on

    rate, games = hit_game_rate(rows)
    leg = {
        "batter": batter["name"], "team": batter["team"],
        "starter": starter_name, "starter_hand": starter_hand,
        "pa_vs_hand": vs_hand["pa"],
        "avg_vs_hand": vs_hand.get("avg"), "xba_vs_hand": vs_hand.get("xba"),
        "xwoba_vs_hand": vs_hand.get("xwoba"),
        "hit_game_pct": round(rate * 100, 1), "games": games,
    }

    if market == "hr":
        hrs = event_count_vs_hand(rows, starter_hand, {"home_run"})
        leg["hr_vs_hand"] = hrs
        leg["rank_metric"] = vs_hand.get("xwoba") or 0
    elif market == "single":
        singles_total = sum(1 for r in rows if r.get("events") == "single")
        leg["singles_per_game"] = round(singles_total / games, 2) if games else 0
        leg["rank_metric"] = vs_hand.get("avg") or 0
    else:  # hit
        leg["rank_metric"] = vs_hand.get("xba") or 0

    # The pitch-mix flavor line ("hitting .335 vs the FF he throws 45%")
    try:
        starter_rows = get_player_season_rows(starter_id, True)
        leg["mix_line"] = top_pitch_matchup_line(rows, starter_rows, batter_side, starter_hand)
    except Exception:
        leg["mix_line"] = None
    return leg


def evaluate_k_leg(starter_id: int, starter_name: str, team: str, opponent: str) -> dict | None:
    """Pitcher strikeout leg: real K%/whiff% vs each side, from validated
    splits."""
    try:
        rows = get_player_season_rows(starter_id, True)
    except Exception:
        return None
    if not rows:
        return None
    vs_l = statcast_api.vs_handedness_stats(rows, "stand", "L")
    vs_r = statcast_api.vs_handedness_stats(rows, "stand", "R")
    if not vs_l and not vs_r:
        return None
    k_l = (vs_l or {}).get("k_pct") or 0
    k_r = (vs_r or {}).get("k_pct") or 0
    total_pa = ((vs_l or {}).get("pa") or 0) + ((vs_r or {}).get("pa") or 0)
    if total_pa < 100:
        return None
    return {
        "starter": starter_name, "team": team, "opponent": opponent,
        "k_pct_vs_l": k_l, "k_pct_vs_r": k_r,
        "whiff_vs_l": (vs_l or {}).get("whiff_pct"), "whiff_vs_r": (vs_r or {}).get("whiff_pct"),
        "pa": total_pa,
        "rank_metric": max(k_l, k_r),
    }


def pick_one_per_game(legs: list[dict], game_of: dict, count: int) -> list[dict]:
    """Classic parlay diversification: best-ranked leg from each distinct
    game, then top `count` overall."""
    legs_sorted = sorted(legs, key=lambda x: -(x.get("rank_metric") or 0))
    chosen, used_games = [], set()
    for leg in legs_sorted:
        game = game_of.get(id(leg))
        if game in used_games:
            continue
        used_games.add(game)
        chosen.append(leg)
        if len(chosen) >= count:
            break
    return chosen
