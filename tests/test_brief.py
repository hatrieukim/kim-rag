"""Product 9: conversations in the day journal, week summaries, and the summary, people and timeline tools."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from lifelog import diarize, graph, journal, pipeline, summarize, voices
from lifelog.db import connect
from lifelog.diarize import DiarizeResult
from lifelog.ingest import ingest_file
from lifelog.journal import compact, day_summary, week_summary, weeks_due
from lifelog.people import profile
from lifelog.timeline import timeline

TUAN = [0.0, 1.0, 0.0, 0.0]
OTHER = [0.0, 0.0, 0.0, 1.0]
MONDAY = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)      # 09:00 in Vietnam, Monday 05/10
TUESDAY = MONDAY + timedelta(days=1)


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


class TwoVoices:
    name = "fake-diarizer"

    def __init__(self, first, second):
        self.first, self.second = first, second

    def run(self, samples, speech):
        end = len(samples) / 16_000
        mid = end / 2
        return DiarizeResult(turns=[(0.2, mid, "A"), (mid, end - 0.2, "B")],
                             embeddings={"A": self.first, "B": self.second}, model="fake-v1")


@pytest.fixture()
def talks(env, audio_dir, monkeypatch):
    """Two recorded conversations with the same colleague, on Monday and Tuesday of one week."""
    monkeypatch.setattr(voices, "MIN_VOICE_S", 0.5)
    monkeypatch.setattr(summarize, "suggest_speakers", lambda transcript, labels, context="": {})
    monkeypatch.setattr(summarize, "SHORT_CONVERSATION", 0)    # test clips are short; summarize them anyway
    monkeypatch.setattr(pipeline, "get_diarizer", lambda _: TwoVoices(TUAN, OTHER))
    env.eps = []
    for name, when in (("a", MONDAY), ("b", TUESDAY)):
        ingest_file(audio_dir / f"{name}.m4a", started_at=when, close_episode=True)
        pipeline.drain()
        env.eps.append(rows("SELECT * FROM episodes WHERE kind = 'voice' ORDER BY created_at DESC LIMIT 1")[0])
    return env


def journal_text(env, day: date) -> str:
    return (env.data / rows("SELECT path FROM journals WHERE day = %s", day)[0]["path"]).read_text(encoding="utf-8")


def test_conversations_make_a_day_journal_and_the_week_gets_a_summary(talks):
    env = talks
    assert [r["day"] for r in rows("SELECT day FROM journals ORDER BY day")] == [date(2026, 10, 5), date(2026, 10, 6)]
    text = journal_text(env, date(2026, 10, 5))
    ep = env.eps[0]
    assert "1 cuộc hội thoại hoặc cuộc họp đã ghi âm." in text and "## Hội thoại" in text
    assert f"· ghi âm · SPEAKER_01, SPEAKER_02 · episode `{str(ep['id'])[:8]}`\n\nTóm tắt ghi âm." in text
    assert rows("SELECT summary FROM episodes WHERE id = %s", ep["id"])[0]["summary"] == "Tóm tắt ghi âm."
    day_call = [s for s in env.summaries if s[0] == "day" and s[1] == "05/10/2026"][-1]
    assert "Tóm tắt ghi âm." in day_call[3]                     # the overview hears about the conversation

    week = rows("SELECT * FROM week_journals")
    assert [w["week_start"] for w in week] == [date(2026, 10, 5)]
    text = (env.data / week[0]["path"]).read_text(encoding="utf-8")
    assert text.startswith("# Tuần 05/10–11/10/2026\n\n2 ngày có nhật ký.\n\n**Việc chính:** tuần 05/10–11/10/2026")
    assert "### Thứ hai 05/10\n\nTổng quan ngày 05/10/2026." in text and "### Thứ ba 06/10" in text
    assert weeks_due() == []

    # Naming the voice: the conversation summary is written again, then the day and the week.
    calls = len(env.summaries)
    speaker = rows("SELECT speaker_id FROM episode_speakers WHERE episode_id = %s AND label = 'SPEAKER_01'",
                   ep["id"])[0]["speaker_id"]
    voices.name_speaker(speaker, "Tuấn")
    pipeline.drain()
    assert "· ghi âm · SPEAKER_02, Tuấn · episode" in journal_text(env, date(2026, 10, 5))
    kinds = [s[0] for s in env.summaries[calls:]]
    assert kinds.count("talk") == 2 and kinds.count("day") == 2 and kinds.count("week") == 1


def test_summary_tool(talks, monkeypatch):
    env = talks
    assert day_summary(date(2026, 10, 5)) == compact(journal_text(env, date(2026, 10, 5)))
    assert "không có dữ liệu" in day_summary(date(2026, 10, 7))
    with pytest.raises(ValueError):
        day_summary(date.today() + timedelta(days=2))

    # A journal older than its data is written again on the spot.
    with connect() as conn:
        conn.execute("UPDATE journals SET signature = 'old' WHERE day = '2026-10-05'")
    calls = len(env.summaries)
    day_summary(date(2026, 10, 5))
    assert [s[0] for s in env.summaries[calls:]] == ["day"]

    assert week_summary(date(2026, 10, 8)).startswith("# Tuần 05/10–11/10/2026")
    assert week_summary(date(2026, 9, 2)) == "Tuần 31/08–06/09/2026 chưa có nhật ký ngày nào."

    # Today is written from the data so far, and not saved.
    monkeypatch.setattr(journal, "datetime", type("Clock", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 10, 6, 20, 0, tzinfo=tz)),
        "combine": datetime.combine}))
    text = day_summary(date(2026, 10, 6))
    assert text.startswith("Lưu ý: Ngày chưa hết: bản này viết từ dữ liệu tới 20:00") and "## Hội thoại" in text


def test_compact_keeps_session_headings_and_drops_their_bodies():
    text = ("# Nhật ký làm việc 05/10/2026\n\nTổng quan.\n\n## Lịch\n\n- 09:00–10:00 Họp\n\n"
            "## Hội thoại\n\n### 09:00–09:05 · ghi âm · Kim · episode `ab`\n\nTóm tắt hội thoại.\n\n"
            "## demo_app\n\n### 10:00–11:00 · Sửa bảng · episode `cd`\n\n**Đã làm:** rất dài.\n\nFile đã sửa: `a.py`\n")
    out = compact(text)
    assert "Tóm tắt hội thoại." in out and "- 09:00–10:00 Họp" in out
    assert "### 10:00–11:00 · Sửa bảng · episode `cd`" in out and "rất dài" not in out and "a.py" not in out
    assert journal.overview(text) == "Tổng quan."


def test_people(talks):
    env = talks
    for ep in env.eps:
        speaker = rows("SELECT speaker_id FROM episode_speakers WHERE episode_id = %s AND label = 'SPEAKER_01'",
                       ep["id"])[0]["speaker_id"]
        voices.name_speaker(speaker, "Tuấn")
    pipeline.drain()
    with connect() as conn:
        conn.execute(
            "INSERT INTO calendar_events (key, title, starts_at, ends_at, attendees) VALUES (%s, %s, %s, %s, %s)",
            ("k1", "Họp kế hoạch", MONDAY, MONDAY + timedelta(hours=1),
             Jsonb([{"name": "Tuấn", "email": "tuan@example.com"}])))

    text = profile("tuấn")
    assert text.startswith("# Tuấn\n\nGiọng đã được nhận ra trong 2 bản ghi")
    assert "## Các lần nói chuyện gần nhất" in text and text.count("Tóm tắt ghi âm.") == 2
    assert "cùng: SPEAKER_02" in text and f"episode {str(env.eps[1]['id'])[:8]}" in text
    assert "## Lịch có mời người này\n- 05/10/2026 09:00–10:00 · Họp kế hoạch" in text
    assert "## Điều đã biết\nTuấn là đồng nghiệp." in text and "## Fact: 2, mới nhất trước" in text

    with connect() as conn:
        conn.execute("INSERT INTO speakers (name) VALUES ('Tuấn Anh'), ('Minh Tuấn')")
    assert profile("Tuấn").startswith("# Tuấn\n")                # an exact name wins
    assert profile("anh").startswith("# Tuấn Anh")              # the only one with "anh" as a whole word
    with connect() as conn:
        conn.execute("DELETE FROM speakers WHERE name = 'Tuấn'")
    assert profile("Tuấn").startswith('Có 2 người khớp với "Tuấn": Minh Tuấn, Tuấn Anh.')

    unknown = profile("Hùng")
    assert unknown.startswith('Không tìm thấy ai tên "Hùng"') and "Minh Tuấn" in unknown
    with pytest.raises(ValueError):
        profile("  ")


def test_timeline_orders_facts_and_passages_by_time_and_drops_far_facts(talks, monkeypatch):
    env = talks
    parts = {r["episode_id"]: str(r["uuid"]) for r in rows("SELECT uuid, episode_id FROM graph_parts")}
    monday, tuesday = (parts[ep["id"]] for ep in env.eps)
    monkeypatch.setattr(graph, "rank_facts", lambda query, limit=50: [
        {"fact": "Kế hoạch đổi sang làm ghi âm trước", "valid_at": None, "invalid_at": None,
         "parts": [tuesday], "score": 0.80},
        {"fact": "Kế hoạch quý bốn là làm tìm kiếm trước", "valid_at": MONDAY, "invalid_at": TUESDAY,
         "parts": [monday], "score": 0.75},
        {"fact": "Chuyện không liên quan", "valid_at": MONDAY, "invalid_at": None, "parts": [monday], "score": 0.6},
    ])
    text = timeline("kế hoạch")
    assert text.startswith('Dòng thời gian "kế hoạch": 2 fact, ')
    assert "không liên quan" not in text
    first = text.index("Kế hoạch quý bốn")
    assert text.index("## 05/10/2026") < first < text.index("## 06/10/2026") < text.index("Kế hoạch đổi sang")
    assert "ĐÃ HẾT ĐÚNG từ 06/10/2026" in text and "- Nguyên văn " in text and "  > SPEAKER_01:" in text

    later = timeline("kế hoạch", since="2026-10-06")
    assert "Kế hoạch quý bốn" not in later and "## 05/10/2026" not in later
    with pytest.raises(ValueError):
        timeline(" ")
