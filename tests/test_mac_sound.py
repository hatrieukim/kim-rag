"""Sound the Mac plays out loud: the iPhone speech of those minutes is left out."""
from __future__ import annotations

import itertools
import json
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lifelog import mac_sound, pipeline
from lifelog.db import connect
from lifelog.events import store_events

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)   # 09:00 in Vietnam
_uid = itertools.count()


def adts(src, dest):
    """Raw ADTS AAC, as the iPhone app writes it."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-c:a", "aac",
                    "-b:a", "32k", "-f", "adts", str(dest)], check=True)
    return dest


def upload(client, path, start):
    meta = {"started_at": start.isoformat(), "client_id": f"iphone:{path.stem}", "source": "iphone"}
    with path.open("rb") as f:
        return client.post("/chunks", files={"audio": (path.name, f, "audio/aac")}, data={"metadata": json.dumps(meta)})


def sound(ts, on, apps=("Google Chrome",)):
    return {"uid": f"mac:sound:{next(_uid)}", "source": "mac", "type": "sound", "ts": ts,
            "payload": {"on": on, "apps": list(apps) if on else [], "output": "MacBook Pro Speakers"}}


def store(*events):
    with connect() as conn:
        store_events(conn, list(events))


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_spans_follow_heartbeats_and_drop_short_sounds():
    s = lambda sec: T0 + timedelta(seconds=sec)  # noqa: E731
    events = [
        sound(s(0), True), sound(s(60), True, ["Spotify"]), sound(s(90), False),   # one song, two apps
        sound(s(92), True), sound(s(140), False),                                  # the next song, 2 s later
        sound(s(300), True), sound(s(303), False),                                 # a notification
        sound(s(400), True),                                                       # the Mac went quiet
    ]
    got = mac_sound.spans([{"ts": e["ts"], "payload": e["payload"]} for e in events])
    assert [(g.start, g.end) for g in got] == [(s(0), s(140)), (s(400), s(400 + mac_sound.STALE_S))]
    assert got[0].apps == {"Google Chrome", "Spotify"}


def test_speech_while_the_mac_plays_is_left_out(env, audio_dir, tmp_path):
    from lifelog.api import app

    a, b = (adts(audio_dir / f"{n}.m4a", tmp_path / f"{n}.aac") for n in "ab")
    later = T0 + timedelta(minutes=30)
    store(sound(T0 - timedelta(seconds=40), True), sound(T0 + timedelta(seconds=20), True),
          sound(T0 + timedelta(seconds=50), False),
          # Only a notification while b was recorded.
          sound(later + timedelta(seconds=2), True), sound(later + timedelta(seconds=4), False))
    with TestClient(app) as client:
        assert upload(client, a, T0).status_code == 201
        assert upload(client, b, later).status_code == 201
    pipeline.drain()

    played, talked = rows("SELECT stage, speech, metadata FROM chunks ORDER BY started_at")
    assert played["stage"] == "no_speech" and played["speech"] == []
    assert played["metadata"]["speech_before_mac_sound"] and played["metadata"]["mac_sound_apps"] == ["Google Chrome"]
    assert talked["stage"] == "grouped" and "mac_sound_cut" not in talked["metadata"]


def test_phone_chunk_waits_for_the_mac_to_report(env, audio_dir, tmp_path):
    from lifelog.api import app

    a = adts(audio_dir / "a.m4a", tmp_path / "a.aac")
    start = datetime.now(timezone.utc) - timedelta(seconds=60)
    store(sound(start - timedelta(minutes=5), False))   # the Mac is on and reporting
    with TestClient(app) as client:
        assert upload(client, a, start).status_code == 201
    pipeline.drain()
    assert rows("SELECT stage FROM chunks")[0]["stage"] == "vad"

    # The report arrives: a video was playing the whole time.
    store(sound(start - timedelta(seconds=30), True, ["IINA"]), sound(start + timedelta(seconds=30), False))
    pipeline.drain()
    chunk = rows("SELECT stage, metadata FROM chunks")[0]
    assert chunk["stage"] == "no_speech" and chunk["metadata"]["mac_sound_apps"] == ["IINA"]


def test_phone_chunk_does_not_wait_for_a_mac_that_is_off(env, audio_dir, tmp_path):
    from lifelog.api import app

    a = adts(audio_dir / "a.m4a", tmp_path / "a.aac")
    with TestClient(app) as client:
        assert upload(client, a, datetime.now(timezone.utc) - timedelta(seconds=60)).status_code == 201
    pipeline.drain()
    assert rows("SELECT stage FROM chunks")[0]["stage"] == "grouped"
