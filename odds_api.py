"""
The Odds API client. One call returns every book's prices for the whole
slate, so responses are cached hard (free tier = 25 requests/day; a 30-min
cache means ~5-8 requests/day total even with heavy bot usage).

Tier-aware: requests only the markets asked for, and degrades gracefully --
if the current plan doesn't include a market (e.g. totals/props on free),
commands simply show no odds line instead of erroring.
"""
import os
import time
import logging
import unicodedata

import requests

log = logging.getLogger("odds_api")

API_KEY = os.getenv("ODDS_API_KEY")
BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"

_cache: dict = {}
CACHE_SECONDS = 1800  # 30 min -- odds move, but not enough to matter here


def get_mlb_odds(markets: str = "h2h") -> list[dict]:
    """Raw events list for the whole slate. Empty list on any problem
    (no key, plan doesn't cover the market, network) -- never raises."""
    if not API_KEY:
        return []
    now = time.time()
    cached = _cache.get(markets)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        resp = requests.get(
            BASE,
            params={"apiKey": API_KEY, "regions": "us", "markets": markets, "oddsFormat": "american", "includeLinks": "true"},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("Odds API %s for markets=%s: %s", resp.status_code, markets, resp.text[:200])
            _cache[markets] = (now, [])
            return []
        data = resp.json()
        _cache[markets] = (now, data)
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            log.info("Odds API ok (markets=%s), requests remaining: %s", markets, remaining)
        return data
    except Exception as e:
        log.warning("Odds API fetch failed: %s", e)
        return []


DEFAULT_STATE = os.getenv("ODDS_LINK_STATE", "ny")


def _clean_link(url) -> str | None:
    """Discord buttons require well-formed absolute URLs. Some books return
    templated links ({state} subdomains etc.) -- fill what we can, reject
    the rest so callers fall back to text odds instead of crashing."""
    if not url or not isinstance(url, str):
        return None
    url = url.replace("{state}", DEFAULT_STATE).replace("{STATE}", DEFAULT_STATE)
    if "{" in url or "}" in url or " " in url:
        return None  # unfilled template -- unusable
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower().strip()


def find_event(events: list[dict], team_a: str, team_b: str) -> dict | None:
    """Match one of our games to an odds event by team names (containment
    either direction, accent/case-insensitive -- handles 'Athletics' vs
    'Oakland Athletics' style differences)."""
    a, b = _fold(team_a), _fold(team_b)

    def _matches(ours: str, theirs: str) -> bool:
        return bool(ours) and bool(theirs) and (ours in theirs or theirs in ours)

    for ev in events:
        home, away = _fold(ev.get("home_team")), _fold(ev.get("away_team"))
        if (_matches(a, home) and _matches(b, away)) or (_matches(a, away) and _matches(b, home)):
            return ev
    return None


def all_prices_and_links(event: dict, market_key: str, outcome_name: str) -> tuple[dict, dict]:
    """({book: price}, {book: deepest available link}) for one outcome."""
    prices, links = {}, {}
    target = _fold(outcome_name)
    for book in event.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                name = _fold(outcome.get("name"))
                if name and (name in target or target in name):
                    title = book.get("title", "?")
                    prices[title] = outcome.get("price")
                    link = _clean_link(outcome.get("link") or market.get("link") or book.get("link"))
                    if link:
                        links[title] = link
    return prices, links


def all_prices(event: dict, market_key: str, outcome_name: str) -> dict:
    """{book_title: price} for one outcome across every book carrying it."""
    prices = {}
    target = _fold(outcome_name)
    for book in event.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                name = _fold(outcome.get("name"))
                if name and (name in target or target in name):
                    prices[book.get("title", "?")] = outcome.get("price")
    return prices


def totals_line(event: dict) -> dict | None:
    """The posted total: {'point': 8.5, 'over': {book: price}, 'under': {...}}
    using the most common point across books."""
    points = {}
    for book in event.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != "totals":
                continue
            for outcome in market.get("outcomes", []) or []:
                pt = outcome.get("point")
                if pt is None:
                    continue
                bucket = points.setdefault(pt, {"over": {}, "under": {}})
                side = (outcome.get("name") or "").lower()
                if side in ("over", "under"):
                    bucket[side][book.get("title", "?")] = outcome.get("price")
    if not points:
        return None
    # Most-quoted point = the consensus line
    best_pt = max(points.items(), key=lambda x: len(x[1]["over"]) + len(x[1]["under"]))
    return {"point": best_pt[0], **best_pt[1]}


def best_price(prices: dict) -> tuple[str, int] | None:
    """Best price for the bettor = highest American odds value
    (-165 beats -180; +140 beats +120)."""
    if not prices:
        return None
    book = max(prices, key=lambda b: prices[b])
    return book, prices[book]


def american_to_decimal(price: int) -> float:
    if price > 0:
        return 1 + price / 100
    return 1 + 100 / abs(price)


def decimal_to_american(dec: float) -> int:
    if dec >= 2:
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))


def parlay_price(american_prices: list[int]) -> int | None:
    """Combined parlay price from individual leg prices."""
    if not american_prices:
        return None
    dec = 1.0
    for p in american_prices:
        dec *= american_to_decimal(p)
    return decimal_to_american(dec)


def format_prices(prices: dict, limit: int = 4) -> str:
    """'DK -140 • FD -138 • MGM -135 ← best' (best marked)."""
    if not prices:
        return ""
    best = best_price(prices)
    shown = sorted(prices.items(), key=lambda x: -x[1])[:limit]
    parts = []
    for book, price in shown:
        tag = f"{book} {price:+d}"
        if best and book == best[0]:
            tag += " ← best"
        parts.append(tag)
    return " • ".join(parts)


# ---------- player props (paid tier) ----------

EVENTS_BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb/events"

# Market keys per our parlay types; hits/HR props are Over 0.5 lines
PROP_MARKETS = {"hit": "batter_hits", "hr": "batter_home_runs", "k": "pitcher_strikeouts"}

_event_cache: dict = {}


def get_events() -> list[dict]:
    """Today's event list (ids + team names). Cached like odds."""
    if not API_KEY:
        return []
    now = time.time()
    cached = _cache.get("__events__")
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        resp = requests.get(EVENTS_BASE, params={"apiKey": API_KEY}, timeout=20)
        if resp.status_code != 200:
            log.warning("Odds events %s: %s", resp.status_code, resp.text[:200])
            _cache["__events__"] = (now, [])
            return []
        data = resp.json()
        _cache["__events__"] = (now, data)
        return data
    except Exception as e:
        log.warning("Odds events fetch failed: %s", e)
        return []


def get_event_props(event_id: str, market_key: str) -> dict | None:
    """One event's prop odds for one market, cached 30 min. None if the
    plan/books don't carry it -- callers degrade gracefully."""
    if not API_KEY or not event_id:
        return None
    now = time.time()
    key = (event_id, market_key)
    cached = _event_cache.get(key)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        resp = requests.get(
            f"{EVENTS_BASE}/{event_id}/odds",
            params={"apiKey": API_KEY, "regions": "us", "markets": market_key, "oddsFormat": "american", "includeLinks": "true"},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("Odds props %s (%s): %s", resp.status_code, market_key, resp.text[:200])
            _event_cache[key] = (now, None)
            return None
        data = resp.json()
        _event_cache[key] = (now, data)
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            log.info("Odds props ok (%s), requests remaining: %s", market_key, remaining)
        return data
    except Exception as e:
        log.warning("Odds props fetch failed: %s", e)
        return None


def player_prop_prices(event_data: dict, market_key: str, player_name: str) -> dict | None:
    """{book: price} for a player's OVER, plus the line. For hits/HR that's
    'Over 0.5' = to record one; for Ks it's the posted strikeout line.
    Returns {'point': x, 'prices': {book: price}} or None if unpriced."""
    if not event_data:
        return None
    target = _fold(player_name)
    target_last = target.split()[-1] if target else ""
    by_point: dict = {}
    for book in event_data.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                if (outcome.get("name") or "").lower() != "over":
                    continue
                desc = _fold(outcome.get("description"))
                if not desc or (target not in desc and target_last not in desc):
                    continue
                pt = outcome.get("point")
                bucket = by_point.setdefault(pt, {"prices": {}, "links": {}})
                title = book.get("title", "?")
                bucket["prices"][title] = outcome.get("price")
                link = _clean_link(outcome.get("link") or market.get("link") or book.get("link"))
                if link:
                    bucket["links"][title] = link
    if not by_point:
        return None
    # Hits/HR: want the 0.5 line when present; otherwise most-quoted point
    if market_key in ("batter_hits", "batter_home_runs") and 0.5 in by_point:
        point = 0.5
    else:
        point = max(by_point, key=lambda p: len(by_point[p]["prices"]))
    chosen = by_point[point]
    return {"point": point, "prices": chosen["prices"], "links": chosen["links"]}


def parlay_by_book(priced_legs: list[dict]) -> dict:
    """Combined parlay price PER BOOK, only for books that price EVERY leg.
    priced_legs: [{'prices': {book: price}, 'links': {book: url}}, ...]
    Returns {book: {'combined': american_int, 'link': first_available_url}}."""
    if not priced_legs or any(not p or not p.get("prices") for p in priced_legs):
        return {}
    books = set(priced_legs[0]["prices"])
    for p in priced_legs[1:]:
        books &= set(p["prices"])
    out = {}
    for book in books:
        dec = 1.0
        for p in priced_legs:
            dec *= american_to_decimal(p["prices"][book])
        link = None
        for p in priced_legs:
            link = (p.get("links") or {}).get(book)
            if link:
                break
        out[book] = {"combined": decimal_to_american(dec), "link": link}
    return out
