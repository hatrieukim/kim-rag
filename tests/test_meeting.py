"""Product 8: online meetings recorded on the Mac, and the iPhone audio of the same minutes."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifelog import pipeline
from lifelog.audio import probe
from lifelog.db import connect
from lifelog.events import store_events
from lifelog.vad import subtract_spans as _subtract

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)   # 09:00 in Vietnam


def stereo(mic, app, dest, seconds):
    """Two-channel file as the Mac app writes it: mic on the left, the meeting app on the right."""
    def src(path):
        return ["-i", str(path)] if path else ["-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=r=16000:cl=mono"]

    subprocess.run(["ffmpeg", "-v", "error", "-y", *src(mic), *src(app), "-filter_complex",
                    f"[0:a]apad=whole_dur={seconds}[l];[1:a]apad=whole_dur={seconds}[r];"
                    "[l][r]join=inputs=2:channel_layout=stereo[a]",
                    "-map", "[a]", "-ar", "16000", "-c:a", "aac", "-t", str(seconds), str(dest)], check=True)
    return dest


def upload(client, path, start, source, client_id, **meta):
    meta = {"started_at": start.isoformat(), "client_id": client_id, "source": source, **meta}
    with path.open("rb") as f:
        return client.post("/chunks", files={"audio": (path.name, f, "audio/mp4")}, data={"metadata": json.dumps(meta)})


def event(kind, ts, meeting_id="m1"):
    with connect() as conn:
        store_events(conn, [{"uid": f"mac:{kind}:{meeting_id}", "source": "mac", "type": kind, "ts": ts,
                             "payload": {"meeting_id": meeting_id, "app": "Microsoft Teams"}}])


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_subtract_cuts_speech_inside_the_meeting():
    assert _subtract([[0, 10], [20, 30]], [(5, 25)]) == [[0, 5], [25, 30]]
    assert _subtract([[0, 10]], [(-5, 60)]) == []
    assert _subtract([[0, 10]], [(10, 20)]) == [[0, 10]]


@pytest.mark.parametrize("phone", ["iphone", "mac-room"])
def test_meeting_is_its_own_episode_and_replaces_phone_audio(env, audio_dir, tmp_path, phone):
    with connect() as conn:
        conn.execute("INSERT INTO speakers (name, status, is_me) VALUES ('Kim', 'named', true)")
    length = probe(audio_dir / "a.m4a")[0] + 2
    mic_only = stereo(audio_dir / "a.m4a", None, tmp_path / "m1.m4a", length)
    event("meeting_start", T0)
    event("meeting_end", T0 + timedelta(seconds=length))
    with TestClient(app_()) as client:
        assert upload(client, mic_only, T0, "mac-meeting", "mac-meeting:m1:0", meeting_id="m1",
                      app="Microsoft Teams", bundle="com.microsoft.teams2", mic="AirPods Pro",
                      channels=["mic", "app"], end_reason="meeting_end", close_episode=True).status_code == 201
        # The phone heard the same meeting, then a chat in the corridor three minutes later.
        assert upload(client, audio_dir / "b.m4a", T0, phone, f"{phone}:b").status_code == 201
        assert upload(client, audio_dir / "c.m4a", T0 + timedelta(minutes=3), phone, f"{phone}:c",
                      close_episode=True).status_code == 201
    pipeline.drain()

    phone_b = rows("SELECT stage, metadata FROM chunks WHERE client_id = %s", f"{phone}:b")[0]
    assert phone_b["stage"] == "no_speech" and phone_b["metadata"]["meeting_cut"]
    eps = rows("SELECT e.meta, e.export_path, array_agg(c.client_id ORDER BY c.client_id) AS chunks FROM episodes e "
               "JOIN chunks c ON c.episode_id = e.id GROUP BY e.id ORDER BY e.meta ? 'meeting'")
    assert [e["chunks"] for e in eps] == [[f"{phone}:c"], ["mac-meeting:m1:0"]]
    meet = eps[1]
    assert meet["meta"]["meeting"]["id"] == "m1" and meet["meta"]["meeting"]["mic_label"] == "SPEAKER_01"
    md = (env.data / meet["export_path"]).read_text(encoding="utf-8")
    assert md.startswith("# Họp online qua Microsoft Teams 12/10/2026 09:00")
    assert "] Kim:**" in md and "AirPods Pro" in md


def test_voice_only_on_the_app_channel_is_not_me(env, audio_dir, tmp_path):
    with connect() as conn:
        conn.execute("INSERT INTO speakers (name, status, is_me) VALUES ('Kim', 'named', true)")
    length = probe(audio_dir / "b.m4a")[0] + 2
    app_only = stereo(None, audio_dir / "b.m4a", tmp_path / "m2.m4a", length)
    with TestClient(app_()) as client:
        upload(client, app_only, T0, "mac-meeting", "mac-meeting:m2:0", meeting_id="m2", app="Zoom",
               close_episode=True)
    pipeline.drain()
    ep = rows("SELECT id, meta FROM episodes")[0]
    assert ep["meta"]["meeting"]["mic_label"] is None
    assert rows("SELECT speaker_id FROM episode_speakers WHERE episode_id = %s", ep["id"])[0]["speaker_id"] is None


def test_phone_waits_for_a_running_meeting_and_keeps_audio_if_the_mac_sent_nothing(env, audio_dir):
    now = datetime.now(timezone.utc)
    event("meeting_start", now - timedelta(minutes=30), "m3")
    with TestClient(app_()) as client:
        upload(client, audio_dir / "a.m4a", now - timedelta(minutes=20), "iphone", "iphone:a", close_episode=True)
    pipeline.drain()
    assert rows("SELECT stage FROM chunks")[0]["stage"] == "vad"      # held while the meeting runs
    assert not rows("SELECT 1 FROM episodes")

    event("meeting_end", now - timedelta(minutes=15), "m3")
    pipeline.drain()
    chunk = rows("SELECT stage, metadata FROM chunks")[0]
    assert chunk["stage"] == "grouped" and "meeting_cut" not in chunk["metadata"]


def app_():
    from lifelog.api import app

    return app
