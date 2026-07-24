import os
import logging
import asyncio
from typing import Literal

LegsT = Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

import discord
from discord import app_commands
from dotenv import load_dotenv

import parlay
import statcast_api
import odds_api
import ev_features
import parlay_track

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("parlay_bot")

intents = discord.Intents.default()

MARKET_CONFIG = {
    "hit": {"title": "🎯 Hit Parlay", "shortlist_pct": "xba",
            "note": "ranked by real xBA vs the starter's hand"},
    "hr": {"title": "💣 HR Parlay", "shortlist_pct": "brl_percent",
           "note": "ranked by real xwOBA vs the starter's hand"},
}


def _leg_lines(leg: dict, market: str) -> str:
    lines = [f"vs {leg['starter']} ({leg['starter_hand']}HP)"]
    stat_bits = [f"{leg['pa_vs_hand']} PA vs {leg['starter_hand']}"]
    if leg.get("avg_vs_hand") is not None:
        stat_bits.append(f"AVG {leg['avg_vs_hand']}")
    if leg.get("xba_vs_hand") is not None:
        stat_bits.append(f"xBA {leg['xba_vs_hand']}")
    if market == "hr":
        if leg.get("xwoba_vs_hand") is not None:
            stat_bits.append(f"xwOBA {leg['xwoba_vs_hand']}")
        stat_bits.append(f"{leg.get('hr_vs_hand', 0)} HR vs {leg['starter_hand']}HP")
    lines.append(" • ".join(stat_bits))
    lines.append(f"Hit in {leg['hit_game_pct']}% of {leg['games']} games")
    if leg.get("mix_line"):
        lines.append(leg["mix_line"])
    return "\n".join(lines)


def parlay_ticket(priced_legs: list, same_game: bool, verb: str = "Parlay",
                  leg_names: list | None = None) -> tuple[str, list]:
    """The ticket treatment every command shares: exact combined price in
    the header for cross-game parlays (real math), NO number for same-game
    (only the book knows its SGP price), and full-slip/build-slip buttons.
    When NO single book prices every leg (common on HR props), degrade
    honestly: per-leg best-price buttons instead of silence."""
    by_book = odds_api.parlay_by_book(priced_legs)
    if not by_book:
        # No book covers EVERY leg. Build the biggest PARTIAL parlay we can
        # at the best-covering book, singles only for the stragglers --
        # a parlay ask deserves a parlay answer, honestly labeled.
        def _name(i):
            return leg_names[i] if leg_names and i < len(leg_names) else f"Leg {i + 1}"
        coverage = {}
        for i, p in enumerate(priced_legs):
            for bk in (p or {}).get("prices") or {}:
                coverage.setdefault(bk, []).append(i)
        multi = {bk: idxs for bk, idxs in coverage.items() if len(idxs) >= 2}

        def _combined(bk, idxs):
            dec = 1.0
            for i in idxs:
                dec *= odds_api.american_to_decimal(priced_legs[i]["prices"][bk])
            return odds_api.decimal_to_american(dec)

        # Prefer the book that parlays the MOST legs; among ties, the one
        # with a one-tap slip (FanDuel/DK), then the better combined price.
        best_bk, best_idxs, best_slip = None, [], None
        for bk, idxs in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            legs_sub = [{"sid": ((priced_legs[i].get("sids") or {}).get(bk)),
                         "link": ((priced_legs[i].get("links") or {}).get(bk))}
                        for i in idxs]
            slip = odds_api.build_slip_link(bk, legs_sub)
            better = (best_bk is None
                      or len(idxs) > len(best_idxs)
                      or (len(idxs) == len(best_idxs) and slip and not best_slip)
                      or (len(idxs) == len(best_idxs) and bool(slip) == bool(best_slip)
                          and _combined(bk, idxs) > _combined(best_bk, best_idxs)))
            if better:
                best_bk, best_idxs, best_slip = bk, idxs, slip

        buttons = []
        covered_idx = set()
        if best_bk:
            covered_idx = set(best_idxs)
            combined = _combined(best_bk, best_idxs)
            label = f"{len(best_idxs)}/{len(priced_legs)} legs @ {best_bk} {combined:+d}"
            if best_slip:
                buttons.append((f"{label} — partial slip", best_slip))
            else:
                # No one-tap scheme at this book (Caesars/BetRivers/etc):
                # give each leg's link AT THAT BOOK so taps land in ONE slip.
                for i in best_idxs:
                    url = (priced_legs[i].get("links") or {}).get(best_bk)
                    if url:
                        buttons.append((f"{_name(i)} {priced_legs[i]['prices'][best_bk]:+d} "
                                        f"@ {best_bk}", url))
                if not any(b for b in buttons):
                    covered_idx = set()

        unpriced = 0
        for i, p in enumerate(priced_legs):
            if i in covered_idx:
                continue
            if not p or not p.get("prices"):
                unpriced += 1
                continue
            bp = odds_api.best_price(p["prices"])
            if not bp:
                unpriced += 1
                continue
            url = (p.get("links") or {}).get(bp[0]) or next(iter((p.get("links") or {}).values()), None)
            if url:
                buttons.append((f"{_name(i)} {bp[1]:+d} @ {bp[0]}", url))
        if not buttons:
            return "", []
        if covered_idx and best_slip:
            header = (f"🎟️ **No single book prices every leg** — biggest parlay is "
                      f"**{len(covered_idx)}/{len(priced_legs)} legs @ {best_bk} "
                      f"{_combined(best_bk, best_idxs):+d}** (one tap loads them all); "
                      "any leftover leg below at its best price"
                      + (f" · {unpriced} unpriced right now" if unpriced else "") + "\n\n")
        elif covered_idx:
            header = (f"🎟️ **No single book prices every leg** — biggest parlay is "
                      f"**{len(covered_idx)}/{len(priced_legs)} legs @ {best_bk} "
                      f"{_combined(best_bk, best_idxs):+d}**. {best_bk} has no one-tap slip link, "
                      "so tap each leg below (they land in the same slip); "
                      "leftovers after that are separate bets"
                      + (f" · {unpriced} unpriced right now" if unpriced else "") + "\n\n")
        else:
            header = ("🎟️ **No single book prices every leg** — and no book prices two of them "
                      "together, so these can only be singles right now"
                      + (f" ({unpriced} leg(s) unpriced)" if unpriced else "") + "\n\n")
        return header, buttons[:25]
    slips = odds_api.parlay_slips(priced_legs, by_book)
    if same_game:
        header = "🎟️ **Same-game parlay** — tap a book below to load the full slip; the book shows its exact SGP price there\n\n"
    else:
        best = max(by_book, key=lambda bk: by_book[bk]["combined"])
        header = f"🎟️ **{verb} pays {by_book[best]['combined']:+d}** best @ {best}\n\n"
    buttons = []
    for bk in sorted(by_book, key=lambda bk: -by_book[bk]["combined"]):
        url = slips.get(bk) or by_book[bk]["link"]
        if not url:
            continue
        if bk in slips:
            label = f"Full slip @ {bk}" if same_game else f"Full slip @ {bk} {by_book[bk]['combined']:+d}"
        else:
            label = f"{bk} (build slip)" if same_game else f"{bk} {by_book[bk]['combined']:+d} (build slip)"
        buttons.append((label, url))
    return header, buttons[:5]


import random


def diversify(evaluated: list, want: int) -> list:
    """Same data, different ticket. The top of the shortlist is a cluster of
    near-equal candidates -- always taking 1..N means every user gets the
    same parlay. Weighted-shuffle the top pool (better rank = better odds of
    being picked) so tickets rotate WITHOUT dipping into worse legs: the
    pool is capped at the top 3x, and everything below stays in rank order
    as backup for pick_legs' game-diversity rules."""
    pool_size = min(len(evaluated), max(want * 3, want + 4))
    pool = list(evaluated[:pool_size])
    rest = evaluated[pool_size:]
    picked = []
    while pool:
        weights = [1.0 / (i + 1.5) for i in range(len(pool))]
        idx = random.choices(range(len(pool)), weights=weights, k=1)[0]
        picked.append(pool.pop(idx))
    return picked + rest


def _track(category: str, chosen: list, priced_legs: list, header: str,
           kind_of, interaction, game_of=None):
    """Record the posted parlay at 1U. Best-effort: never breaks a command."""
    try:
        by_book = odds_api.parlay_by_book(priced_legs)
        if not by_book:
            return
        book = max(by_book, key=lambda bk: by_book[bk]["combined"])
        price = by_book[book]["combined"]
        legs = []
        for leg, priced in zip(chosen, priced_legs):
            spec = kind_of(leg, priced)
            if not spec:
                return
            spec.setdefault("price", (priced or {}).get("prices", {}).get(book))
            spec.setdefault("book", book)
            legs.append(spec)
        parlay_track.record(category, legs, price, book,
                            requested_by=str(getattr(interaction.user, "id", "")))
    except Exception as e:
        log.warning("parlay tracking skipped: %s", e)


def build_bet_buttons(leg_links: list[tuple[str, str]]) -> discord.ui.View | None:
    """Link buttons: [('Leg 1: Soto @ BetRivers', url), ...]. Discord caps
    at 25 buttons; we stay well under. None if no book gave us links."""
    if not leg_links:
        return None
    view = discord.ui.View()
    for label, url in leg_links[:25]:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=label[:80], url=url))
    return view


class ParlayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        for name, market, desc in [
            ("hitparlay", "hit", "Build a hits parlay from today's real matchup data"),
            ("hrparlay", "hr", "Build a home run parlay from today's real matchup data"),
        ]:
            cmd = app_commands.Command(
                name=name,
                description=desc,
                callback=self._make_batter_callback(market),
            )
            self.tree.add_command(cmd)

        streak_cmd = app_commands.Command(
            name="streakparlay",
            description="Parlay every hitter on today's slate riding an active hit streak",
            callback=self._streak_callback,
        )
        self.tree.add_command(streak_cmd)

        sgp_cmd = app_commands.Command(
            name="samegameparlay",
            description="Build a same-game parlay: 1 strikeouts leg + hit legs from one game",
            callback=self._sgp_callback,
        )
        self.tree.add_command(sgp_cmd)
        sgp_cmd.autocomplete("game")(self._game_autocomplete)

        ml_cmd = app_commands.Command(
            name="moneylineparlay",
            description="Build a moneyline parlay from real starter-quality gaps + recent scoring",
            callback=self._moneyline_callback,
        )
        self.tree.add_command(ml_cmd)

        totals_cmd = app_commands.Command(
            name="totalsparlay",
            description="Rank today's run environments for over/under leans (compare vs your book's line)",
            callback=self._totals_callback,
        )
        self.tree.add_command(totals_cmd)

        k_cmd = app_commands.Command(
            name="strikeoutsparlay",
            description="Build a pitcher-strikeouts parlay from today's real K/whiff splits",
            callback=self._strikeouts_callback,
        )
        self.tree.add_command(k_cmd)

        # Bot Cooks merge: nightly EV pick + grading + live EV commands
        # (/setevchannel, /topev, /evcheck, /evrecord, /postevpick).
        # Fully self-contained module -- own Odds API calls, own sqlite
        # ledger, zero coupling to the parlay code above.
        ev_features.register_commands(self.tree)
        parlay_track.register_commands(self.tree)

        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    def _make_batter_callback(self, market: str):
        async def callback(interaction: discord.Interaction, legs: LegsT = 3,
                           min_odds: int = None, max_odds: int = None):
            await self._batter_parlay(interaction, market, legs, min_odds, max_odds)
        return callback

    async def _batter_parlay(self, interaction: discord.Interaction, market: str, legs: int,
                              min_odds: int = None, max_odds: int = None):
        await interaction.response.defer()
        cfg = MARKET_CONFIG[market]
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        # Build candidate list: top hitters per team (Savant's own percentile
        # scores), each mapped to the OPPOSING starter they'd face
        candidates = []
        game_of = {}
        for g in slate:
            for side, opp_side in (("home", "away"), ("away", "home")):
                team = g["teams"][side]
                opp = g["teams"][opp_side]
                if not opp["starter_id"]:
                    continue  # no probable starter announced yet
                try:
                    hand = await asyncio.to_thread(parlay.get_starter_hand, opp["starter_id"])
                except Exception:
                    continue
                if hand not in ("L", "R"):
                    continue
                short = await asyncio.to_thread(
                    parlay.shortlist_hitters, [team["abbrev"]], cfg["shortlist_pct"], 2
                )
                for batter in short:
                    candidates.append((batter, opp, hand, g["game_pk"]))

        if not candidates:
            await interaction.followup.send("No probable starters announced yet -- try closer to game time.")
            return

        # Evaluate with real pitch-level data (top candidates only -- each is
        # a full season fetch, so this takes a bit)
        evaluated = []
        for batter, opp, hand, game_pk in candidates[:24]:
            leg = await asyncio.to_thread(
                parlay.evaluate_hit_leg, batter, opp["starter_id"], opp["starter_name"], hand, market
            )
            if leg:
                evaluated.append(leg)
                game_of[id(leg)] = game_pk

        if not evaluated:
            await interaction.followup.send("Couldn't build qualified legs from today's matchups.")
            return

        # Prop odds: match each leg's game to an odds event, price the prop
        game_names = {g["game_pk"]: (g["teams"]["home"]["name"], g["teams"]["away"]["name"]) for g in slate}
        events = await asyncio.to_thread(odds_api.get_events)
        market_key = odds_api.PROP_MARKETS.get(market)

        def _price_leg(leg):
            gpk = game_of.get(id(leg))
            names = game_names.get(gpk)
            if not names or not events or not market_key:
                return None
            ev = odds_api.find_event(events, names[0], names[1])
            if not ev:
                return None
            props = odds_api.get_event_props(ev.get("id"), market_key)
            return odds_api.player_prop_prices(props, market_key, leg["batter"]) if props else None

        if min_odds is not None or max_odds is not None:
            filtered = []
            seen_prices = []
            for leg in evaluated:
                priced = await asyncio.to_thread(_price_leg, leg)
                bp = odds_api.best_price(priced["prices"]) if priced else None
                if bp is None:
                    continue
                seen_prices.append(bp[1])
                if min_odds is not None and bp[1] < min_odds:
                    continue
                if max_odds is not None and bp[1] > max_odds:
                    continue
                leg["_priced"] = priced
                filtered.append(leg)
            evaluated = filtered
            if not evaluated:
                if seen_prices:
                    lo, hi = min(seen_prices), max(seen_prices)
                    await interaction.followup.send(
                        f"No legs fit that odds range — the {len(seen_prices)} priced legs today ran "
                        f"**{lo:+d} to {hi:+d}**. Widen the range (or drop min/max) to see them. "
                        f"Heads up: hit props on top hitters usually live around -250 to -400."
                    )
                else:
                    await interaction.followup.send(
                        "No legs have live prices right now — props are unposted or suspended "
                        "(books pull props once games go live; tonight's lines post closer to game time)."
                    )
                return

        chosen = parlay.pick_legs(diversify(evaluated, legs), game_of, legs)

        # Price every leg, then the PARLAY per book (only books carrying all legs)
        priced_legs = []
        for leg in chosen:
            priced_legs.append(leg.get("_priced") or await asyncio.to_thread(_price_leg, leg))
        same_game = len({game_of.get(id(l)) for l in chosen}) < len(chosen)
        header, bet_buttons = parlay_ticket(
            priced_legs, same_game, leg_names=[l["batter"] for l in chosen])
        _track("hr" if market == "hr" else "hit", chosen, priced_legs, header,
               lambda leg, priced: {
                   "kind": "batter_hr" if market == "hr" else "batter_hit",
                   "name": leg["batter"], "team": leg.get("team"),
                   "game_pk": game_of.get(id(leg)),
                   "point": (priced or {}).get("point", 0.5), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"{cfg['title']} — {len(chosen)} legs", color=discord.Color.gold())
        embed.description = header + cfg["note"] + " • best legs win, any game"
        for i, leg in enumerate(chosen, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['batter']} ({leg['team']})",
                value=_leg_lines(leg, market),
                inline=False,
            )
        embed.set_footer(text="Research, not advice — confirm lineups before betting • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _strikeouts_callback(self, interaction: discord.Interaction, legs: LegsT = 3):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        evaluated = []
        game_of = {}
        for g in slate:
            for side, opp_side in (("home", "away"), ("away", "home")):
                team = g["teams"][side]
                opp = g["teams"][opp_side]
                if not team["starter_id"]:
                    continue
                leg = await asyncio.to_thread(
                    parlay.evaluate_k_leg, team["starter_id"], team["starter_name"],
                    team["abbrev"], opp["abbrev"],
                )
                if leg:
                    evaluated.append(leg)
                    game_of[id(leg)] = g["game_pk"]

        if not evaluated:
            await interaction.followup.send("No probable starters with enough data yet -- try closer to game time.")
            return

        chosen = parlay.pick_legs(diversify(evaluated, legs), game_of, legs)
        game_names = {g["game_pk"]: (g["teams"]["home"]["name"], g["teams"]["away"]["name"]) for g in slate}
        events = await asyncio.to_thread(odds_api.get_events)

        priced_legs, k_lines = [], {}
        for leg in chosen:
            priced = None
            names = game_names.get(game_of.get(id(leg)))
            if names and events:
                ev = odds_api.find_event(events, names[0], names[1])
                if ev:
                    props = await asyncio.to_thread(odds_api.get_event_props, ev.get("id"), "pitcher_strikeouts")
                    priced = odds_api.player_prop_prices(props, "pitcher_strikeouts", leg["starter"]) if props else None
            priced_legs.append(priced)
            if priced:
                k_lines[id(leg)] = priced["point"]
        same_game = len({game_of.get(id(l)) for l in chosen}) < len(chosen)
        header, bet_buttons = parlay_ticket(priced_legs, same_game, verb="Overs parlay",
                                           leg_names=[l["starter"] for l in chosen])
        _track("k", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "pitcher_k", "name": leg["starter"],
                                    "team": leg.get("team"),
                                    "game_pk": game_of.get(id(leg)),
                                    "point": (priced or {}).get("point"), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"⚔️ Strikeouts Parlay — {len(chosen)} legs", color=discord.Color.red())
        embed.description = header + "ranked by real K% vs either side"
        for i, leg in enumerate(chosen, 1):
            value = (f"K%: {leg['k_pct_vs_l']}% vs L | {leg['k_pct_vs_r']}% vs R\n"
                     f"Whiff%: {leg['whiff_vs_l']}% vs L | {leg['whiff_vs_r']}% vs R\n"
                     f"{leg['pa']} PA faced this season")
            if id(leg) in k_lines:
                value += f"\nBet: over {k_lines[id(leg)]} strikeouts"
            embed.add_field(
                name=f"Leg {i}: {leg['starter']} ({leg['team']}) vs {leg['opponent']}",
                value=value,
                inline=False,
            )

        embed.set_footer(text="Research, not advice — K prop lines vary by book • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _streak_callback(self, interaction: discord.Interaction,
                                min_streak: Literal[3, 4, 5, 6, 7, 8, 10] = 5,
                                legs: LegsT = 5):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        legs_found, game_of = await asyncio.to_thread(parlay.streak_candidates, slate, min_streak)
        if not legs_found:
            await interaction.followup.send(
                f"No scanned hitter on today's slate is riding a {min_streak}+ game hit streak."
            )
            return

        chosen = parlay.pick_legs(diversify(legs_found, legs), game_of, legs)

        game_names = {g["game_pk"]: (g["teams"]["home"]["name"], g["teams"]["away"]["name"]) for g in slate}
        events = await asyncio.to_thread(odds_api.get_events)

        def _price_streak_leg(leg):
            names = game_names.get(game_of.get(id(leg)))
            if not names or not events:
                return None
            ev = odds_api.find_event(events, names[0], names[1])
            if not ev:
                return None
            props = odds_api.get_event_props(ev.get("id"), "batter_hits")
            return odds_api.player_prop_prices(props, "batter_hits", leg["batter"]) if props else None

        priced_legs = []
        for leg in chosen:
            priced_legs.append(await asyncio.to_thread(_price_streak_leg, leg))
        same_game = len({game_of.get(id(l)) for l in chosen}) < len(chosen)
        header, bet_buttons = parlay_ticket(priced_legs, same_game,
                                            leg_names=[l["batter"] for l in chosen])
        _track("streak", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "batter_hit", "name": leg["batter"],
                                    "team": leg.get("team"),
                                    "game_pk": game_of.get(id(leg)),
                                    "point": (priced or {}).get("point", 0.5), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"🔥 Streak Parlay — {len(chosen)} legs (streaks of {min_streak}+)", color=discord.Color.orange())
        embed.description = header + "each leg = hitter to extend their ACTIVE hit streak • ranked by streak length"
        for i, leg in enumerate(chosen, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['batter']} ({leg['team']}) — 🔥 {leg['streak']}-game hit streak",
                value=_leg_lines(leg, "hit"),
                inline=False,
            )
        embed.set_footer(text="Streaks computed from real game logs • Research, not advice — confirm lineups • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _game_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception:
            return []
        choices = []
        cur = current.lower()
        for g in slate:
            label = f"{g['teams']['away']['abbrev']} @ {g['teams']['home']['abbrev']}"
            if cur in label.lower():
                choices.append(app_commands.Choice(name=label, value=str(g["game_pk"])))
            if len(choices) >= 25:
                break
        return choices

    async def _sgp_callback(self, interaction: discord.Interaction, game: str,
                             legs: LegsT = 3):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        target = next((g for g in slate if str(g["game_pk"]) == game), None)
        if target is None:
            await interaction.followup.send("Couldn't find that game on today's slate -- pick one from the dropdown.")
            return

        cands = await asyncio.to_thread(parlay.sgp_candidates, target)
        chosen = []
        if cands["k_legs"]:
            chosen.append(("k", cands["k_legs"][0]))
        for hit in cands["hit_legs"]:
            if len(chosen) >= legs:
                break
            chosen.append(("hit", hit))

        if len(chosen) < 2:
            await interaction.followup.send(
                "Not enough qualified legs in this game yet (starters unannounced or thin samples) -- try closer to game time."
            )
            return

        matchup = f"{target['teams']['away']['abbrev']} @ {target['teams']['home']['abbrev']}"

        events = await asyncio.to_thread(odds_api.get_events)
        ev = odds_api.find_event(events, target["teams"]["home"]["name"], target["teams"]["away"]["name"]) if events else None

        def _price_sgp_leg(kind, leg):
            if not ev:
                return None
            market_key = "pitcher_strikeouts" if kind == "k" else "batter_hits"
            player = leg["starter"] if kind == "k" else leg["batter"]
            props = odds_api.get_event_props(ev.get("id"), market_key)
            return odds_api.player_prop_prices(props, market_key, player) if props else None

        priced_legs = []
        for kind, leg in chosen:
            priced_legs.append(await asyncio.to_thread(_price_sgp_leg, kind, leg))
        header, bet_buttons = parlay_ticket(priced_legs, same_game=True)
        _track("sgp", chosen, priced_legs, header,
               lambda pair, priced: {
                   "kind": "pitcher_k" if pair[0] == "k" else "batter_hit",
                   "name": pair[1].get("starter") if pair[0] == "k" else pair[1].get("batter"),
                   "team": pair[1].get("team"), "game_pk": target,
                   "point": (priced or {}).get("point", 0.5), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"🎰 Same Game Parlay — {matchup}", color=discord.Color.purple())
        embed.description = header + "structure: best strikeouts leg + top hit legs (xBA vs hand) • all one game"
        for i, (kind, leg) in enumerate(chosen, 1):
            if kind == "k":
                embed.add_field(
                    name=f"Leg {i}: {leg['starter']} strikeouts ({leg['team']})",
                    value=(f"K%: {leg['k_pct_vs_l']}% vs L | {leg['k_pct_vs_r']}% vs R\n"
                           f"Whiff%: {leg['whiff_vs_l']}% vs L | {leg['whiff_vs_r']}% vs R"),
                    inline=False,
                )
            else:
                embed.add_field(
                    name=f"Leg {i}: {leg['batter']} ({leg['team']}) to record a hit",
                    value=_leg_lines(leg, "hit"),
                    inline=False,
                )
        embed.set_footer(text="SGP legs are correlated — the book shows its exact price on the slip • Research, not advice • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _moneyline_callback(self, interaction: discord.Interaction, legs: LegsT = 3,
                                   min_odds: int = None, max_odds: int = None):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        evaluated, game_of = [], {}
        for g in slate:
            leg = await asyncio.to_thread(parlay.evaluate_moneyline_leg, g)
            if leg:
                evaluated.append(leg)
                game_of[id(leg)] = g["game_pk"]
        if not evaluated:
            await interaction.followup.send("No games with both probable starters qualified yet -- try closer to game time.")
            return

        odds_events = await asyncio.to_thread(odds_api.get_mlb_odds, "h2h")
        if odds_events and (min_odds is not None or max_odds is not None):
            filtered = []
            seen_prices = []
            for leg in evaluated:
                event = odds_api.find_event(odds_events, leg["pick_team"], leg["opp_team"])
                bp = odds_api.best_price(odds_api.all_prices(event, "h2h", leg["pick_team"])) if event else None
                if bp is None:
                    continue
                seen_prices.append(bp[1])
                if min_odds is not None and bp[1] < min_odds:
                    continue
                if max_odds is not None and bp[1] > max_odds:
                    continue
                filtered.append(leg)
            evaluated = filtered
            if not evaluated:
                if seen_prices:
                    lo, hi = min(seen_prices), max(seen_prices)
                    await interaction.followup.send(
                        f"No moneyline legs fit that odds range — the {len(seen_prices)} priced legs today ran "
                        f"**{lo:+d} to {hi:+d}**. Widen the range (or drop min/max) to see them."
                    )
                else:
                    await interaction.followup.send("No moneyline legs have live prices right now.")
                return
        chosen = parlay.pick_legs(diversify(evaluated, legs), game_of, legs, max_per_game=1)

        priced_legs = []
        for leg in chosen:
            event = odds_api.find_event(odds_events, leg["pick_team"], leg["opp_team"]) if odds_events else None
            if event:
                prices, links, sids = odds_api.all_prices_and_links(event, "h2h", leg["pick_team"])
                priced_legs.append({"prices": prices, "links": links, "sids": sids} if prices else None)
            else:
                priced_legs.append(None)
        header, bet_buttons = parlay_ticket(priced_legs, same_game=False,
                                            leg_names=[l["pick_team"] for l in chosen])
        _track("moneyline", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "moneyline", "name": leg["pick_team"],
                                    "team": leg["pick_team"],
                                    "game_pk": game_of.get(id(leg)),
                                    "point": None, "side": "win"},
               interaction)

        embed = discord.Embed(title=f"💰 Moneyline Parlay — {len(chosen)} legs", color=discord.Color.green())
        embed.description = header + "ranked by real starter xwOBA-against gap • one leg per game"
        for i, leg in enumerate(chosen, 1):
            lines = [
                f"{leg['pick_starter']} xwOBA-against {leg['pick_xwoba']} vs {leg['opp_starter']} {leg['opp_xwoba']} (gap {leg['rank_metric']})",
                f"K%: {leg['pick_k']}% vs {leg['opp_k']}%",
            ]
            if leg.get("pick_runs") and leg.get("opp_runs"):
                lines.append(
                    f"Last 10 runs/gm: {leg['pick_abbrev']} {leg['pick_runs']['runs_pg']} scored / {leg['pick_runs']['runs_allowed_pg']} allowed"
                    f" • opp {leg['opp_runs']['runs_pg']} / {leg['opp_runs']['runs_allowed_pg']}"
                )
            embed.add_field(
                name=f"Leg {i}: {leg['pick_team']} ML over {leg['opp_team']}",
                value="\n".join(lines),
                inline=False,
            )
        embed.set_footer(text="Research, not advice — starter-quality gap, not a win probability • Data: Baseball Savant / MLB / The Odds API")

        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _totals_callback(self, interaction: discord.Interaction,
                                lean: Literal["overs", "unders"] = "overs",
                                legs: LegsT = 3):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        evaluated, game_of = [], {}
        for g in slate:
            leg = await asyncio.to_thread(parlay.evaluate_total_leg, g)
            if leg:
                if lean == "unders":
                    leg["rank_metric"] = -leg["rank_metric"]  # lowest environments first
                evaluated.append(leg)
                game_of[id(leg)] = g["game_pk"]
        if not evaluated:
            await interaction.followup.send("Couldn't compute run environments yet -- try closer to game time.")
            return

        chosen = parlay.pick_one_per_game(evaluated, game_of, legs)
        odds_events = await asyncio.to_thread(odds_api.get_mlb_odds, "totals")
        arrow = "⬆️" if lean == "overs" else "⬇️"
        embed = discord.Embed(title=f"{arrow} Totals Leans ({lean}) — {len(chosen)} games", color=discord.Color.blue())
        embed.description = "ranked by combined runs/gm (last 10) • starters shown for context"
        priced_legs = []
        side_name = "Over" if lean == "overs" else "Under"
        for leg in chosen:
            priced = None
            if odds_events:
                names = [t["team"]["name"] for t in leg["teams"]]
                event = odds_api.find_event(odds_events, names[0], names[1])
                if event:
                    tl = odds_api.totals_line(event)
                    if tl:
                        leg["_point"] = tl["point"]
                        prices, links, sids = odds_api.all_prices_and_links(event, "totals", side_name, point=tl["point"])
                        if prices:
                            priced = {"prices": prices, "links": links, "sids": sids}
            priced_legs.append(priced)
        header, bet_buttons = parlay_ticket(priced_legs, same_game=False, verb=f"{side_name}s parlay",
                                            leg_names=[f"{side_name} {l.get('_point')}" for l in chosen])
        _track("totals", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "total", "name": f"{side_name} {leg.get('_point')}",
                                    "team": None, "game_pk": game_of.get(id(leg)),
                                    "point": leg.get("_point"), "side": side_name.lower()},
               interaction)
        if header:
            embed.description = header + embed.description

        for i, leg in enumerate(chosen, 1):
            lines = [f"Combined recent scoring: {leg['combined_runs_pg']} runs/gm"]
            if leg.get("_point") is not None:
                lines.append(f"Bet: {side_name.lower()} {leg['_point']} total runs")
            for t in leg["teams"]:
                s = t["starter_stats"]
                starter_bit = f" — {t['team']['starter_name']} xwOBA-against {s['xwoba']}" if s and s.get("xwoba") is not None else ""
                lines.append(f"{t['team']['abbrev']}: {t['runs']['runs_pg']} scored / {t['runs']['runs_allowed_pg']} allowed{starter_bit}")
            embed.add_field(name=f"{i}. {leg['matchup']}", value="\n".join(lines), inline=False)
        footer = ("Data: MLB / Baseball Savant / The Odds API" if odds_events
                  else "No totals lines on current odds plan — compare vs your book • Data: MLB / Baseball Savant")
        embed.set_footer(text=footer)
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        ev_features.start_tasks(self)
        parlay_track.start_tasks(self)


client = ParlayBot()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
