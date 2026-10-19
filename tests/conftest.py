"""Test chạy trên database riêng `lifelog_test` trong cùng Postgres, không đụng dữ liệu thật."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

import psycopg
import pytest

BASE_URL = os.environ.get("LIFELOG_TEST_BASE_URL", "postgresql://lifelog:lifelog@127.0.0.1:54329")
TEST_DB = "lifelog_test"


@pytest.fixture(scope="session", autouse=True)
def _database():
    try:
        with psycopg.connect(f"{BASE_URL}/lifelog", autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
                conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError as exc:
        pytest.exit(f"Cần Postgres để chạy test (docker compose up -d): {exc}", returncode=2)
    yield


@pytest.fixture(scope="session")
def audio_dir(tmp_path_factory) -> Path:
    """Tạo vài file audio tiếng Việt bằng giọng đọc có sẵn của macOS."""
    if not shutil.which("say"):
        pytest.skip("cần lệnh `say` của macOS để tạo audio thử")
    d = tmp_path_factory.mktemp("audio")
    lines = {
        "a": "Xin chào, hôm nay chúng ta họp về kế hoạch quý bốn. Tôi muốn nghe ý kiến của mọi người.",
        "b": "Tôi nghĩ nên ưu tiên phần ghi âm trước, sau đó mới làm phần tìm kiếm.",
        "c": "Đồng ý. Tuần sau tôi sẽ gửi bản kế hoạch chi tiết cho cả nhóm.",
    }
    for name, text in lines.items():
        subprocess.run(["say", "-v", "Linh", "-o", str(d / f"{name}.aiff"), text], check=True)
        # Thêm một giây im lặng ở hai đầu, xuất m4a như file ghi âm thật.
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(d / f"{name}.aiff"),
             "-af", "adelay=1000:all=1,apad=pad_dur=1", "-ac", "1", "-ar", "16000", str(d / f"{name}.m4a")],
            check=True,
        )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "6", str(d / "silence.m4a")],
        check=True,
    )
    (d / "not_audio.m4a").write_text("đây không phải audio")
    return d


class FakeBackend:
    """Dịch vụ chuyển chữ giả: trả về chữ cố định và đếm số lần được gọi."""

    def __init__(self, name: str = "fake") -> None:
        from lifelog.asr import AsrText

        self._AsrText = AsrText
        self.name = name
        self.model = f"{name}-model"
        self.calls = 0
        self.fail_first = 0
        self.unavailable: str | None = None
        self._lock = threading.Lock()

    def check(self) -> None:
        from lifelog.asr import AsrUnavailable

        if self.unavailable:
            raise AsrUnavailable(self.unavailable)

    def transcribe(self, wav: bytes):
        with self._lock:
            self.calls += 1
            n = self.calls
            if self.fail_first > 0:
                self.fail_first -= 1
                raise RuntimeError("lỗi mạng giả")
        assert wav[:4] == b"RIFF"
        return self._AsrText(text=f"[{self.name}] lượt {n}", confidence=0.9)


class FakeGraph:
    """Kho graph giả: ghi lại những phần được nạp và bị gỡ, không gọi Graphiti hay Google."""

    def __init__(self) -> None:
        self.added: list[dict] = []     # mỗi phần: {uuid, n, name, text}
        self.removed: list[str] = []
        self.asked: list[tuple[str, int]] = []
        self.unavailable: str | None = None
        self.fail_after: int | None = None  # ném lỗi sau khi nạp xong ngần này phần trong một lần gọi

    def check(self) -> None:
        from lifelog.graph import GraphUnavailable

        if self.unavailable:
            raise GraphUnavailable(self.unavailable)

    def add_parts(self, parts, *, name, description, on_done, prose=False, instructions=None) -> None:
        from lifelog.graph import PartResult, Usage

        for count, (n, window) in enumerate(parts):
            if self.fail_after is not None and count >= self.fail_after:
                self.fail_after = None
                raise RuntimeError("lỗi Graphiti giả")
            part_uuid = str(uuid.uuid4())
            self.added.append({"uuid": part_uuid, "n": n, "name": name, "text": window.text, "prose": prose,
                               "description": description, "instructions": instructions})
            on_done(n, window, PartResult(uuid=part_uuid, nodes=2, edges=1, seconds=0.1, usage=Usage(3, 100, 50)))

    def remove_parts(self, uuids) -> None:
        self.removed.extend(uuids)

    def search_facts(self, query: str, limit: int = 8) -> list[dict]:
        """Mỗi phần đang nằm trong graph cho một fact giả, phần nạp sau đứng trước."""
        self.asked.append((query, limit))
        return [
            {"fact": f"fact từ {a['name']}", "valid_at": None, "invalid_at": None, "parts": [a["uuid"]]}
            for a in reversed(self.live)
        ][:limit]

    def rank_facts(self, query: str, limit: int = 50) -> list[dict]:
        """Same facts as `search_facts`, each a little further from the query than the one before."""
        return [{**f, "score": 0.9 - 0.04 * i} for i, f in enumerate(self.search_facts(query, limit))]

    def person(self, name: str, limit: int = 15) -> dict:
        """A person is known when some part in the graph mentions them; each such part gives one fact."""
        parts = [a for a in reversed(self.live) if name.lower() in a["text"].lower()]
        if not parts:
            return {"nodes": [], "facts": []}
        return {"nodes": [{"name": name, "summary": f"{name} là đồng nghiệp."}],
                "facts": [{"fact": f"{name} nói trong {a['name']}", "valid_at": None, "invalid_at": None,
                           "parts": [a["uuid"]]} for a in parts][:limit]}

    @property
    def live(self) -> list[dict]:
        """Những phần đang nằm trong graph."""
        return [a for a in self.added if a["uuid"] not in self.removed]


def fake_embed(texts: list[str]) -> list[list[float]]:
    """Vector giả 1.536 chiều, cùng chữ thì cùng vector."""
    out = []
    for text in texts:
        seed = hashlib.sha256(text.encode()).digest()
        out.append([seed[i % len(seed)] / 255.0 for i in range(1536)])
    return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Môi trường sạch cho mỗi test: database trống, thư mục dữ liệu tạm, dịch vụ giả."""
    monkeypatch.chdir(tmp_path)  # không đọc .env thật của dự án
    monkeypatch.setenv("DATABASE_URL", f"{BASE_URL}/{TEST_DB}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DIARIZER", "none")
    monkeypatch.setenv("ASR_BACKEND", "fake")
    monkeypatch.setenv("ASR_CONCURRENCY", "1")
    monkeypatch.setenv("API_TOKEN", "")
    monkeypatch.setenv("TIMEZONE", "Asia/Ho_Chi_Minh")
    monkeypatch.setenv("GRAPH", "graphiti")
    monkeypatch.setenv("EMBED_MODEL", "fake-embed")
    monkeypatch.setenv("GRAPH_LLM_MODEL", "fake-llm")

    from lifelog import asr, embed, graph, pipeline, summarize
    from lifelog.config import get_settings
    from lifelog.db import connect, init_db

    get_settings.cache_clear()
    init_db()
    with connect() as conn:
        conn.execute(
            "TRUNCATE chunks, episodes, segments, segment_texts, episode_speakers, speakers, windows, graph_parts, "
            "events, event_files, journals, week_journals, code_sessions, mac_spans, calendar_events, voiceprints CASCADE"
        )
    fake, fake2 = FakeBackend("fake"), FakeBackend("fake2")
    asr.register("fake", lambda: fake)
    asr.register("fake2", lambda: fake2)
    fake_graph = FakeGraph()
    monkeypatch.setattr(embed, "check", lambda: None)
    monkeypatch.setattr(embed, "embed_texts", fake_embed)
    for attr in ("check", "add_parts", "remove_parts", "search_facts", "rank_facts", "person"):
        monkeypatch.setattr(graph, attr, getattr(fake_graph, attr))
    # LLM viết tóm tắt giả: ghi lại đã được gọi với gì, trả về chữ cố định.
    summaries: list[tuple[str, str]] = []
    monkeypatch.setattr(summarize, "check", lambda: None)
    monkeypatch.setattr(summarize, "summarize_episode",
                        lambda transcript, project: summaries.append((project, transcript)) or f"**Đã làm:** việc ở {project}.")
    monkeypatch.setattr(summarize, "summarize_day",
                        lambda day, sessions, mac="", talks="": summaries.append(("day", day, mac, talks))
                        or f"Tổng quan ngày {day}.")
    monkeypatch.setattr(summarize, "summarize_conversation",
                        lambda transcript, what="hội thoại": summaries.append(("talk", what, transcript))
                        or f"Tóm tắt {what}.")
    monkeypatch.setattr(summarize, "summarize_week",
                        lambda week, days: summaries.append(("week", week, days)) or f"**Việc chính:** tuần {week}.")
    monkeypatch.setattr(pipeline, "_last_code_scan", 0.0)
    monkeypatch.setattr(pipeline, "_last_git_scan", 0.0)

    class Env:
        pass

    e = Env()
    e.fake, e.fake2, e.graph, e.data, e.summaries = fake, fake2, fake_graph, tmp_path / "data", summaries
    yield e
    get_settings.cache_clear()
