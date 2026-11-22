"""Alle Slash-Commands."""
from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app import formatters as fmt
from app.config import CUP_LEAGUES, LEAGUE_NAMES, settings
from app.db import AsyncSessionLocal, db_ok
from app.fuzzy import find_team, rank_teams
from app.matchdata import GoalEvent, parse_match
from app.models import Match
from app.repositories.matches import (
    get_current_matchday, get_day_matches, get_live_matches, get_matchday_matches,
    get_team_current_or_last, get_team_form, get_team_league, get_team_next_match,
    get_team_season_stats, get_upcoming_matches,
)
from app.repositories.teams import get_all_teams, get_team_table_position
from app.sync import get_cached_table, seed_aliases, sync_table_cache
from app.timeutil import discord_ts, local_today, utcnow

logger = logging.getLogger(__name__)

LIGA_CHOICES = [
    app_commands.Choice(name=LEAGUE_NAMES.get(lg, lg.upper()), value=lg) for lg in settings.league_list
][:25]
TABLE_CHOICES = [c for c in LIGA_CHOICES if c.value not in CUP_LEAGUES] or LIGA_CHOICES


def _lg(choice: app_commands.Choice[str] | None) -> str | None:
    return choice.value if choice else None


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id in settings.admin_ids:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not is_admin(interaction):
            raise app_commands.CheckFailure("Nur für Admins.")
        return True

    return app_commands.check(predicate)


async def team_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    async with AsyncSessionLocal() as session:
        teams = await get_all_teams(session, settings.league_list, settings.current_season)
        if not teams:
            teams = await get_all_teams(session)
    return [app_commands.Choice(name=t.name[:100], value=str(t.id)) for t in rank_teams(current, teams, 25)]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TickerCog(bot))


class TickerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def client(self):
        return self.bot.ol_client

    @property
    def ticker(self):
        return self.bot.ticker

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ Kein Zugriff – nur für Admins."
        else:
            logger.exception("Command-Fehler in /%s", interaction.command.name if interaction.command else "?",
                             exc_info=error)
            msg = "❌ Da ist etwas schiefgelaufen. Details stehen im Bot-Log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    # ── User-Commands ────────────────────────────────────────────────────────
    @app_commands.command(name="hilfe", description="Alle verfügbaren Commands")
    async def cmd_hilfe(self, interaction: discord.Interaction):
        e = fmt._base("⚽ Stadion-Echo – BL-Ticker", fmt.COLOR_DEFAULT)
        e.add_field(name="👤 Spiele", value=(
            "`/heute` – Spiele heute\n"
            "`/live` – laufende Spiele\n"
            "`/upcoming` – die nächsten 7 Tage\n"
            "`/spieltag` – Spieltag-Übersicht\n"
            "`/spiel <verein>` – aktuelles/letztes Spiel mit Torschützen"
        ), inline=False)
        e.add_field(name="📊 Infos", value=(
            "`/verein <name>` – Tabelle, Form, nächstes Spiel\n"
            "`/tabelle` – Ligatabelle\n"
            "`/torjaeger` – Torjägerliste"
        ), inline=False)
        if is_admin(interaction):
            e.add_field(name="🔧 Admin", value=(
                "`/status` `/sync` `/reload` `/kanaele`\n"
                "`/test_spiel` (ganzes Spiel in alle Channels) · `/test_aufraeumen` · `/leeren`\n"
                "`/test_anpfiff` `/test_tor` `/test_halbzeit` `/test_abpfiff`\n"
                "`/test_opener` `/test_konferenz` `/test_tabelle`"
            ), inline=False)
        e.add_field(name="ℹ️ Ticker", value=(
            "Anpfiff, Tore (Schütze wird nachgetragen), Halbzeit, Abpfiff und die Tabelle "
            "kommen automatisch – in die Konferenz und in den Channel des jeweiligen Spiels. "
            "Daten: OpenLigaDB (Community, wenige Sekunden bis Minuten Verzug, keine Karten)."
        ), inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="heute", description="Spiele heute")
    @app_commands.describe(liga="optional: nur eine Liga")
    @app_commands.choices(liga=LIGA_CHOICES)
    async def cmd_heute(self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None):
        async with AsyncSessionLocal() as session:
            matches = await get_day_matches(session, local_today(), _lg(liga))
        title = f"📅 Heute – {LEAGUE_NAMES.get(_lg(liga), 'alle Wettbewerbe')}"
        await interaction.response.send_message(embed=fmt.embed_match_list(matches, title, "Heute keine Spiele."))

    @app_commands.command(name="live", description="Laufende Spiele")
    @app_commands.choices(liga=LIGA_CHOICES)
    async def cmd_live(self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None):
        async with AsyncSessionLocal() as session:
            matches = await get_live_matches(session, _lg(liga))
        await interaction.response.send_message(
            embed=fmt.embed_match_list(matches, "🔴 Live", "Gerade läuft kein Spiel.")
        )

    @app_commands.command(name="upcoming", description="Kommende Spiele (7 Tage)")
    @app_commands.choices(liga=LIGA_CHOICES)
    async def cmd_upcoming(self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None):
        async with AsyncSessionLocal() as session:
            matches = await get_upcoming_matches(session, _lg(liga), days=7)
        await interaction.response.send_message(
            embed=fmt.embed_match_list(matches, "🗓 Kommende Spiele", "In den nächsten 7 Tagen keine Spiele.")
        )

    @app_commands.command(name="spieltag", description="Spiele eines Spieltags")
    @app_commands.describe(liga="Standard: 1. Bundesliga", spieltag="Nummer (Standard: aktueller)")
    @app_commands.choices(liga=LIGA_CHOICES)
    async def cmd_spieltag(
        self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None,
        spieltag: app_commands.Range[int, 1, 38] | None = None,
    ):
        league = _lg(liga) or "bl1"
        async with AsyncSessionLocal() as session:
            day = spieltag or await get_current_matchday(session, league)
            matches = await get_matchday_matches(session, day, league)
        await interaction.response.send_message(
            embed=fmt.embed_match_list(
                matches, f"🏆 {fmt.league_label(league, day, group_name=matches[0].group_name if matches else None)}")
        )

    @app_commands.command(name="spiel", description="Aktuelles bzw. letztes Spiel eines Vereins (mit Torschützen)")
    @app_commands.describe(verein="Vereinsname oder Kürzel")
    @app_commands.autocomplete(verein=team_autocomplete)
    async def cmd_spiel(self, interaction: discord.Interaction, verein: str):
        await interaction.response.defer()
        async with AsyncSessionLocal() as session:
            team = await find_team(session, verein)
            if not team:
                await interaction.followup.send(f"❌ Kein Verein gefunden für **{verein}**.")
                return
            m = await get_team_current_or_last(session, team.id) or await get_team_next_match(session, team.id)
        if not m:
            await interaction.followup.send(f"Keine Spiele für **{team.name}** gefunden.")
            return
        raw = m.raw_json
        if not m.is_finished and m.kickoff_utc <= utcnow():
            raw = await self.client.get_match(m.match_id) or raw  # live: frisch laden
        snap = parse_match(raw or {}, m.league_id, m.season)
        if not snap:
            await interaction.followup.send("❌ Keine Spieldaten vorhanden.")
            return
        await interaction.followup.send(embed=fmt.embed_match_detail(snap))

    @app_commands.command(name="verein", description="Vereinsinfos: Tabelle, Form, nächstes Spiel")
    @app_commands.describe(name="Vereinsname oder Kürzel (z.B. bvb, effzeh, hsv)")
    @app_commands.autocomplete(name=team_autocomplete)
    async def cmd_verein(self, interaction: discord.Interaction, name: str):
        season = settings.current_season
        async with AsyncSessionLocal() as session:
            team = await find_team(session, name)
            if not team:
                await interaction.response.send_message(f"❌ Kein Verein gefunden für **{name}**.", ephemeral=True)
                return
            form = await get_team_form(session, team.id, limit=5)
            stats = await get_team_season_stats(session, team.id, season)
            league = await get_team_league(session, team.id, season) or "bl1"
            pos = await get_team_table_position(session, team.id, league, season)
            nxt = await get_team_next_match(session, team.id)

        e = fmt._base(f"🏟 {team.name}", fmt.team_color(team.name, fmt.COLOR_DEFAULT))
        if pos:
            e.add_field(name="📊 Tabelle", value=(
                f"Platz **{pos['position']}**/{pos['total_teams']} · {pos['points']} Pkt · "
                f"{pos['goals']}:{pos['opponent_goals']} ({pos['goal_diff']:+d})"
            ), inline=False)
        if stats["played"]:
            e.add_field(name="⚽ Saisonbilanz", value=(
                f"{stats['played']} Spiele · {stats['w']}S {stats['d']}U {stats['l']}N · {stats['gf']}:{stats['ga']} Tore"
            ), inline=False)
        if form:
            lines, icons = [], ""
            for m in form:
                home = m.home_team_id == team.id
                ts, os_ = (m.home_score, m.away_score) if home else (m.away_score, m.home_score)
                icon = "🟩" if ts > os_ else ("🟨" if ts == os_ else "🟥")
                icons = icon + icons
                lines.append(f"{icon} {m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}")
            e.add_field(name=f"📈 Form {icons}", value="\n".join(lines), inline=False)
        if nxt:
            e.add_field(name="➡️ Nächstes Spiel", value=(
                f"{discord_ts(nxt.kickoff_utc, 'f')} ({discord_ts(nxt.kickoff_utc, 'R')})\n"
                f"{nxt.home_team.name} – {nxt.away_team.name}"
            ), inline=False)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="tabelle", description="Aktuelle Ligatabelle")
    @app_commands.describe(liga="Standard: 1. Bundesliga")
    @app_commands.choices(liga=TABLE_CHOICES)
    async def cmd_tabelle(self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        league = _lg(liga) or "bl1"
        season = settings.current_season
        try:
            rows = await sync_table_cache(self.client, league, season)
        except Exception as e:
            logger.warning("Tabelle live nicht ladbar (%s) – nutze Cache", e)
            rows = await get_cached_table(league, season)
        await interaction.followup.send(embed=fmt.embed_table(rows, league))

    @app_commands.command(name="torjaeger", description="Torjägerliste")
    @app_commands.describe(liga="Standard: 1. Bundesliga", top="Anzahl (1–25)")
    @app_commands.choices(liga=LIGA_CHOICES)
    async def cmd_torjaeger(
        self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None,
        top: app_commands.Range[int, 1, 25] = 10,
    ):
        await interaction.response.defer()
        league = _lg(liga) or "bl1"
        try:
            getters = await self.client.get_goalgetters(league, settings.current_season)
        except Exception:
            await interaction.followup.send("❌ Torjäger-Daten aktuell nicht verfügbar.")
            return
        await interaction.followup.send(embed=fmt.embed_goalgetters(getters, league, top))

    # ── Admin ────────────────────────────────────────────────────────────────
    @app_commands.command(name="status", description="Bot-Status (Admin)")
    @admin_only()
    async def cmd_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        api = await self.client.health_check()
        db = await db_ok()
        st = self.ticker.status
        e = fmt._base("🤖 Bot-Status", fmt.COLOR_DEFAULT)
        e.add_field(name="Datenbank", value="✅" if db else "❌", inline=True)
        e.add_field(name="OpenLigaDB", value="✅" if api else "❌", inline=True)
        e.add_field(name="Saison · Ligen", value=f"{settings.current_season} · {', '.join(settings.league_list)}", inline=True)
        e.add_field(name="Letzter Tick", value=discord_ts(st.last_tick, "R") if st.last_tick else "–", inline=True)
        e.add_field(name="Nächster Tick", value=discord_ts(st.next_wake, "R") if st.next_wake else "–", inline=True)
        e.add_field(name="Aktive Spiele", value=str(st.active_matches), inline=True)
        e.add_field(name="Letzter Season-Sync", value=discord_ts(st.last_full_sync, "R") if st.last_full_sync else "–", inline=True)
        e.add_field(name="Posts ok / fehlgeschlagen", value=f"{self.ticker.pub.sent} / {self.ticker.pub.failed}", inline=True)
        e.add_field(name="API-Requests (Fehler)", value=f"{self.client.request_count} ({self.client.error_count})", inline=True)
        perms = []
        for ch in settings.all_ticker_channels:
            missing = await self.ticker.pub.check_permissions(ch)
            perms.append(f"<#{ch}>: " + ("✅" if not missing else "❌ " + ", ".join(missing)))
        e.add_field(name="Ticker-Channel", value="\n".join(perms), inline=False)
        cm = self.bot.channels
        if cm.enabled:
            e.add_field(name="Spiel-Channels", value=(
                f"{'✅' if cm.can_manage() else '❌ Recht **Kanäle verwalten** fehlt'} · "
                f"{cm.count()} aktiv · Ligen {', '.join(settings.match_channel_league_list)} · "
                f"angelegt {cm.created} / gelöscht {cm.deleted}"
            ), inline=False)
        if st.last_error:
            e.add_field(name="Letzter Fehler", value=fmt._cut(st.last_error, 1000), inline=False)
        async with AsyncSessionLocal() as session:
            next_m = (await get_upcoming_matches(session, days=60, limit=1))
        if next_m:
            m = next_m[0]
            e.add_field(name="Nächstes Spiel", value=(
                f"{discord_ts(m.kickoff_utc, 'f')} · {m.home_team.name} – {m.away_team.name}"
            ), inline=False)
        else:
            e.add_field(name="Nächstes Spiel", value="⚠️ keins in der DB – stimmt SEASON? `/sync` ausführen", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @app_commands.command(name="sync", description="Season-Sync jetzt ausführen (Admin)")
    @admin_only()
    async def cmd_sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        from app.sync import sync_all_leagues

        res = await sync_all_leagues(self.client)
        self.ticker.status.last_full_sync = utcnow()
        self.ticker.wake()
        txt = ", ".join(f"{lg}: {n if n >= 0 else 'Fehler'}" for lg, n in res.items())
        await interaction.followup.send(f"✅ Sync abgeschlossen ({txt} Spiele).", ephemeral=True)

    @app_commands.command(name="kanaele", description="Spiel-Channels für den nächsten Spieltag jetzt anlegen (Admin)")
    @admin_only()
    async def cmd_kanaele(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cm = self.bot.channels
        await cm.resolve_conference_channels()
        txt = await cm.prepare(utcnow(), force=True)
        conf = ", ".join(f"{lg}: <#{settings.ticker_channel_for(lg)}>" for lg in settings.league_list)
        await interaction.followup.send(f"{txt or 'Nichts zu tun.'}\nKonferenz: {conf}", ephemeral=True)

    @app_commands.command(name="reload", description="Vereins-Aliase + Sprüche neu laden (Admin)")
    @admin_only()
    async def cmd_reload(self, interaction: discord.Interaction):
        from app import sprueche

        n = await seed_aliases()
        c = sprueche.counts()
        await interaction.response.send_message(
            f"✅ {n} Aliase gesetzt.\n💬 Sprüche: " + ", ".join(f"{k} {v}" for k, v in c.items()), ephemeral=True)

    # ── Test-Commands: posten mit echtem Layout in den Ticker-Channel ────────
    async def _sample_snapshot(self):
        async with AsyncSessionLocal() as session:
            q = (select(Match).where(Match.league_id.in_(settings.league_list), Match.is_finished.is_(True),
                                     Match.raw_json.is_not(None))
                 .order_by(Match.kickoff_utc.desc()).limit(1))
            m = (await session.execute(q)).scalars().first()
            if not m:
                m = (await session.execute(select(Match).where(Match.raw_json.is_not(None)).limit(1))).scalars().first()
        return parse_match(m.raw_json, m.league_id, m.season) if m else None

    async def _test_post(self, interaction: discord.Interaction, build):
        await interaction.response.defer(ephemeral=True)
        snap = await self._sample_snapshot()
        if not snap:
            await interaction.followup.send("Kein Spiel mit Daten in der DB – erst `/sync`.", ephemeral=True)
            return
        res = await build(snap)
        embed, file = res if isinstance(res, tuple) else (res, None)
        embed.set_author(name="🧪 TEST")
        ch = settings.ticker_channel_for(snap.league)
        msg = await self.ticker.pub.send(ch, embed, file)
        if msg:
            self.bot.test_posts.append((ch, msg.id))
            await interaction.followup.send(f"✅ Gesendet: {msg.jump_url}", ephemeral=True)
        else:
            missing = await self.ticker.pub.check_permissions(ch)
            await interaction.followup.send(
                f"❌ Senden in <#{ch}> fehlgeschlagen. Fehlend: {', '.join(missing) or 'siehe Log'}", ephemeral=True
            )

    @app_commands.command(name="test_spiel",
                          description="Test: ein ganzes Spiel in Konferenz + Spiel-Channel durchspielen (Admin)")
    @app_commands.describe(liga="Welche Liga/Konferenz getestet wird")
    @app_commands.choices(liga=LIGA_CHOICES)
    @admin_only()
    async def cmd_test_spiel(self, interaction: discord.Interaction, liga: app_commands.Choice[str] | None = None):
        import asyncio

        await interaction.response.defer(ephemeral=True, thinking=True)
        league = _lg(liga) or "bl1"
        async with AsyncSessionLocal() as session:
            m = (await session.execute(
                select(Match).where(Match.league_id == league, Match.is_finished.is_(True), Match.raw_json.is_not(None))
                .order_by(Match.kickoff_utc.desc()).limit(1))).scalars().first()
        if not m:
            await interaction.followup.send(f"Kein beendetes {league}-Spiel in der DB – erst `/sync`.", ephemeral=True)
            return
        snap = parse_match(m.raw_json, m.league_id, m.season)
        pub = self.ticker.pub
        report: list[str] = []
        targets = [("Konferenz", settings.ticker_channel_for(league))]
        if league in settings.match_channel_league_list:
            try:
                targets.append(("Spiel-Channel", await self.bot.channels.create_test_channel(snap)))
            except discord.Forbidden:
                report.append("❌ Spiel-Channel: Recht **Kanäle verwalten** fehlt")
            except discord.HTTPException as e:
                report.append(f"❌ Spiel-Channel: {e}")

        async def post(name: str, ch: int, embed, file=None) -> bool:
            embed.set_author(name="🧪 TEST")
            msg = await pub.send(ch, embed, file)
            if msg:
                self.bot.test_posts.append((ch, msg.id))
            await asyncio.sleep(0.6)  # Discord-Rate-Limit schonen
            return bool(msg)

        for label, ch in targets:
            ok = fail = 0
            steps = []
            if label == "Spiel-Channel":
                steps.append(("Vorschau", lambda: fmt.embed_match_preview(snap)))
            steps.append(("Anpfiff", None))
            for ev in snap.goals:
                steps.append(("Tor", lambda ev=ev: fmt.embed_goal(snap, ev, f"test{ev.score_key}")))
            steps += [("Halbzeit", lambda: fmt.embed_halftime(snap)), ("Abpfiff", lambda: fmt.embed_fulltime(snap))]
            for name, build in steps:
                if build is None:
                    embed, file = await fmt.embed_kickoff(snap)
                else:
                    embed, file = build(), None
                if await post(name, ch, embed, file):
                    ok += 1
                else:
                    fail += 1
            if label == "Konferenz" and league not in CUP_LEAGUES:
                rows = await get_cached_table(league, snap.season)
                if rows:
                    await post("Tabelle", ch, fmt.embed_table(rows, league, highlight_team_id=snap.home.id))
            missing = await pub.check_permissions(ch)
            report.append(f"{'✅' if not fail else '❌'} {label} <#{ch}>: {ok} Posts"
                          + (f", {fail} fehlgeschlagen – fehlt: {', '.join(missing) or 'siehe Log'}" if fail else ""))
        await interaction.followup.send(
            f"🧪 **{snap.home.name} – {snap.away.name}** ({fmt.snap_label(snap)})\n" + "\n".join(report)
            + "\n\nAlles wieder weg: `/test_aufraeumen`", ephemeral=True)

    @app_commands.command(name="test_aufraeumen", description="Alle Test-Nachrichten und Test-Channels löschen (Admin)")
    @admin_only()
    async def cmd_test_aufraeumen(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pub = self.ticker.pub
        test_channels = set(self.bot.channels.test_channels)
        n = 0
        for ch, mid in list(self.bot.test_posts):
            if ch in test_channels or await pub.delete(ch, mid):
                n += 1
            self.bot.test_posts.remove((ch, mid))
        c = await self.bot.channels.delete_test_channels()
        await interaction.followup.send(f"🧹 {n} Test-Nachrichten und {c} Test-Channel gelöscht.", ephemeral=True)

    @app_commands.command(name="leeren", description="Alle Nachrichten löschen – dieser Channel oder alle Ticker-Channels (Admin)")
    @app_commands.describe(
        ziel="Nur dieser Channel oder alle Ticker-/Konferenz- und Spiel-Channels",
        bestaetigen="Zur Sicherheit auf True setzen",
        nur_bot="Nur Nachrichten des Bots löschen (Standard: alles)",
    )
    @app_commands.choices(ziel=[
        app_commands.Choice(name="dieser Channel", value="hier"),
        app_commands.Choice(name="alle Ticker-Channels (Konferenzen + Spiel-Channels)", value="alle"),
    ])
    @admin_only()
    async def cmd_leeren(self, interaction: discord.Interaction, ziel: app_commands.Choice[str],
                         bestaetigen: bool = False, nur_bot: bool = False):
        if not bestaetigen:
            await interaction.response.send_message(
                "⚠️ Löscht unwiderruflich. Nochmal mit `bestaetigen:True` ausführen.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if ziel.value == "hier":
            ids = [interaction.channel_id]
        else:
            ids = sorted(settings.all_ticker_channels)
            cm = self.bot.channels
            ids += [e.channel_id for e in cm._map.values()] + list(cm.test_channels)
        me = self.bot.user
        lines, total = [], 0
        for cid in dict.fromkeys(ids):
            ch = await self.ticker.pub.channel(cid)
            if ch is None or not hasattr(ch, "purge"):
                continue
            try:
                # <14 Tage alte Nachrichten in 100er-Blöcken, ältere einzeln (dauert dann etwas)
                gone = await ch.purge(limit=None, check=(lambda m: m.author == me) if nur_bot else None,
                                      bulk=True, reason=f"/leeren von {interaction.user}")
                total += len(gone)
                lines.append(f"✅ <#{cid}>: {len(gone)}")
            except discord.Forbidden:
                lines.append(f"❌ <#{cid}>: Recht **Nachrichten verwalten** / **Nachrichtenverlauf lesen** fehlt")
            except discord.HTTPException as e:
                lines.append(f"❌ <#{cid}>: {e}")
        self.bot.test_posts.clear()
        logger.warning("/leeren durch %s: %d Nachrichten gelöscht", interaction.user, total)
        await interaction.followup.send(
            fmt._cut(f"🧹 {total} Nachrichten gelöscht.\n" + "\n".join(lines), 1900), ephemeral=True)

    @app_commands.command(name="test_anpfiff", description="Test: Anpfiff-Post (Admin)")
    @admin_only()
    async def cmd_test_anpfiff(self, interaction: discord.Interaction):
        await self._test_post(interaction, fmt.embed_kickoff)

    @app_commands.command(name="test_tor", description="Test: Tor-Post (Admin)")
    @admin_only()
    async def cmd_test_tor(self, interaction: discord.Interaction):
        async def build(snap):
            ev = snap.goals[0] if snap.goals else GoalEvent(1, 0, "home", 23, "F. Mustermann")
            return fmt.embed_goal(snap, ev)

        await self._test_post(interaction, build)

    @app_commands.command(name="test_halbzeit", description="Test: Halbzeit-Post (Admin)")
    @admin_only()
    async def cmd_test_halbzeit(self, interaction: discord.Interaction):
        async def build(snap):
            return fmt.embed_halftime(snap)

        await self._test_post(interaction, build)

    @app_commands.command(name="test_abpfiff", description="Test: Abpfiff-Post (Admin)")
    @admin_only()
    async def cmd_test_abpfiff(self, interaction: discord.Interaction):
        async def build(snap):
            return fmt.embed_fulltime(snap)

        await self._test_post(interaction, build)

    @app_commands.command(name="test_opener", description="Test: Spieltag-Opener (Admin)")
    @admin_only()
    async def cmd_test_opener(self, interaction: discord.Interaction):
        async def build(snap):
            async with AsyncSessionLocal() as session:
                matches = await get_matchday_matches(session, snap.matchday, snap.league, snap.season)
            return fmt.embed_matchday_opener(matches[:9], local_today())

        await self._test_post(interaction, build)

    @app_commands.command(name="test_konferenz", description="Test: Live-Konferenz (Admin)")
    @admin_only()
    async def cmd_test_konferenz(self, interaction: discord.Interaction):
        async def build(snap):
            async with AsyncSessionLocal() as session:
                matches = await get_matchday_matches(session, snap.matchday, snap.league, snap.season)
            snaps = [s for s in (parse_match(m.raw_json, m.league_id, m.season) for m in matches if m.raw_json) if s]
            # Zeit so setzen, dass die Spiele "laufen"
            now = min(s.kickoff_utc for s in snaps) + timedelta(minutes=70)
            return fmt.embed_conference(snaps, local_today(), now)

        await self._test_post(interaction, build)

    @app_commands.command(name="test_tabelle", description="Test: Tabellen-Post (Admin)")
    @admin_only()
    async def cmd_test_tabelle(self, interaction: discord.Interaction):
        async def build(snap):
            rows = await get_cached_table(snap.league, snap.season)
            return fmt.embed_table(rows, snap.league, highlight_team_id=snap.home.id)

        await self._test_post(interaction, build)
