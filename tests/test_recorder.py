"""Product 5: chunks from the iPhone recorder, their sidecar and gaps."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lifelog import pipeline
from lifelog.audio import decode, probe
from lifelog.db import connect
from lifelog.recorder import day_report

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)   # 09:00 in Vietnam


def adts(src, dest):
    """Raw ADTS AAC, as the iPhone app writes it."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-c:a", "aac",
                    "-b:a", "32k", "-f", "adts", str(dest)], check=True)
    return dest


def upload(client, path, start, **meta):
    meta = {"started_at": start.isoformat(), "client_id": f"iphone:{path.stem}", "source": "iphone", **meta}
    with path.open("rb") as f:
        return client.post("/chunks", files={"audio": (path.name, f, "audio/aac")}, data={"metadata": json.dumps(meta)})


def test_adts_length_is_exact(audio_dir, tmp_path):
    path = adts(audio_dir / "a.m4a", tmp_path / "a.aac")
    duration, _ = probe(path)
    assert abs(duration - len(decode(path)) / 16_000) < 1e-6


def test_chunks_and_gaps(env, audio_dir, tmp_path):
    from lifelog.api import app

    a, b, c = (adts(audio_dir / f"{n}.m4a", tmp_path / f"{n}.aac") for n in "abc")
    len_a = probe(a)[0]
    with TestClient(app) as client:
        # a: ends by rotation, the next chunk starts right after it
        assert upload(client, a, T0, end_reason="rotate", input="iPhone Microphone",
                      battery_start=0.8, location={"lat": 10.77, "lon": 106.7, "accuracy": 65}).status_code == 201
        # b: a phone call interrupts it; recording resumes 2 minutes later
        start_b = T0 + timedelta(seconds=len_a)
        assert upload(client, b, start_b, end_reason="interruption", call=True).status_code == 201
        start_c = start_b + timedelta(seconds=probe(b)[0] + 120)
        assert upload(client, c, start_c, end_reason="stop", close_episode=True).status_code == 201
        assert upload(client, c, start_c, end_reason="stop").status_code == 200        # resent: not stored twice
        report = client.get("/recorder/day", params={"day": "2026-10-12"}).json()

    assert report["chunks"] == 3 and report["calls"] == 1
    assert [(g["reason"], g["seconds"]) for g in report["gaps"]] == [("interruption", 120)]
    assert report["lost_s"] == 120
    with connect() as conn:
        meta = conn.execute("SELECT metadata FROM chunks WHERE client_id = 'iphone:a'").fetchone()["metadata"]
    assert meta["location"]["lat"] == 10.77 and meta["input"] == "iPhone Microphone"


def test_day_without_recordings(env):
    r = day_report(T0.date())
    assert r.chunks == 0 and r.lost_s == 0 and r.gaps == []


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_sound_played_on_the_phone_speaker_is_not_speech(env, audio_dir, tmp_path, monkeypatch):
    from lifelog.api import app
    from lifelog.config import get_settings

    monkeypatch.setenv("WEAK_CHUNK_SPEECH_S", "0")   # what is left of b may be short; keep it anyway
    get_settings.cache_clear()

    a, b = (adts(audio_dir / f"{n}.m4a", tmp_path / f"{n}.aac") for n in "ab")
    with TestClient(app) as client:
        upload(client, a, T0, playback=[[0, 600]])
        upload(client, b, T0 + timedelta(minutes=30), playback=[[0, 3]])
    pipeline.drain()
    whole, part = rows("SELECT stage, speech, metadata FROM chunks ORDER BY started_at")
    assert whole["stage"] == "no_speech" and whole["speech"] == [] and whole["metadata"]["speech_before_playback"]
    assert part["stage"] == "grouped" and part["speech"][0][0] >= 3


def test_a_few_seconds_of_speech_neither_opens_nor_extends_an_episode(env, audio_dir, tmp_path, monkeypatch):
    from lifelog.api import app
    from lifelog.config import get_settings

    a, b, c = (adts(audio_dir / f"{n}.m4a", tmp_path / f"{n}.aac") for n in "abc")
    with TestClient(app) as client:
        upload(client, a, T0)
        upload(client, b, T0 + timedelta(minutes=5))
        upload(client, c, T0 + timedelta(minutes=30))
    # Every test sentence is longer than 5 s, so raise the bar until b and c count as stray sounds.
    monkeypatch.setenv("WEAK_CHUNK_SPEECH_S", "1000")
    get_settings.cache_clear()
    with connect() as conn:
        # a stays strong: it is uploaded from a source the rule does not touch.
        conn.execute("UPDATE chunks SET source = 'upload' WHERE client_id = 'iphone:a'")
    pipeline.drain()
    ca, cb, cc = rows("SELECT stage, episode_id, started_at, speech FROM chunks ORDER BY started_at")
    assert cb["stage"] == "grouped" and cb["episode_id"] == ca["episode_id"]     # joins the open conversation
    assert cc["stage"] == "no_speech" and cc["episode_id"] is None               # alone: dropped
    (ep,) = rows("SELECT ended_at FROM episodes")
    assert ep["ended_at"] == ca["started_at"] + timedelta(seconds=ca["speech"][-1][1])   # not extended by b
    assert rows("SELECT metadata->'weak' AS w FROM chunks WHERE client_id = 'iphone:c'")[0]["w"] is True


def test_mac_room_recording_has_its_own_report(env, audio_dir):
    """While the iPhone is off the Mac records the room; its chunks are reported apart, and sleep is no loss."""
    from lifelog.api import app
    from lifelog.recorder import MAC_SOURCE

    a, b = audio_dir / "a.m4a", audio_dir / "b.m4a"
    start_b = T0 + timedelta(seconds=probe(a)[0] + 600)
    with TestClient(app) as client:
        for path, start, reason in [(a, T0, "sleep"), (b, start_b, "close")]:
            meta = {"started_at": start.isoformat(), "client_id": f"mac-room:{path.stem}", "source": MAC_SOURCE,
                    "mic": "MacBook Pro Microphone", "end_reason": reason, "close_episode": reason == "close"}
            with path.open("rb") as f:
                assert client.post("/chunks", files={"audio": (path.name, f, "audio/mp4")},
                                   data={"metadata": json.dumps(meta)}).status_code == 201
        room = client.get("/recorder/day", params={"day": "2026-10-12", "source": MAC_SOURCE}).json()
        phone = client.get("/recorder/day", params={"day": "2026-10-12"}).json()

    assert phone["chunks"] == 0
    assert room["chunks"] == 2 and [g["reason"] for g in room["gaps"]] == ["sleep"] and room["lost_s"] == 0
    pipeline.drain()
    # Handled like the iPhone recording: both chunks become conversations (ten minutes apart, so two).
    assert [r["episode_id"] is not None for r in rows("SELECT episode_id FROM chunks ORDER BY started_at")] == [True, True]
