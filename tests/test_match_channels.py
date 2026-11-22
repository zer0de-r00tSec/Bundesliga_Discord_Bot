"""Spiel-Channels: Ticker postet doppelt (Konferenz + Spiel-Channel), ChannelManager baut je Spieltag um."""
from __future__ import annotations

import asyncio
import types
from datetime import timedelta

from app import channels as chm
from tests.fixtures import clone, goal, match, result
from tests.test_ticker import KO, env, run  # noqa: F401  (Fixture wiederverwenden)


class FakeChannels:
    enabled = True

    def __init__(self, ids):
        self.ids = ids
        self.status: list[tuple[int, str]] = []

    async def start(self):
        pass

    async def prepare(self, now=None, force=False):
        return ""

    def channel_for(self, mid):
        return self.ids.get(mid)

    def mark_status(self, mid, status):
        self.status.append((mid, status))


def test_events_go_to_conference_and_match_channel(env):
    api, pub, store = env.api, env.pub, env.store
    a = match(70001, 40, 7, KO)
    b = match(70002, 6, 1635, KO)
    api.set(clone(a)); api.set(clone(b))
    fc = FakeChannels({70001: 501, 70002: 502})
    t = env.make(); t.channels = fc

    run(t, store, KO + timedelta(minutes=1))
    assert [ch for ch, e in pub.sent if e.title == "🟢 Anpfiff!"] == [100, 501, 100, 502]
    assert (70001, "live") in fc.status

    a["goals"] = [goal(1, 1, 0, 11, "Harry Kane")]
    api.set(clone(a))
    run(t, store, KO + timedelta(minutes=12))
    goals = [(ch, e.title) for ch, e in pub.sent if e.title.startswith("⚽")]
    assert [ch for ch, _ in goals] == [100, 501]
    assert "mc:goal:70001:1-0" in store.posted and store.posted["mc:goal:70001:1-0"].channel_id == 501

    a["matchResults"] = [result(1, 1, 0), result(2, 1, 0)]; a["matchIsFinished"] = True
    b["matchResults"] = [result(1, 0, 0), result(2, 0, 0)]; b["matchIsFinished"] = True
    api.set(clone(a)); api.set(clone(b))
    run(t, store, KO + timedelta(minutes=110))
    ft = [ch for ch, e in pub.sent if e.title == "🏁 Abpfiff!"]
    assert sorted(ft) == [100, 100, 501, 502]
    assert (70002, "ft") in fc.status

    # Neustart: keine Duplikate in keinem Channel
    before = len(pub.sent)
    t2 = env.make(); t2.channels = fc
    run(t2, store, KO + timedelta(minutes=111))
    assert [e.title for _, e in pub.sent[before:] if not e.title.startswith(("📡", "📊"))] == []


def test_match_channel_created_late_gets_only_one_card(env):
    api, pub, store = env.api, env.pub, env.store
    a = match(70005, 40, 7, KO, goals=[goal(1, 1, 0, 5, "Kane")], results=[])
    api.set(clone(a))
    fc = FakeChannels({})
    t = env.make(); t.channels = fc
    run(t, store, KO + timedelta(minutes=2))
    fc.ids[70005] = 777  # Channel erst jetzt angelegt
    run(t, store, KO + timedelta(minutes=30))
    in_mc = [e.title for ch, e in pub.sent if ch == 777]
    assert len(in_mc) == 1 and in_mc[0].startswith("📡 Live dabei")


# ── ChannelManager gegen einen Fake-Discord-Server ───────────────────────────

class FakeCh:
    def __init__(self, guild, cid, name, category=None, topic=None):
        self.guild, self.id, self.name, self.category, self.topic = guild, cid, name, category, topic
        self.channels = []

    async def delete(self, reason=None):
        self.guild.chans.pop(self.id, None)
        if self.category:
            self.category.channels.remove(self)

    async def edit(self, name=None, reason=None):
        self.name = name


class FakeGuild:
    def __init__(self):
        self.chans: dict[int, FakeCh] = {}
        self.cats: list[FakeCh] = []
        self._id = 900
        self.me = types.SimpleNamespace(guild_permissions=types.SimpleNamespace(manage_channels=True))

    @property
    def categories(self):
        return self.cats

    @property
    def text_channels(self):
        return list(self.chans.values())

    def get_channel(self, cid):
        return self.chans.get(cid)

    async def create_category(self, name, reason=None):
        self._id += 1
        c = FakeCh(self, self._id, name)
        self.cats.append(c)
        return c

    async def create_text_channel(self, name, category=None, topic=None, reason=None):
        self._id += 1
        c = FakeCh(self, self._id, name, category, topic)
        self.chans[c.id] = c
        if category:
            category.channels.append(c)
        return c


def _row(mid, home, away, short_h, short_a, md, ko, league="bl1", finished=False):
    return types.SimpleNamespace(
        match_id=mid, league_id=league, season=2026, matchday=md, kickoff_utc=ko, is_finished=finished,
        group_name=f"{md}. Spieltag", raw_json=match(mid, 40, 7, ko, day=md),
        home_team=types.SimpleNamespace(name=home, short_name=short_h),
        away_team=types.SimpleNamespace(name=away, short_name=short_a),
    )


def test_channel_manager_builds_and_rotates(monkeypatch):
    db: dict[int, types.SimpleNamespace] = {}
    saved: dict[int, int] = {}

    class Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, q):
            return types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: list(db.values())))

    async def between(session, s, e, league=None):
        return sorted([m for m in db.values() if s <= m.kickoff_utc < e], key=lambda m: m.kickoff_utc)

    async def md_matches(session, md, lg=None, season=None):
        return [m for m in db.values() if m.matchday == md and m.league_id == lg]

    repo = types.SimpleNamespace(
        all_channels=lambda: asyncio.sleep(0, []),
        save=lambda mid, lg, s, md, cid: asyncio.sleep(0, saved.__setitem__(mid, cid)),
        set_status=lambda mid, st: asyncio.sleep(0),
        remove=lambda ids: asyncio.sleep(0, [saved.pop(i, None) for i in ids]),
    )
    monkeypatch.setattr(chm, "AsyncSessionLocal", Sess)
    monkeypatch.setattr(chm, "get_matches_between", between)
    monkeypatch.setattr(chm, "get_matchday_matches", md_matches)
    monkeypatch.setattr(chm, "ch_repo", repo)
    monkeypatch.setattr(chm, "select", lambda *a: types.SimpleNamespace(where=lambda *x: None))
    monkeypatch.setattr(chm, "Match", types.SimpleNamespace(match_id=types.SimpleNamespace(in_=lambda x: None)))

    guild = FakeGuild()
    bot = types.SimpleNamespace(get_guild=lambda gid: guild)
    sent = []

    class Pub:
        async def send(self, ch, embed, file=None):
            sent.append((ch, embed.title))
            return types.SimpleNamespace(id=1)

        def forget(self, cid):
            pass

    cm = chm.ChannelManager(bot, Pub())

    db[1] = _row(1, "Borussia Dortmund", "1. FC Köln", "Dortmund", "Köln", 11, KO + timedelta(hours=2))
    db[2] = _row(2, "FC Bayern München", "Bayer 04 Leverkusen", "Bayern", "Leverkusen", 11, KO)
    db[3] = _row(3, "FC St. Pauli", "VfL Bochum", "St. Pauli", "Bochum", 11, KO, league="bl2")
    db[4] = _row(4, "Verlegt A", "Verlegt B", "A", "B", 11, KO + timedelta(days=20))  # verlegtes Spiel

    asyncio.run(cm.start())
    out = asyncio.run(cm.prepare(KO - timedelta(hours=10)))
    names = {c.category.name: [] for c in guild.chans.values()}
    for c in guild.chans.values():
        names[c.category.name].append(c.name)
    assert names == {"Bundesliga 1": ["bayern-vs-leverkusen", "dortmund-vs-köln"],
                     "Bundesliga 2": ["st.-pauli-vs-bochum".replace(".", "")]}
    assert "3 Channel angelegt" in out and len(sent) == 3
    assert cm.channel_for(4) is None  # liegt zu weit weg

    # Spiel läuft → 🔴 im Namen
    async def status():
        cm.mark_status(2, "live")
        await asyncio.gather(*cm._tasks)
    asyncio.run(status())
    assert guild.get_channel(cm.channel_for(2)).name == "🔴bayern-vs-leverkusen"

    # Spieltag 11 vorbei, Spieltag 12 rückt näher → neu anlegen, alte (beendete) löschen
    for m in (1, 2, 3):
        db[m].is_finished = True
    nxt = KO + timedelta(days=7)
    db[10] = _row(10, "Hamburger SV", "SV Werder Bremen", "HSV", "Werder", 12, nxt)
    old_ids = {cm.channel_for(1), cm.channel_for(2)}
    asyncio.run(cm.prepare(nxt - timedelta(hours=5)))
    bl1 = [c.name for c in guild.chans.values() if c.category.name == "Bundesliga 1"]
    assert bl1 == ["hsv-vs-werder"]
    assert not (old_ids & set(guild.chans))
    # 2. Liga hat noch keinen neuen Spieltag → ihr Channel bleibt
    assert [c.name for c in guild.chans.values() if c.category.name == "Bundesliga 2"] == ["st-pauli-vs-bochum"]
