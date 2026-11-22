"""Simuliert einen kompletten Spieltag mit Fake-DB, Fake-API und Fake-Discord."""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timedelta

import pytest

from app import ticker as tk
from app.config import settings
from app.sync import parse_all
from app.timeutil import local_day_bounds_utc
from tests.fixtures import clone, goal, match, result

KO = datetime(2026, 11, 14, 14, 30)  # 15:30 Berlin


class Store:
    def __init__(self):
        self.posted: dict[str, types.SimpleNamespace] = {}
        self.events: dict[str, types.SimpleNamespace] = {}
        self.now = KO

    # ev_repo-API
    async def posted_keys_for(self, ids):
        return {k for k, v in self.posted.items() if v.match_id in ids}

    async def get_posted(self, key):
        return self.posted.get(key)

    async def get_posted_many(self, keys):
        return [self.posted[k] for k in keys if k in self.posted]

    async def is_posted(self, key):
        return key in self.posted

    async def mark_posted(self, key, mid, ch, msg_id):
        self.posted[key] = types.SimpleNamespace(event_key=key, match_id=mid, channel_id=ch, message_id=msg_id,
                                                 posted_at=self.now)

    async def unmark_posted(self, keys):
        for k in keys:
            self.posted.pop(k, None)

    async def get_event(self, key):
        return self.events.get(key)

    async def upsert_event(self, key, mid, typ, **kw):
        self.events[key] = types.SimpleNamespace(event_key=key, **kw)


class FakeAPI:
    def __init__(self):
        self.raws: dict[int, dict] = {}
        self.version = 0
        self.table = [{"teamInfoId": 40, "teamName": "FC Bayern München", "shortName": "Bayern",
                       "points": 25, "goalDiff": 20, "matches": 10}]

    def set(self, raw):
        self.raws[raw["matchID"]] = raw
        self.version += 1

    async def get_last_change_date(self, lg, season, day):
        return f"2026-11-14T15:{self.version:02d}:00.5"


class FakePub:
    def __init__(self):
        self.sent: list[tuple[int, object]] = []
        self.edits: list[tuple[int, int, object]] = []
        self.fail = False
        self._id = 1000

    async def send(self, ch, embed, file=None, content=None):
        if self.fail:
            return None
        self._id += 1
        self.sent.append((ch, embed))
        return types.SimpleNamespace(id=self._id, jump_url="x")

    async def edit(self, ch, mid, embed):
        self.edits.append((ch, mid, embed))
        return True

    async def check_permissions(self, ch):
        return []

    def titles(self):
        return [e.title for _, e in self.sent]


class DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def env(monkeypatch):
    store, api, pub = Store(), FakeAPI(), FakePub()
    db: dict[int, types.SimpleNamespace] = {}

    def to_row(snap):
        return types.SimpleNamespace(
            match_id=snap.match_id, league_id=snap.league, season=snap.season, matchday=snap.matchday,
            kickoff_utc=snap.kickoff_utc, is_finished=snap.finished, raw_json=snap.raw,
            home_team=types.SimpleNamespace(name=snap.home.name), away_team=types.SimpleNamespace(name=snap.away.name),
        )

    async def sync_matchday(client, lg, season, day):
        snaps = parse_all([r for r in api.raws.values() if r["group"]["groupOrderID"] == day], lg, season)
        for s in snaps:
            db[s.match_id] = to_row(s)
        return snaps

    async def sync_all(client):
        await sync_matchday(client, "bl1", 2026, 10)
        return {"bl1": len(db)}

    async def between(session, start, end, league=None):
        return sorted([m for m in db.values() if start <= m.kickoff_utc < end], key=lambda m: m.kickoff_utc)

    async def day_matches(session, day=None, league=None):
        s, e = local_day_bounds_utc(day)
        return await between(session, s, e)

    async def next_ko(session, after=None):
        ks = [m.kickoff_utc for m in db.values() if m.kickoff_utc > after]
        return min(ks) if ks else None

    async def md_matches(session, md, lg=None, season=None):
        return [m for m in db.values() if m.matchday == md]

    async def table(client, lg, season):
        return api.table

    monkeypatch.setattr(tk, "ev_repo", store)
    monkeypatch.setattr(tk, "AsyncSessionLocal", DummySession)
    monkeypatch.setattr(tk, "sync_matchday", sync_matchday)
    monkeypatch.setattr(tk, "sync_all_leagues", sync_all)
    monkeypatch.setattr(tk, "sync_table_cache", table)
    monkeypatch.setattr(tk, "get_matches_between", between)
    monkeypatch.setattr(tk, "get_day_matches", day_matches)
    monkeypatch.setattr(tk, "get_next_kickoff", next_ko)
    monkeypatch.setattr(tk, "get_matchday_matches", md_matches)
    monkeypatch.setattr(tk, "FORCE_REFETCH_SECONDS", 0)

    async def no_image(*a, **k):
        return None

    monkeypatch.setattr(tk.fmt, "compose_vs_image", no_image)

    def make_ticker():
        return tk.Ticker(client=api, bot=None, publisher=pub)

    return types.SimpleNamespace(store=store, api=api, pub=pub, db=db, make=make_ticker)


def run(t, store, now):
    store.now = now
    return asyncio.run(t.tick(now))


def test_full_matchday(env):
    api, pub, store = env.api, env.pub, env.store
    a = match(70001, 40, 7, KO)
    b = match(70002, 6, 1635, KO)
    api.set(clone(a)); api.set(clone(b))
    t = env.make()

    # 40 Min. vorher: nichts, schläft bis Vorlauf
    delay = run(t, store, KO - timedelta(minutes=40))
    assert pub.sent == [] and 500 < delay <= 600

    # 29 Min. vorher: Opener
    run(t, store, KO - timedelta(minutes=29))
    assert pub.titles() == ["🏟 Fußball am 14.11.2026 – Los geht's!"]

    # Anpfiff: 2x Anpfiff + Konferenz, Poll-Intervall
    delay = run(t, store, KO + timedelta(minutes=1))
    assert pub.titles()[1:] == ["🟢 Anpfiff!", "🟢 Anpfiff!", "📡 Live-Konferenz · 14.11.2026"]
    assert delay == settings.poll_interval_live_seconds
    n = len(pub.sent)

    # Tor ohne Schütze → Post "wird nachgetragen"
    a["goals"] = [goal(1, 1, 0, None, None)]
    api.set(clone(a))
    run(t, store, KO + timedelta(minutes=12))
    goal_posts = [e for _, e in pub.sent[n:] if e.title.startswith("⚽")]
    assert len(goal_posts) == 1 and "Bayern" in goal_posts[0].title
    assert "nachgetragen" in goal_posts[0].fields[0].value

    # Schütze nachgetragen → Edit statt neuer Post
    a["goals"] = [goal(1, 1, 0, 11, "Harry Kane")]
    api.set(clone(a))
    before = len(pub.sent)
    run(t, store, KO + timedelta(minutes=14))
    assert len([e for _, e in pub.sent[before:] if e.title.startswith("⚽")]) == 0
    goal_edits = [e for _, _, e in pub.edits if e.title and e.title.startswith("⚽")]
    assert goal_edits and goal_edits[-1].fields[0].value == "Harry Kane"

    # Ausgleich + Halbzeit
    a["goals"].append(goal(2, 1, 1, 40, "Guirassy"))
    a["matchResults"] = [result(1, 1, 1)]
    api.set(clone(a))
    run(t, store, KO + timedelta(minutes=50))
    assert "⏸ Halbzeit" in pub.titles()
    assert any("Dortmund" in x for x in pub.titles() if x.startswith("⚽"))

    # gleicher Stand nochmal → keine Duplikate
    before = len(pub.sent)
    run(t, store, KO + timedelta(minutes=51))
    assert [x for x in pub.titles()[before:] if not x.startswith("📡")] == []

    # VAR: 1:1 zurückgenommen → Korrektur, später erneut 1:1 → wieder gemeldet
    a["goals"] = a["goals"][:1]
    a["matchResults"] = [result(1, 1, 0)]
    api.set(clone(a))
    run(t, store, KO + timedelta(minutes=70))
    assert pub.titles()[-1].startswith("⚠️ Korrektur")
    a["goals"].append(goal(3, 1, 1, 75, "Adeyemi"))
    api.set(clone(a))
    before = len(pub.sent)
    run(t, store, KO + timedelta(minutes=80))
    assert any(x.startswith("⚽") for x in pub.titles()[before:])

    # Abpfiff beider Spiele
    a["goals"].append(goal(4, 2, 1, 93, "Musiala"))
    a["matchResults"] = [result(1, 1, 0), result(2, 2, 1)]
    a["matchIsFinished"] = True
    b["matchResults"] = [result(1, 0, 0), result(2, 0, 0)]
    b["matchIsFinished"] = True
    api.set(clone(a)); api.set(clone(b))
    run(t, store, KO + timedelta(minutes=112))
    ft = [e for _, e in pub.sent if e.title == "🏁 Abpfiff!"]
    assert len(ft) == 2
    assert "2 : 1" in ft[0].description and "Musiala" in ft[0].fields[-1].value
    assert not any(x.startswith("📊") for x in pub.titles())  # Tabelle erst nach Verzögerung

    run(t, store, KO + timedelta(minutes=116))
    assert pub.titles()[-1].startswith("📊 Tabelle nach dem 10. Spieltag")

    # Neustart: neuer Ticker, gleicher Speicher → keine Duplikate
    before = len(pub.sent)
    t2 = env.make()
    delay = run(t2, store, KO + timedelta(minutes=118))
    assert [x for x in pub.titles()[before:] if not x.startswith("📡")] == []
    assert delay >= 30


def test_catch_up_finished_match_only_posts_fulltime(env):
    api, pub, store = env.api, env.pub, env.store
    a = match(70010, 16, 91, KO, goals=[goal(1, 1, 0, 5, "Undav")],
              results=[result(1, 1, 0), result(2, 1, 0)], finished=True)
    api.set(a)
    t = env.make()
    run(t, store, KO + timedelta(minutes=150))
    assert [x for x in pub.titles() if not x.startswith("📡")] == ["🏁 Abpfiff!"]
    assert "goal:70010:1-0" in store.posted and store.posted["goal:70010:1-0"].message_id is None
    # kein Tabellen-Post, weil nicht live begleitet
    run(t, store, KO + timedelta(minutes=160))
    assert not any(x.startswith("📊") for x in pub.titles())


def test_discord_failure_is_retried(env):
    api, pub, store = env.api, env.pub, env.store
    api.set(match(70020, 40, 7, KO))
    t = env.make()
    pub.fail = True
    run(t, store, KO + timedelta(minutes=2))
    assert "ko:70020" not in store.posted
    pub.fail = False
    run(t, store, KO + timedelta(minutes=3))
    assert "ko:70020" in store.posted and "🟢 Anpfiff!" in pub.titles()


def test_long_match_still_tracked_after_115_minutes(env):
    """Alter Bug: nach 115 Min. wurde nicht mehr gepollt → Abpfiff verpasst."""
    api, pub, store = env.api, env.pub, env.store
    a = match(70030, 40, 7, KO)
    api.set(clone(a))
    t = env.make()
    run(t, store, KO + timedelta(minutes=1))
    a["matchIsFinished"] = True
    a["matchResults"] = [result(2, 0, 0)]
    api.set(clone(a))
    run(t, store, KO + timedelta(minutes=125))
    assert "🏁 Abpfiff!" in pub.titles()


def test_halftime_not_posted_right_after_kickoff(env):
    """Bug 22.11.: OpenLigaDB trägt 'Halbzeit 0:0' schon beim Anpfiff ein → Halbzeit nach 3 Min."""
    api, pub, store = env.api, env.pub, env.store
    a = match(70040, 40, 7, KO, results=[result(1, 0, 0), result(2, 0, 0)])
    api.set(clone(a))
    t = env.make()
    run(t, store, KO + timedelta(minutes=1))
    run(t, store, KO + timedelta(minutes=3))
    assert "🟢 Anpfiff!" in pub.titles()
    assert "⏸ Halbzeit" not in pub.titles() and "ht:70040" not in store.posted

    # Tor in der 1. Hälfte: HZ-Ergebnis wird live mitgezogen
    a["goals"] = [goal(1, 0, 1, 30, "Guirassy")]
    a["matchResults"] = [result(1, 0, 1), result(2, 0, 1)]
    api.set(clone(a))
    run(t, store, KO + timedelta(minutes=31))
    run(t, store, KO + timedelta(minutes=47))
    assert "⏸ Halbzeit" not in pub.titles()

    run(t, store, KO + timedelta(minutes=51))
    ht = [e for _, e in pub.sent if e.title == "⏸ Halbzeit"]
    assert len(ht) == 1 and "0 : 1" in ht[0].description
    run(t, store, KO + timedelta(minutes=55))
    assert len([x for x in pub.titles() if x == "⏸ Halbzeit"]) == 1


def test_halftime_skipped_when_bot_was_down_during_break(env):
    """Bot war in der Pause weg → keine verspätete Halbzeit mitten in der 2. Hälfte."""
    api, pub, store = env.api, env.pub, env.store
    a = match(70041, 40, 7, KO, results=[result(1, 0, 0), result(2, 0, 0)])
    api.set(clone(a))
    t = env.make()
    run(t, store, KO + timedelta(minutes=1))
    run(t, store, KO + timedelta(minutes=80))
    assert "⏸ Halbzeit" not in pub.titles() and "ht:70041" in store.posted


def test_late_join_keeps_upcoming_halftime(env):
    """Einstieg nach 20 Min.: Halbzeit darf nicht schon still markiert werden."""
    api, pub, store = env.api, env.pub, env.store
    a = match(70042, 40, 7, KO, results=[result(1, 0, 0), result(2, 0, 0)])
    api.set(clone(a))
    t = env.make()
    run(t, store, KO + timedelta(minutes=20))
    assert any(x.startswith("📡 Live dabei") for x in pub.titles())
    assert "ht:70042" not in store.posted
    run(t, store, KO + timedelta(minutes=52))
    assert "⏸ Halbzeit" in pub.titles()
