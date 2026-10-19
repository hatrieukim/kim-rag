"""Calendar events from the Mac app: stored, kept up to date, used for speaker names and the day journal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lifelog import calendar, pipeline, voices
from lifelog.db import connect
from lifelog.journal import signature

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)   # 09:00 in Vietnam


def event(uid: str, key: str, title: str, start_min: float, end_min: float, attendees=(), **extra) -> dict:
    payload = {"key": key, "title": title, "start": (T0 + timedelta(minutes=start_min)).isoformat(),
               "end": (T0 + timedelta(minutes=end_min)).isoformat(), "calendar": "Work",
               "attendees": list(attendees), **extra}
    return {"uid": uid, "source": "calendar", "type": "event", "ts": payload["start"], "payload": payload}


ME = {"name": "Kim", "email": "kim@example.com", "status": "accepted", "me": True}
TUAN = {"name": "Tuấn Nguyễn", "email": "tuan@example.com", "status": "accepted", "me": False}
LAN = {"name": None, "email": "lan@example.com", "status": "tentative", "me": False}
NO = {"name": "Hùng", "email": "hung@example.com", "status": "declined", "me": False}


def post(batch):
    from lifelog.api import app

    with TestClient(app) as client:
        assert client.post("/events", json=batch).status_code == 200
    pipeline.drain()


def test_events_are_updated_and_deleted(env):
    post([
        event("c1", "A@1", "Họp kế hoạch quý", 0, 60, [ME, TUAN, LAN, NO]),
        event("c2", "B@1", "Ăn trưa", 180, 240),
        event("c3", "C@1", "Nghỉ lễ", -120, 1320, all_day=True),
    ])
    post([
        event("c4", "A@1", "Họp kế hoạch quý tháng 10", 0, 90, [ME, TUAN, LAN, NO]),   # edited: same key, new content
        {"uid": "c5", "source": "calendar", "type": "snapshot", "ts": T0.isoformat(),
         "payload": {"from": (T0 - timedelta(days=1)).isoformat(), "to": (T0 + timedelta(days=1)).isoformat(),
                     "keys": ["A@1", "C@1"]}},                                     # B was deleted
    ])
    with connect() as conn:
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM calendar_events")}
        stages = {r["stage"] for r in conn.execute("SELECT stage FROM events WHERE source = 'calendar'")}
        assert [m["title"] for m in calendar.meetings(conn, T0 - timedelta(hours=1), T0 + timedelta(hours=8))] == \
            ["Họp kế hoạch quý tháng 10"]                                               # all-day and deleted left out
        journal = calendar.render_day(conn, T0.date())
    assert stages == {"grouped"}
    assert rows["A@1"]["title"] == "Họp kế hoạch quý tháng 10" and not rows["A@1"]["cancelled"]
    assert rows["B@1"]["cancelled"]
    assert calendar.people(rows["A@1"]["attendees"]) == ["Tuấn Nguyễn", "lan"]     # not me, not who declined
    assert journal == ["## Lịch", "", "- 09:00–10:30 Họp kế hoạch quý tháng 10 (với Tuấn Nguyễn, lan)", ""]


def test_meeting_attendees_are_offered_for_voices(env, audio_dir, monkeypatch):
    from lifelog import summarize
    from lifelog.diarize import DiarizeResult
    from lifelog.ingest import ingest_file

    monkeypatch.setattr(voices, "MIN_VOICE_S", 0.5)
    seen = []
    monkeypatch.setattr(summarize, "suggest_speakers",
                        lambda transcript, labels, context="": seen.append(context) or {})

    class OneVoice:
        def run(self, samples, speech):
            return DiarizeResult(turns=[(0.2, len(samples) / 16_000 - 0.2, "A")], embeddings={"A": [0, 1, 0]},
                                 model="fake-v1")

    post([event("c1", "A@1", "Họp kế hoạch quý", -5, 60, [ME, TUAN])])
    monkeypatch.setattr(pipeline, "get_diarizer", lambda _: OneVoice())
    ingest_file(audio_dir / "a.m4a", started_at=T0 + timedelta(minutes=10), close_episode=True)
    pipeline.drain()

    with connect() as conn:
        meta = conn.execute("SELECT meta FROM episodes WHERE kind = 'voice'").fetchone()["meta"]
    assert meta["calendar_people"] == ["Tuấn Nguyễn"] and meta["calendar_titles"] == ["Họp kế hoạch quý"]
    assert seen == ['meeting "Họp kế hoạch quý", invited: Tuấn Nguyễn']
    waiting = voices.queue()            # one short voice on one day, but recorded in a meeting
    assert len(waiting) == 1 and waiting[0]["invited"] == ["Tuấn Nguyễn"]


def test_my_other_emails_count_as_me(env, monkeypatch):
    from lifelog.config import get_settings

    monkeypatch.setenv("MY_EMAILS", "kim@work.example, Kim.Personal@Example.org")
    get_settings.cache_clear()
    bun = {"name": "Bun", "email": " kim.personal@example.org ", "status": "accepted", "me": False}   # lu.ma invite
    post([event("c1", "A@1", "Meetup", 0, 60, [bun, TUAN], organizer={**bun, "status": None})])
    with connect() as conn:
        row = conn.execute("SELECT attendees, organizer FROM calendar_events").fetchone()
    assert [a["me"] for a in row["attendees"]] == [True, False] and row["organizer"]["me"]
    assert calendar.people(row["attendees"]) == ["Tuấn Nguyễn"]
    assert calendar.people([bun, LAN]) == ["lan"]                  # rows stored before the setting existed


def test_journal_signature_counts_meetings():
    assert signature(2, 0, 0) == "2"                     # old journals keep their signature
    assert signature(2, 5, 1, 0, 3) == "2|m5|g1|c3"
