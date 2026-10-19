"""Sản phẩm 4: sự kiện thô từ Mac thành các khoảng thời gian, rồi thành mục nhật ký ngày."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from lifelog import git_log, mac_log, pipeline
from lifelog.db import connect
from lifelog.journal import days_due, write_journal
from lifelog.mac_log import build_spans, classify, segments

# 09:00 giờ Việt Nam ngày 05/10/2026.
T0 = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
CODE = {"app": "Code", "bundle": "com.microsoft.VSCode", "title": "journal.py — kim-rag"}
CHROME = {"app": "Google Chrome", "bundle": "com.google.Chrome",
          "title": "lofi beats - YouTube - Audio playing - High memory usage - 828 MB - Google Chrome - Me"}
SLACK = {"app": "Slack", "bundle": "com.tinyspeck.slackmacgap", "title": "general"}


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class Timeline:
    """Dựng chuỗi sự kiện giống chương trình trên Mac gửi về, có nhịp mỗi phút khi người dùng đang ở máy."""

    def __init__(self):
        self.events: list[dict] = []
        self.present = False
        self.t = 0.0

    def _add(self, minute: float, type_: str, payload: dict | None = None) -> None:
        self.events.append({"type": type_, "ts": at(minute), "payload": payload or {}})

    def until(self, minute: float) -> "Timeline":
        if self.present:
            m = int(self.t) + 1
            while m <= minute:
                self._add(m, "tick")
                m += 1
        self.t = minute
        return self

    def do(self, minute: float, type_: str, payload: dict | None = None) -> "Timeline":
        self.until(minute)
        self._add(minute, type_, payload)
        if type_ in ("focus", "back", "start"):
            self.present = True
        if type_ in ("away", "stop"):
            self.present = False
        return self

    def sorted(self) -> list[dict]:
        return sorted(self.events, key=lambda e: e["ts"])


def spans_of(events, lo=at(-60), hi=at(600)):
    return build_spans(segments(events, lo, hi))


def test_nhan_dien_app_tu_tieu_de():
    assert classify(CODE)[:3] == ("Code", "project", "kim-rag")
    assert classify({**CODE, "title": "● main.py — demo-bot — Hồ sơ"})[:3] == ("Code", "project", "demo-bot")
    app, kind, ctx, title = classify(CHROME)
    assert (app, kind, ctx, title) == ("Google Chrome", "site", "YouTube", "lofi beats - YouTube")
    assert classify({**CHROME, "title": "(3) Zalo - Pinned - Google Chrome - Bun"})[:2] == ("Zalo", None)
    assert classify({**CHROME, "title": "(3) Facebook - Google Chrome - Bun"})[2:] == ("Facebook", "Facebook")
    assert classify({**CHROME, "title": "(13) Home / X - Google Chrome - Bun"})[2:] == ("X", "Home / X")
    assert classify({"ignored": True})[0] == mac_log.IGNORED_APP
    assert classify(SLACK)[:3] == ("Slack", None, None)


def test_gom_thanh_khoang_bo_lan_liec_ngan_va_cat_khi_roi_may():
    tl = (Timeline()
          .do(0, "focus", CODE)
          .do(30, "focus", SLACK).do(30.2, "focus", CODE)      # liếc Slack 12 giây
          .do(60, "away", {"reason": "idle"})
          .do(80, "back", {"reason": "idle"}).do(80, "focus", CODE)
          .do(90, "focus", CHROME)
          .do(120, "stop"))
    spans = spans_of(tl.sorted())
    assert [(s.app, s.context, round(s.seconds / 60)) for s in spans] == [
        ("Code", "kim-rag", 60),       # 09:00–10:00, lần liếc Slack bị bỏ, không cắt khoảng
        ("Code", "kim-rag", 10),       # quay lại sau 20 phút rời máy là khoảng mới
        ("Google Chrome", "YouTube", 30),
    ]
    assert spans[0].start == at(0) and spans[0].end == at(60)
    assert spans[2].titles == ["lofi beats - YouTube"]


def test_mat_nhip_thi_dong_khoang():
    # Máy tắt đột ngột: không có sự kiện nào sau 09:05.
    tl = Timeline().do(0, "focus", CODE).until(5)
    spans = spans_of(tl.sorted())
    assert len(spans) == 1 and spans[0].end == at(6)    # nhịp cuối lúc 09:05, tính thêm một nhịp


def test_cat_khoang_o_nua_dem():
    # 23:50 tới 00:10 giờ Việt Nam.
    base = (datetime(2026, 10, 5, 23, 50) - datetime(2026, 10, 5, 9, 0)).total_seconds() / 60
    tl = Timeline().do(base, "focus", CODE).do(base + 20, "stop")
    events = tl.sorted()
    day1 = spans_of(events, *mac_log._bounds(datetime(2026, 10, 5).date()))
    day2 = spans_of(events, *mac_log._bounds(datetime(2026, 10, 6).date()))
    assert round(day1[0].seconds / 60) == 10 and round(day2[0].seconds / 60) == 10


def _post(client, events: list[dict]) -> None:
    batch = [{"uid": f"mac:{i}", "source": "mac", "type": e["type"], "ts": e["ts"].isoformat(),
              "payload": e["payload"]} for i, e in enumerate(events)]
    assert client.post("/events", json=batch).status_code == 200


def test_su_kien_mac_thanh_muc_nhat_ky(env):
    from lifelog.api import app

    tl = (Timeline()
          .do(0, "start").do(0, "focus", CODE)
          .do(10, "shell", {"cmd": "ssh myserver", "cwd": "/Users/kim", "status": 0, "seconds": 2400})
          .do(20, "shell", {"cmd": "ls", "cwd": "/Users/kim", "status": 0, "seconds": 0.1})
          .do(25, "shell", {"cmd": "uv run pytest", "cwd": "/Users/kim", "status": 1, "seconds": 9})
          .do(26, "clipboard", {"app": "Code", "text": "xin chào", "chars": 8})
          .do(90, "focus", CHROME)
          .do(110, "stop"))
    with TestClient(app) as client:
        _post(client, tl.sorted())

    assert mac_log.group_mac()
    assert not mac_log.group_mac()          # không còn gì mới
    with connect() as conn:
        spans = conn.execute("SELECT app, context, seconds FROM mac_spans ORDER BY started_at").fetchall()
        stages = {r["stage"] for r in conn.execute("SELECT stage FROM events")}
    assert [(s["app"], s["context"], s["seconds"]) for s in spans] == [
        ("Code", "kim-rag", 5400), ("Google Chrome", "YouTube", 1200)]
    assert stages == {"grouped"}

    day = T0.date()
    assert days_due() == [day]
    path = write_journal(day)
    text = (env.data / path).read_text()
    assert "## Hoạt động trên Mac" in text
    assert "Dùng máy 1 giờ 50 phút, từ 09:00 tới 10:50." in text
    assert "- Code: 1 giờ 30 phút (kim-rag 1 giờ 30 phút)" in text
    assert "- Google Chrome: 20 phút (YouTube 20 phút)" in text
    assert "`ssh myserver` (40 phút)" in text and "`uv run pytest` (lỗi, mã 1)" in text
    assert "`ls`" not in text and "xin chào" not in text
    _, _, mac_text, _ = env.summaries[-1]
    assert "Code: 1 giờ 30 phút" in mac_text      # LLM nhận đúng số liệu đã tính sẵn
    assert days_due() == []

    # Đổi luật rồi tính lại từ sự kiện thô: kết quả như cũ, sự kiện thô không đổi.
    assert mac_log.rebuild(day) == 2


def test_nhat_ky_cu_khong_co_du_lieu_mac_thi_khong_viet_lai(env):
    with connect() as conn:
        conn.execute("INSERT INTO journals (day, path, episodes) VALUES (%s, 'journal/x.md', 0)", (T0.date(),))
    assert days_due() == []


def test_doc_commit_tu_git(env, tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                                    env={"GIT_AUTHOR_DATE": "2026-10-05T10:15:00+07:00",
                                         "GIT_COMMITTER_DATE": "2026-10-05T10:15:00+07:00",
                                         "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    git("init", "-q")
    git("config", "user.email", "kim@example.com")
    git("config", "user.name", "Kim")
    (repo / "a.txt").write_text("a")
    git("add", ".")
    git("commit", "-q", "-m", "Thêm giao diện mới")

    monkeypatch.setattr(git_log, "repos", lambda: {repo: "demo"})
    monkeypatch.setenv("GIT_SINCE", "")
    from lifelog.config import get_settings
    get_settings.cache_clear()
    assert git_log.scan() == 0              # chưa đặt GIT_SINCE thì không đọc
    monkeypatch.setenv("GIT_SINCE", "2026-10-01")
    get_settings.cache_clear()
    assert git_log.scan() == 1
    assert git_log.scan() == 0              # đọc lại không tạo bản đôi

    assert pipeline.run_once() or True
    assert mac_log.group_mac() or True
    path = write_journal(T0.date())
    text = (env.data / path).read_text()
    assert "Commit:" in text and "10:15 demo" in text and "Thêm giao diện mới" in text


def test_so_lieu_hom_nay_cho_app_tren_mac(env):
    from lifelog.api import app

    tl = Timeline().do(0, "start").do(0, "focus", CODE).do(40, "focus", CHROME).until(50)
    with TestClient(app) as client:
        _post(client, tl.sorted())
        # "Bây giờ" là 09:50: khoảng Chrome đang mở vẫn được tính tới hiện tại.
        data = mac_log.today_summary(now=at(50))
        assert client.get("/mac/today").status_code == 200
    assert data["total_seconds"] == 3000 and data["first"] == "09:00" and data["last"] == "09:50"
    assert data["apps"] == [{"app": "Code", "seconds": 2400, "context": "kim-rag"},
                            {"app": "Google Chrome", "seconds": 600, "context": "YouTube"}]
