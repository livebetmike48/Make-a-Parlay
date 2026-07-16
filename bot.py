import os
import logging
import asyncio
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv

import parlay
import statcast_api
import odds_api

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("parlay_bot")

intents = discord.Intents.default()

MARKET_CONFIG = {
    "hit": {"title": "🎯 Hit Parlay", "shortlist_pct": "xba",
            "note": "ranked by real xBA vs the starter's hand"},
    "single": {"title": "1️⃣ Singles Parlay", "shortlist_pct": "xba",
               "note": "ranked by real AVG vs the starter's hand"},
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
    elif market == "single":
        stat_bits.append(f"{leg.get('singles_per_game', 0)} singles/gm")
    lines.append(" • ".join(stat_bits))
    lines.append(f"Hit in {leg['hit_game_pct']}% of {leg['games']} games")
    if leg.get("mix_line"):
        lines.append(leg["mix_line"])
    return "\n".join(lines)


class ParlayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        for name, market, desc in [
            ("hitparlay", "hit", "Build a hits parlay from today's real matchup data"),
            ("singlesparlay", "single", "Build a singles parlay from today's real matchup data"),
            ("hrparlay", "hr", "Build a home run parlay from today's real matchup data"),
        ]:
            cmd = app_commands.Command(
                name=name,
                description=desc,
                callback=self._make_batter_callback(market),
            )
            self.tree.add_command(cmd)

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
        async def callback(interaction: discord.Interaction, legs: Literal[2, 3, 4, 5] = 3):
            await self._batter_parlay(interaction, market, legs)
        return callback

    async def _batter_parlay(self, interaction: discord.Interaction, market: str, legs: int):
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

        chosen = parlay.pick_one_per_game(evaluated, game_of, legs)

        embed = discord.Embed(title=f"{cfg['title']} — {len(chosen)} legs", color=discord.Color.gold())
        embed.description = cfg["note"] + " • one leg per game"
        for i, leg in enumerate(chosen, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['batter']} ({leg['team']})",
                value=_leg_lines(leg, market),
                inline=False,
            )
        embed.set_footer(text="Research, not advice — confirm lineups before betting • Data: Baseball Savant / MLB")
        await interaction.followup.send(embed=embed)

    async def _strikeouts_callback(self, interaction: discord.Interaction, legs: Literal[2, 3, 4, 5] = 3):
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

        chosen = parlay.pick_one_per_game(evaluated, game_of, legs)

        embed = discord.Embed(title=f"⚔️ Strikeouts Parlay — {len(chosen)} legs", color=discord.Color.red())
        embed.description = "ranked by real K% vs either side • one leg per game"
        for i, leg in enumerate(chosen, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['starter']} ({leg['team']}) vs {leg['opponent']}",
                value=(f"K%: {leg['k_pct_vs_l']}% vs L | {leg['k_pct_vs_r']}% vs R\n"
                       f"Whiff%: {leg['whiff_vs_l']}% vs L | {leg['whiff_vs_r']}% vs R\n"
                       f"{leg['pa']} PA faced this season"),
                inline=False,
            )
        embed.set_footer(text="Research, not advice — K prop lines vary by book • Data: Baseball Savant / MLB")
        await interaction.followup.send(embed=embed)

    async def _moneyline_callback(self, interaction: discord.Interaction, legs: Literal[2, 3, 4, 5] = 3):
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

        chosen = parlay.pick_one_per_game(evaluated, game_of, legs)
        odds_events = await asyncio.to_thread(odds_api.get_mlb_odds, "h2h")

        best_leg_prices = []
        embed = discord.Embed(title=f"💰 Moneyline Parlay — {len(chosen)} legs", color=discord.Color.green())
        embed.description = "ranked by real starter xwOBA-against gap • one leg per game"
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
            event = odds_api.find_event(odds_events, leg["pick_team"], leg["opp_team"]) if odds_events else None
            if event:
                prices = odds_api.all_prices(event, "h2h", leg["pick_team"])
                if prices:
                    lines.append("Odds: " + odds_api.format_prices(prices))
                    bp = odds_api.best_price(prices)
                    if bp:
                        best_leg_prices.append(bp[1])
            embed.add_field(
                name=f"Leg {i}: {leg['pick_team']} ML over {leg['opp_team']}",
                value="\n".join(lines),
                inline=False,
            )
        if len(best_leg_prices) == len(chosen) and best_leg_prices:
            combo = odds_api.parlay_price(best_leg_prices)
            if combo is not None:
                embed.add_field(name="Parlay at best prices", value=f"**{combo:+d}**", inline=False)
        embed.set_footer(text="Research, not advice — starter-quality gap, not a win probability • Data: Baseball Savant / MLB / The Odds API")
        await interaction.followup.send(embed=embed)

    async def _totals_callback(self, interaction: discord.Interaction,
                                lean: Literal["overs", "unders"] = "overs",
                                legs: Literal[2, 3, 4, 5] = 3):
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
        for i, leg in enumerate(chosen, 1):
            lines = [f"Combined recent scoring: {leg['combined_runs_pg']} runs/gm"]
            if odds_events:
                names = [t["team"]["name"] for t in leg["teams"]]
                event = odds_api.find_event(odds_events, names[0], names[1])
                if event:
                    tl = odds_api.totals_line(event)
                    if tl:
                        side_prices = tl["over"] if lean == "overs" else tl["under"]
                        bp = odds_api.best_price(side_prices)
                        bp_str = f" ({lean[:-1]} {bp[1]:+d} @ {bp[0]})" if bp else ""
                        lines.append(f"Posted line: {tl['point']}{bp_str}")
            for t in leg["teams"]:
                s = t["starter_stats"]
                starter_bit = f" — {t['team']['starter_name']} xwOBA-against {s['xwoba']}" if s and s.get("xwoba") is not None else ""
                lines.append(f"{t['team']['abbrev']}: {t['runs']['runs_pg']} scored / {t['runs']['runs_allowed_pg']} allowed{starter_bit}")
            embed.add_field(name=f"{i}. {leg['matchup']}", value="\n".join(lines), inline=False)
        footer = ("Data: MLB / Baseball Savant / The Odds API" if odds_events
                  else "No totals lines on current odds plan — compare vs your book • Data: MLB / Baseball Savant")
        embed.set_footer(text=footer)
        await interaction.followup.send(embed=embed)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)


client = ParlayBot()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
