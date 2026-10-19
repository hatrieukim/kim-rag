"""Sản phẩm 7: trang đã đọc trên Chrome thành bài tìm lại được và mục "Đã đọc" trong nhật ký."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lifelog.db import connect
from lifelog.journal import days_due, write_journal
from lifelog.pipeline import drain
from lifelog.reading import chunks
from lifelog.search import search

T0 = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)   # 10:00 giờ Việt Nam
URL = "https://blog.getzep.com/graphiti-temporal"
ARTICLE = "\n\n".join([
    "Graphiti là thư viện dựng đồ thị tri thức có thời gian, mỗi fact mang cửa sổ hiệu lực riêng.",
    "Khi một điều cũ không còn đúng, Graphiti không xóa mà đánh dấu fact đó hết hiệu lực từ một thời điểm.",
    "Truy vấn kết hợp tìm theo nghĩa, BM25 và duyệt đồ thị, không cần gọi LLM lúc hỏi. " * 4,
])


def ev(uid: str, type_: str, minutes: float, **payload) -> dict:
    return {"uid": uid, "source": "chrome", "type": type_, "ts": (T0 + timedelta(minutes=minutes)).isoformat(),
            "payload": payload}


def test_chia_bai_thanh_khuc():
    parts = chunks("a" * 600 + "\n\n" + "b" * 300 + "\n\n" + "c" * 1500, limit=1000)
    assert [len(p) for p in parts] == [902, 1000, 500]   # hai đoạn đầu gộp, đoạn dài bị cắt


def test_bai_doc_thanh_episode_tim_duoc_va_vao_nhat_ky(env):
    from lifelog.api import app

    batch = [
        ev("chrome:1", "visit", 0, url=URL, title="Graphiti: đồ thị có thời gian", domain="blog.getzep.com", seconds=60),
        ev("chrome:read:abc", "read", 0.5, url=URL, title="Graphiti: đồ thị có thời gian", domain="blog.getzep.com",
           site="Zep", text=ARTICLE, chars=len(ARTICLE)),
        ev("chrome:2", "visit", 1, url=URL, title="Graphiti: đồ thị có thời gian", domain="blog.getzep.com", seconds=45),
        ev("chrome:3", "selection", 1.2, url=URL, domain="blog.getzep.com", text="đánh dấu fact đó hết hiệu lực"),
        ev("chrome:4", "visit", 5, url="https://news.ycombinator.com/", title="Hacker News",
           domain="news.ycombinator.com", seconds=20),
    ]
    with TestClient(app) as client:
        assert client.post("/events", json=batch).json()["new"] == 5
        assert client.post("/events", json=batch[1:2]).json()["new"] == 0   # đọc lại cùng bài không tạo bản đôi

    drain()
    with connect() as conn:
        eps = conn.execute("SELECT kind, stage, meta FROM episodes").fetchall()
        stages = {r["stage"] for r in conn.execute("SELECT stage FROM events")}
    assert len(eps) == 1 and eps[0]["kind"] == "read" and eps[0]["stage"] == "indexed"   # không vào Graphiti
    assert eps[0]["meta"]["url"] == URL
    assert stages == {"grouped"}
    assert env.graph.added == []

    result = search("fact hết hiệu lực trong Graphiti")
    assert result.passages and result.passages[0].origin == 'Chrome, trang blog.getzep.com, bài "Graphiti: đồ thị có thời gian"'

    day = T0.date()
    with connect() as conn:   # người thợ đã tự viết nhật ký của ngày đó trong lúc chạy
        journal = conn.execute("SELECT path FROM journals WHERE day = %s", (day,)).fetchone()
    assert journal and journal["path"] == write_journal(day)
    text = (env.data / journal["path"]).read_text()
    assert "## Đã đọc" in text
    assert "- 10:00 Graphiti: đồ thị có thời gian (blog.getzep.com, 2 phút)" in text
    assert "Hacker News" not in text           # ở 20 giây, không đọc: không đưa vào nhật ký
    assert days_due() == []
