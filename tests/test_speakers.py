"""Product 6: voices linked to people across recordings, named once, relabelled everywhere."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from lifelog import diarize, pipeline, summarize, voices
from lifelog.db import connect
from lifelog.diarize import DiarizeResult
from lifelog.ingest import ingest_file
from lifelog.search import search

KIM = [1.0, 0.0, 0.0, 0.0]
TUAN = [0.0, 1.0, 0.0, 0.0]
CALLER = [0.0, 0.0, 1.0, 0.0]
OTHER = [0.0, 0.0, 0.0, 1.0]


def near(vec, by=0.08):
    """The same voice recorded another time: close, not identical."""
    out = list(vec)
    out[(out.index(max(out)) + 1) % len(out)] += by
    return out


class TwoVoices:
    """Fake diarizer: first half of the audio is one voice, second half another."""

    name = "fake-diarizer"

    def __init__(self, first, second=None):
        self.first, self.second = first, second

    def run(self, samples, speech):
        end = len(samples) / 16_000
        if self.second is None:
            return DiarizeResult(turns=[(0.2, end - 0.2, "A")], embeddings={"A": self.first}, model="fake-v1")
        mid = end / 2
        return DiarizeResult(turns=[(0.2, mid, "A"), (mid, end - 0.2, "B")],
                             embeddings={"A": self.first, "B": self.second}, model="fake-v1")


@pytest.fixture()
def voice_env(env, monkeypatch):
    monkeypatch.setattr(voices, "MIN_VOICE_S", 0.5)   # test clips are a few seconds long
    monkeypatch.setattr(voices, "MIN_ENROLL_S", 2.0)
    env.suggested = {}
    env.llm_calls = []

    def fake_suggest(transcript, labels, context=""):
        env.llm_calls.append((transcript, labels, context))
        return {label: env.suggested.get(label, {}) for label in labels}

    monkeypatch.setattr(summarize, "suggest_speakers", fake_suggest)
    return env


def record(monkeypatch, audio_dir, name, when, first, second=None):
    monkeypatch.setattr(pipeline, "get_diarizer", lambda _: TwoVoices(first, second))
    ingest_file(audio_dir / f"{name}.m4a", started_at=when, close_episode=True)
    pipeline.drain()
    with connect() as conn:
        return conn.execute("SELECT * FROM episodes WHERE kind = 'voice' ORDER BY created_at DESC LIMIT 1").fetchone()


def copy_of(audio_dir, src, name):
    """Another recording with the same speech: different bytes, so it is not taken for a duplicate."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(audio_dir / f"{src}.m4a"), "-c", "copy",
                    "-metadata", f"comment={name}", str(audio_dir / f"{name}.m4a")], check=True)


def links(episode_id):
    with connect() as conn:
        return {r["label"]: r for r in conn.execute(
            "SELECT es.label, es.speaker_id, es.assigned_by, es.role, es.suggested_name, p.name, p.status "
            "FROM episode_speakers es LEFT JOIN speakers p ON p.id = es.speaker_id WHERE es.episode_id = %s",
            (episode_id,))}


def markdown(env, episode_id):
    with connect() as conn:
        path = conn.execute("SELECT export_path FROM episodes WHERE id = %s", (episode_id,)).fetchone()["export_path"]
    return (env.data / path).read_text(encoding="utf-8")


DAY1 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)


def test_recurring_stranger_is_asked_then_named_everywhere(voice_env, audio_dir, monkeypatch):
    env = voice_env
    env.suggested = {"SPEAKER_01": {"name": "anh Tuấn", "role": "đồng nghiệp"},
                     "SPEAKER_02": {"name": None, "role": "nhân viên tổng đài"}}
    ep1 = record(monkeypatch, audio_dir, "a", DAY1, TUAN, CALLER)
    ep2 = record(monkeypatch, audio_dir, "b", DAY2, near(TUAN), OTHER)

    l1, l2 = links(ep1["id"]), links(ep2["id"])
    assert l1["SPEAKER_01"]["speaker_id"] == l2["SPEAKER_01"]["speaker_id"]          # same stranger on two days
    assert l1["SPEAKER_02"]["speaker_id"] != l2["SPEAKER_02"]["speaker_id"]
    assert {r["status"] for r in [*l1.values(), *l2.values()]} == {"unknown"}

    # Only the voice heard on two days is worth asking about; the one-off caller is not.
    waiting = voices.queue()
    assert [w["id"] for w in waiting] == [l1["SPEAKER_01"]["speaker_id"]]
    assert waiting[0]["days"] == 2 and waiting[0]["names"] == ["anh Tuấn"]

    # Unnamed voices show their role label, with the recording id for Graphiti.
    assert "nhân viên tổng đài:" in markdown(env, ep1["id"])
    assert any(f"nhân viên tổng đài (bản ghi {str(ep1['id'])[:8]}):" in a["text"] for a in env.graph.live)

    calls = len(env.llm_calls)
    res = voices.name_speaker(waiting[0]["id"], "Tuấn")
    assert res == {"id": str(waiting[0]["id"]), "name": "Tuấn", "merged": False, "episodes": 2}
    with connect() as conn:
        stages = {r["stage"] for r in conn.execute("SELECT stage FROM episodes WHERE kind = 'voice'")}
    assert stages == {"transcribed"}                        # both go back to be indexed and ingested again
    assert "] Tuấn:**" in markdown(env, ep1["id"])          # markdown is rewritten right away

    pipeline.drain()
    assert len(env.llm_calls) == calls                      # suggestions are not asked again
    live = "\n".join(a["text"] for a in env.graph.live)
    assert "Tuấn:" in live and "Người nói 1" not in live
    assert env.graph.removed                                # old parts with SPEAKER labels were removed
    assert voices.queue() == []
    found = search("kế hoạch", speakers=["Tuấn"])
    assert found.passages and all("Tuấn" in p.speakers for p in found.passages)
    with connect() as conn:
        sample = conn.execute("SELECT sample_path FROM speakers WHERE name = 'Tuấn'").fetchone()["sample_path"]
    assert (env.data / sample).stat().st_size > 1000        # audio sample kept for the named person


def test_enrolled_voice_is_matched_in_new_and_old_recordings(voice_env, audio_dir, monkeypatch):
    env = voice_env
    old = record(monkeypatch, audio_dir, "a", DAY1, near(KIM), OTHER)
    assert links(old["id"])["SPEAKER_01"]["status"] == "unknown"

    monkeypatch.setattr(diarize, "get_diarizer", lambda _: TwoVoices(KIM))
    got = voices.enroll(audio_dir / "c.m4a", me=True)
    assert (got.name, got.episodes) == ("Kim", 1)          # OWNER_NAME by default; the old recording relabelled
    with connect() as conn:
        assert conn.execute("SELECT name FROM speakers WHERE is_me").fetchone()["name"] == "Kim"
        # The stranger that turned out to be Kim is gone.
        assert conn.execute("SELECT count(*) AS n FROM speakers WHERE status = 'unknown'").fetchone()["n"] == 1
    pipeline.drain()
    assert "Kim:" in markdown(env, old["id"])

    new = record(monkeypatch, audio_dir, "b", DAY2, near(KIM, 0.15), near(KIM, 0.2))
    l = links(new["id"])
    named = [r for r in l.values() if r["name"] == "Kim"]
    assert len(named) == 1 and named[0]["assigned_by"] == "auto"   # one recording, one Kim
    cov = voices.coverage(days=10_000)
    assert 0 < cov["auto"] <= cov["named"] < cov["total"]


def test_wrong_link_can_be_detached_and_stays_detached(voice_env, audio_dir, monkeypatch):
    env = voice_env
    monkeypatch.setattr(diarize, "get_diarizer", lambda _: TwoVoices(KIM))
    voices.enroll(audio_dir / "c.m4a", name="Kim")
    ep = record(monkeypatch, audio_dir, "a", DAY1, near(KIM), OTHER)
    assert links(ep["id"])["SPEAKER_01"]["name"] == "Kim"

    assert voices.detach(ep["id"], "SPEAKER_01") == {"others": 0}
    pipeline.drain()                                        # re-index runs matching again
    row = links(ep["id"])["SPEAKER_01"]
    assert row["name"] is None and row["status"] == "unknown"   # matched anew: a stranger, not Kim
    assert "Kim:" not in markdown(env, ep["id"])


def test_not_this_person_relearns_similar_voices(voice_env, audio_dir, monkeypatch):
    monkeypatch.setattr(diarize, "get_diarizer", lambda _: TwoVoices(TUAN))
    voices.enroll(audio_dir / "c.m4a", name="Tuấn")
    lookalike = [1.0, 1.0, 0.0, 0.0]                        # another man, close enough to pass for Tuấn
    copy_of(audio_dir, "a", "d")
    copy_of(audio_dir, "b", "e")
    real = record(monkeypatch, audio_dir, "a", DAY1, near(TUAN), CALLER)
    wrong = record(monkeypatch, audio_dir, "b", DAY2, lookalike, CALLER)
    again = record(monkeypatch, audio_dir, "d", DAY2 + timedelta(hours=3), near(lookalike, 0.05), OTHER)
    assert {links(e["id"])["SPEAKER_01"]["name"] for e in (real, wrong, again)} == {"Tuấn"}

    assert voices.detach(wrong["id"], "SPEAKER_01") == {"others": 1}
    l_wrong, l_again = links(wrong["id"])["SPEAKER_01"], links(again["id"])["SPEAKER_01"]
    assert l_wrong["status"] == l_again["status"] == "unknown"
    assert l_wrong["speaker_id"] == l_again["speaker_id"]  # the same other man, now a stranger to ask about
    assert links(real["id"])["SPEAKER_01"]["name"] == "Tuấn"

    pipeline.drain()
    later = record(monkeypatch, audio_dir, "e", DAY2 + timedelta(days=1), near(lookalike, 0.1), near(TUAN, 0.1))
    l_later = links(later["id"])
    assert l_later["SPEAKER_01"]["speaker_id"] == l_wrong["speaker_id"]
    assert l_later["SPEAKER_02"]["name"] == "Tuấn"


def test_ignored_and_old_strangers_are_forgotten(voice_env, audio_dir, monkeypatch):
    ep = record(monkeypatch, audio_dir, "a", DAY1, TUAN, CALLER)
    tuan = links(ep["id"])["SPEAKER_01"]["speaker_id"]
    voices.ignore_speaker(tuan)
    with pytest.raises(voices.VoiceError):
        voices.ignore_speaker("00000000-0000-0000-0000-000000000000")
    with connect() as conn:
        assert conn.execute("SELECT status FROM speakers WHERE id = %s", (tuan,)).fetchone()["status"] == "ignored"

    assert voices.forget_strangers(now=DAY1 + timedelta(days=30)) == 0
    assert voices.forget_strangers(now=DAY1 + timedelta(days=91)) == 2
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM voiceprints").fetchone()["n"] == 0
    assert links(ep["id"])["SPEAKER_01"]["speaker_id"] is None


def test_not_important_voice_comes_back_when_it_keeps_turning_up(voice_env, audio_dir, monkeypatch):
    copy_of(audio_dir, "a", "d")
    copy_of(audio_dir, "b", "e")
    ep = record(monkeypatch, audio_dir, "a", DAY1, TUAN, CALLER)
    record(monkeypatch, audio_dir, "b", DAY2, near(TUAN), OTHER)
    tuan = links(ep["id"])["SPEAKER_01"]["speaker_id"]
    assert [w["id"] for w in voices.queue()] == [tuan]

    voices.ignore_speaker(tuan)
    with connect() as conn:   # as if marked on the evening of DAY2
        conn.execute("UPDATE speakers SET ignored_at = %s WHERE id = %s", (DAY2 + timedelta(hours=10), tuan))
    assert voices.queue() == []

    record(monkeypatch, audio_dir, "d", DAY2 + timedelta(days=1), near(TUAN, 0.05))
    assert voices.queue() == []                             # heard on one day since: not yet
    record(monkeypatch, audio_dir, "e", DAY2 + timedelta(days=2), near(TUAN, 0.1))
    waiting = voices.queue()
    assert [w["id"] for w in waiting] == [tuan]
    assert waiting[0]["days"] == 2 and waiting[0]["episodes"] == 2 and waiting[0]["ignored_at"]

    voices.ignore_speaker(tuan)                             # not important again: quiet until it turns up again
    assert voices.queue() == []


def test_naming_with_an_existing_name_merges(voice_env, audio_dir, monkeypatch):
    monkeypatch.setattr(diarize, "get_diarizer", lambda _: TwoVoices(TUAN))
    voices.enroll(audio_dir / "c.m4a", name="Tuấn")
    ep = record(monkeypatch, audio_dir, "a", DAY1, OTHER, CALLER)   # Tuấn on a bad phone line: no match
    stranger = links(ep["id"])["SPEAKER_01"]["speaker_id"]
    res = voices.name_speaker(stranger, "tuấn")
    assert res["merged"]
    with connect() as conn:
        rows = conn.execute("SELECT name, (SELECT count(*) FROM voiceprints v WHERE v.speaker_id = p.id) AS prints "
                            "FROM speakers p WHERE status = 'named'").fetchall()
    assert [(r["name"], r["prints"]) for r in rows] == [("Tuấn", 2)]   # enrolment + the new voice


def test_rescan_matches_recordings_indexed_before(voice_env, audio_dir, monkeypatch):
    ep = record(monkeypatch, audio_dir, "a", DAY1, TUAN, CALLER)
    with connect() as conn:   # as if this episode was indexed before product 6
        conn.execute("DELETE FROM speakers")
        conn.execute("UPDATE episode_speakers SET llm_checked = false, role = NULL, assigned_by = NULL")
    voice_env.suggested = {"SPEAKER_02": {"role": "nhân viên tổng đài"}}
    assert voices.rescan() == {"episodes": 1, "rebuilt": 1}
    assert links(ep["id"])["SPEAKER_02"]["role"] == "nhân viên tổng đài"
    pipeline.drain()
    assert voices.rescan() == {"episodes": 1, "rebuilt": 0}


def test_page_and_api(voice_env, audio_dir, monkeypatch):
    from lifelog.api import app

    record(monkeypatch, audio_dir, "a", DAY1, TUAN, CALLER)
    record(monkeypatch, audio_dir, "b", DAY2, near(TUAN), OTHER)
    with TestClient(app) as client:
        assert "<title>kim-rag voices</title>" in client.get("/speakers").text
        data = client.get("/speakers/data").json()
        assert len(data["queue"]) == 1 and data["people"] == []
        sid = data["queue"][0]["id"]
        clip = client.get(f"/speakers/{sid}/clip")
        assert clip.status_code == 200 and clip.content[:4] == b"RIFF"
        assert client.post(f"/speakers/{sid}/name", json={"name": " "}).status_code == 400
        assert client.post(f"/speakers/{sid}/name", json={"name": "Tuấn"}).json()["name"] == "Tuấn"
        people = client.get("/speakers/data").json()["people"]
        assert people[0]["name"] == "Tuấn" and people[0]["episodes"] == 2
        voices_list = client.get(f"/speakers/{sid}/voices").json()
        assert len(voices_list) == 2
        v = voices_list[0]
        assert client.post(f"/voices/{v['episode_id']}/{v['label']}/detach").json() == {"ok": True, "others": 0}
        assert client.post("/speakers/00000000-0000-0000-0000-000000000000/ignore").status_code == 404
