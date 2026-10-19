"""Sản phẩm 3: phiên Claude Code thành sự kiện, thành episode, vào bộ nhớ, thành nhật ký ngày."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lifelog import claude_code, pipeline
from lifelog.claude_code import parse_line, project_of, scrub
from lifelog.code_sessions import build_turns, split_text
from lifelog.config import get_settings
from lifelog.db import connect
from lifelog.journal import days_due, write_journal
from lifelog.pipeline import drain
from lifelog.reprocess import reprocess
from lifelog.search import search

DAY = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)   # 09:00 giờ Việt Nam, ngày 05/10
LONG = "Tôi đã thêm cột ngày tạo vào bảng danh sách và sửa lại phần xuất Excel cho khớp với cột mới. " * 3


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def line(n: int, kind: str, content, *, at: datetime, session: str = "s1", **extra) -> str:
    """Một dòng JSONL giống Claude Code ghi."""
    return json.dumps({
        "type": kind, "uuid": f"{session}-{n}", "timestamp": at.isoformat().replace("+00:00", "Z"),
        "sessionId": session, "cwd": "/Users/kim/App/demo_app", "gitBranch": "main",
        "message": {"role": kind, "content": content}, **extra,
    }, ensure_ascii=False)


def session_lines(start: datetime, session: str = "s1") -> list[str]:
    return [
        line(1, "user", "Thêm cột ngày tạo vào bảng danh sách giúp tôi, đặt sau cột trạng thái.", at=start, session=session),
        line(2, "assistant", [{"type": "thinking", "thinking": "nghĩ thầm"},
                              {"type": "text", "text": "Tôi sẽ sửa component bảng rồi chạy test."}],
             at=start + timedelta(seconds=5), session=session),
        line(3, "assistant", [{"type": "tool_use", "name": "Edit",
                               "input": {"file_path": "/Users/kim/App/demo_app/src/table.tsx", "old_string": "a", "new_string": "b"}}],
             at=start + timedelta(seconds=20), session=session),
        line(4, "user", [{"type": "tool_result", "content": "SECRET FILE CONTENT sk-abcdefghijklmnopqrstuvwxyz123456"}],
             at=start + timedelta(seconds=21), session=session),
        line(5, "assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "npm test", "description": "Chạy bộ test"}}],
             at=start + timedelta(seconds=30), session=session),
        line(6, "assistant", [{"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}],
             at=start + timedelta(seconds=31), session=session),
        line(7, "assistant", [{"type": "text", "text": LONG}], at=start + timedelta(seconds=60), session=session),
        line(8, "user", "<system-reminder>nội bộ</system-reminder>", at=start + timedelta(seconds=61), session=session),
        line(9, "user", "<task-notification>agent xong</task-notification>", at=start + timedelta(seconds=62), session=session),
        line(10, "user", "tóm tắt cũ", at=start + timedelta(seconds=63), session=session, isCompactSummary=True),
        line(11, "assistant", [{"type": "text", "text": "lời của agent con"}], at=start + timedelta(seconds=64),
             session=session, isSidechain=True),
    ]


@pytest.fixture()
def code(env, tmp_path, monkeypatch):
    """Thư mục ~/.claude/projects giả với một dự án được phép và một dự án không."""
    root = tmp_path / "claude"
    allowed = root / "-Users-kim-App-demo-app"
    other = root / "-Users-kim-App-bi-mat"
    for d in (allowed, other, allowed / "s1" / "subagents"):
        d.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_DIR", str(root))
    monkeypatch.setenv("CLAUDE_CODE_PROJECTS", "demo_app")
    get_settings.cache_clear()
    (other / "x.jsonl").write_text("\n".join(session_lines(DAY, "bi-mat")) + "\n", encoding="utf-8")
    (allowed / "s1" / "subagents" / "a.jsonl").write_text("\n".join(session_lines(DAY, "con")) + "\n", encoding="utf-8")
    env.session = allowed / "s1.jsonl"
    env.session.write_text("\n".join(session_lines(DAY)) + "\n", encoding="utf-8")
    return env


def again() -> None:
    """Cho thợ nhìn lại thư mục phiên ngay, không chờ hết quãng nghỉ giữa hai lần quét."""
    pipeline._last_code_scan = 0.0
    drain()


# ---------------------------------------------------------------- đọc file phiên

def test_khop_ten_du_an_voi_thu_muc_phien():
    projects = ["Acme_Price_List", "kim-rag", "intranet"]
    assert project_of("-Users-me-code-Acme-Price-List", projects) == "Acme_Price_List"
    assert project_of("-Users-me-code-kim-rag", projects) == "kim-rag"
    assert project_of("-Users-me-code-kim-rag--claude-worktrees-thu-nghiem", projects) == "kim-rag"
    assert project_of("-Users-me-code-kim-rag-2", projects) is None
    assert project_of("-Users-me-code-other-bot", projects) is None


def test_che_khoa_va_mat_khau():
    text = scrub("khóa sk-abcdefghijklmnopqrstuvwxyz123456 và hf_abcdefghijklmnopqrstuvwx, PASSWORD=sieubimat123 xong")
    assert "sk-abc" not in text and "hf_abc" not in text and "sieubimat" not in text
    assert text.count("[đã che]") == 3 and "PASSWORD=[đã che]" in text
    assert scrub("token vào 7547 ra 2369") == "token vào 7547 ra 2369"   # chữ thường không bị che nhầm


def test_mot_dong_thanh_su_kien_va_bo_phan_nhieu():
    events = [e for raw in session_lines(DAY) for e in parse_line(json.loads(raw), "demo_app")]
    assert [(e["type"], e["payload"].get("text") or e["payload"].get("file") or e["payload"].get("what")) for e in events] == [
        ("prompt", "Thêm cột ngày tạo vào bảng danh sách giúp tôi, đặt sau cột trạng thái."),
        ("reply", "Tôi sẽ sửa component bảng rồi chạy test."),
        ("tool", "/Users/kim/App/demo_app/src/table.tsx"),
        ("tool", "Chạy bộ test"),
        ("reply", LONG.strip()),
    ]
    # Kết quả tool, lời nghĩ thầm, tool chỉ đọc, dòng nội bộ, lời agent con: không cái nào thành sự kiện.
    assert "SECRET" not in json.dumps(events, ensure_ascii=False)
    assert events[0]["payload"]["project"] == "demo_app" and events[0]["payload"]["session"] == "s1"
    assert len({e["uid"] for e in events}) == len(events)


def test_cat_chu_dai_va_gom_viec_da_lam():
    pieces = split_text(("Một đoạn văn khá dài. " * 40 + "\n\n") * 3)
    assert len(pieces) > 1 and all(len(p) <= 1000 for p in pieces)
    assert split_text("ngắn") == ["ngắn"] and split_text("  ") == []

    at = DAY
    events = [
        {"type": "prompt", "ts": at, "payload": {"text": "sửa giúp", "cwd": "/w"}},
        {"type": "tool", "ts": at, "payload": {"tool": "Edit", "file": "/w/a.py"}},
        {"type": "tool", "ts": at, "payload": {"tool": "Edit", "file": "/w/a.py"}},
        {"type": "tool", "ts": at, "payload": {"tool": "Write", "file": "/private/tmp/claude-501/x/scratchpad/nhap.js"}},
        {"type": "tool", "ts": at, "payload": {"tool": "Bash", "what": "Chạy test"}},
        {"type": "reply", "ts": at, "payload": {"text": "xong"}},
    ]
    assert [(t["speaker"], t["text"]) for t in build_turns(events, "Kim")] == [
        ("Kim", "sửa giúp"), ("Claude", "(Việc đã làm: sửa file a.py. Chạy test)"), ("Claude", "xong"),
    ]


# ---------------------------------------------------------------- cả luồng

def test_phien_claude_code_thanh_episode_va_vao_bo_nho(code):
    drain()

    assert rows("SELECT count(*) AS n FROM events")[0]["n"] == 5          # chỉ dự án được phép, không agent con
    assert {e["payload"]["project"] for e in rows("SELECT payload FROM events")} == {"demo_app"}
    ep = rows("SELECT * FROM episodes")[0]
    assert (ep["kind"], ep["stage"], ep["asr_backend"]) == ("code", "ingested", "claude-code")
    assert ep["meta"]["project"] == "demo_app" and ep["meta"]["files"] == ["src/table.tsx"]
    assert (ep["meta"]["prompts"], ep["meta"]["actions"], ep["meta"]["branch"]) == (1, 2, "main")
    assert ep["started_at"] == DAY and ep["summary"] == "**Đã làm:** việc ở demo_app."

    turns = rows("SELECT s.speaker, t.text FROM segments s JOIN segment_texts t ON t.segment_id = s.id ORDER BY s.idx")
    assert [t["speaker"] for t in turns] == ["Kim", "Claude", "Claude", "Claude"]
    assert turns[2]["text"] == "(Việc đã làm: sửa file src/table.tsx. Chạy bộ test)"

    md = (code.data / ep["export_path"]).read_text(encoding="utf-8")
    assert md.startswith("# Phiên Claude Code 05/10/2026 09:00–09:01") and "Dự án: demo_app (nhánh main)" in md

    # LLM được đưa đúng lời thoại của phiên để tóm tắt.
    project, transcript = code.summaries[0]
    assert project == "demo_app" and transcript.startswith("Kim: Thêm cột ngày tạo") and "Chạy bộ test" in transcript

    wins = rows("SELECT kind, text FROM windows ORDER BY idx")
    assert wins[0]["kind"] == "summary" and all(w["kind"] == "turns" for w in wins[1:]) and len(wins) > 1
    assert wins[0]["text"].startswith("Phiên làm việc với Claude Code, dự án demo_app, ngày 05/10/2026 từ 09:00 tới 09:01.")
    assert "File đã sửa: src/table.tsx" in wins[0]["text"]

    # Graphiti nhận bản tóm tắt dạng văn xuôi, không nhận từng lời thoại.
    assert len(code.graph.added) == 1 and code.graph.added[0]["prose"] is True
    assert code.graph.added[0]["text"] == wins[0]["text"]


def test_chi_doc_phan_moi_va_khong_tao_ban_doi(code):
    drain()
    more = [
        line(20, "user", "Giờ thêm nút xuất Excel cho bảng đó, đặt ở góc phải phía trên bảng nhé.", at=DAY + timedelta(hours=3)),
        line(21, "assistant", [{"type": "text", "text": LONG}], at=DAY + timedelta(hours=3, seconds=30)),
    ]
    with code.session.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(more) + "\n")
        fh.write('{"type": "user", "uuid": "dang-ghi-do')       # dòng cuối đang được ghi dở
    again()

    assert rows("SELECT count(*) AS n FROM events")[0]["n"] == 7
    eps = rows("SELECT started_at, stage FROM episodes ORDER BY started_at")
    # Cách phiên trước ba tiếng nên thành episode riêng; episode cũ không bị đụng tới.
    assert [e["started_at"] for e in eps] == [DAY, DAY + timedelta(hours=3)]
    assert len(code.graph.added) == 2

    # Quét lại từ đầu file (ví dụ mất sổ ghi vị trí đọc) cũng không sinh sự kiện trùng.
    with connect() as conn:
        conn.execute("DELETE FROM event_files")
    assert claude_code.scan() == 0


def test_phien_dang_dien_ra_thi_chua_dong_va_cum_it_chu_bi_bo(code):
    now = datetime.now(timezone.utc)
    live = code.session.parent / "s2.jsonl"
    live.write_text("\n".join([
        line(1, "user", "Đang làm dở việc này, kiểm tra giúp tôi phần đăng nhập còn lỗi gì không.", at=now, session="s2"),
        line(2, "assistant", [{"type": "text", "text": LONG}], at=now, session="s2"),
    ]) + "\n", encoding="utf-8")
    tiny = code.session.parent / "s3.jsonl"
    tiny.write_text(line(1, "user", "chào", at=DAY, session="s3") + "\n", encoding="utf-8")
    drain()

    stages = {(r["session"], r["stage"]) for r in rows("SELECT DISTINCT payload->>'session' AS session, stage FROM events")}
    assert stages == {("s1", "grouped"), ("s2", "received"), ("s3", "ignored")}
    assert rows("SELECT count(*) AS n FROM episodes")[0]["n"] == 1

    # Phiên kéo dài nhiều ngày: quãng hôm trước đã kết thúc thì được gom ngay, quãng đang diễn ra thì chờ.
    old = live.read_text(encoding="utf-8")
    live.write_text("\n".join([
        line(8, "user", "Hôm qua: rà lại phần đăng nhập, liệt kê các chỗ còn thiếu kiểm tra quyền.", at=now - timedelta(days=1), session="s2"),
        line(9, "assistant", [{"type": "text", "text": LONG}], at=now - timedelta(days=1), session="s2"),
    ]) + "\n" + old, encoding="utf-8")
    with connect() as conn:
        conn.execute("DELETE FROM event_files")   # file đổi phần đầu nên đọc lại từ đầu; uid chặn bản trùng
    again()
    s2 = rows("SELECT stage, count(*) AS n FROM events WHERE payload->>'session' = 's2' GROUP BY stage ORDER BY stage")
    assert s2 == [{"stage": "grouped", "n": 2}, {"stage": "received", "n": 2}]
    assert rows("SELECT count(*) AS n FROM episodes")[0]["n"] == 2


def test_tim_duoc_viec_da_lam_va_biet_thuoc_du_an_nao(code):
    drain()
    summary = rows("SELECT text FROM windows WHERE kind = 'summary'")[0]["text"]
    res = search(summary, since="2026-10-05", until="2026-10-05")
    top = res.passages[0]
    assert top.summary and top.origin == "Claude Code, dự án demo_app" and top.score == pytest.approx(1.0, abs=1e-4)
    assert res.facts and res.facts[0].sources[0].origin == "Claude Code, dự án demo_app"
    assert search(summary, since="2026-10-06").passages == []


# ---------------------------------------------------------------- nhật ký

def test_nhat_ky_ngay_tu_sinh_va_viet_lai_khi_co_them_phien(code):
    drain()
    journal = rows("SELECT * FROM journals")
    assert len(journal) == 1 and journal[0]["episodes"] == 1 and str(journal[0]["day"]) == "2026-10-05"
    text = (code.data / journal[0]["path"]).read_text(encoding="utf-8")
    assert text.startswith("# Nhật ký làm việc 05/10/2026\n\n1 phiên Claude Code ở 1 dự án: demo_app.\n\nTổng quan ngày 05/10/2026.")
    assert "## demo_app" in text and "### 09:00–09:01 · episode" in text and "File đã sửa: `src/table.tsx`" in text
    assert days_due() == []                                   # đã đủ, không viết lại mãi

    with code.session.open("a", encoding="utf-8") as fh:
        fh.write("\n".join([
            line(30, "user", "Chiều nay sửa tiếp phần phân trang của bảng, mỗi trang năm mươi dòng.", at=DAY + timedelta(hours=5)),
            line(31, "assistant", [{"type": "text", "text": LONG}], at=DAY + timedelta(hours=5, seconds=9)),
        ]) + "\n")
    again()
    journal = rows("SELECT * FROM journals")
    assert len(journal) == 1 and journal[0]["episodes"] == 2
    assert "### 14:00–14:00" in (code.data / journal[0]["path"]).read_text(encoding="utf-8")
    assert write_journal(DAY.date() + timedelta(days=30)) is None   # ngày không có phiên nào


def test_chay_lai_phien_code(code):
    drain()
    first = code.graph.added[0]["uuid"]

    # Phiên code không có audio: chạy lại từ bước tách người nói hay chuyển chữ không đụng tới nó.
    assert reprocess("diarize", since=DAY.date())["episodes"] == 0
    assert reprocess("transcribe", since=DAY.date())["episodes"] == 0

    # Viết lại tóm tắt: vector, graph và nhật ký của ngày đó đều được dựng lại.
    assert reprocess("index", since=DAY.date())["episodes"] == 1
    assert rows("SELECT summary FROM episodes")[0]["summary"] is None and rows("SELECT count(*) AS n FROM journals")[0]["n"] == 0
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested" and len(code.summaries) >= 4
    assert code.graph.removed == [first] and rows("SELECT count(*) AS n FROM journals")[0]["n"] == 1

    # Gom lại từ đầu: episode cũ bị xóa, sự kiện quay về chờ gom, rồi thành episode mới.
    old_id = rows("SELECT id FROM episodes")[0]["id"]
    reprocess("group", since=DAY.date())
    assert rows("SELECT count(*) AS n FROM episodes")[0]["n"] == 0
    assert {r["stage"] for r in rows("SELECT stage FROM events")} == {"received"}
    drain()
    new = rows("SELECT id, stage FROM episodes")
    assert len(new) == 1 and new[0]["id"] != old_id and new[0]["stage"] == "ingested"
    assert len(code.graph.live) == 1


# ---------------------------------------------------------------- POST /events

def test_post_events_nhan_me_su_kien_va_chong_gui_trung(env):
    from lifelog.api import app

    batch = [
        {"uid": "mac:1", "source": "mac", "type": "app", "ts": "2026-10-05T09:00:00+07:00", "payload": {"app": "Chrome"}},
        {"uid": "mac:2", "source": "mac", "type": "app", "ts": "2026-10-05T09:05:00+07:00"},
    ]
    with TestClient(app) as client:
        assert client.post("/events", json=batch).json() == {"received": 2, "new": 2}
        assert client.post("/events", json=batch).json() == {"received": 2, "new": 0}
        assert client.post("/events", json=[{**batch[0], "uid": "mac:3", "ts": "2026-10-05T09:00:00"}]).status_code == 400
        assert client.post("/events", json=[{"uid": "mac:4"}]).status_code == 422
    stored = rows("SELECT uid, source, type, payload, stage FROM events ORDER BY uid")
    assert [s["uid"] for s in stored] == ["mac:1", "mac:2"] and stored[0]["payload"] == {"app": "Chrome"}
    assert {s["stage"] for s in stored} == {"received"}


def test_ten_phien_vao_episode_va_doi_theo_ten_moi(code):
    with code.session.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "ai-title", "aiTitle": "Thêm cột bảng", "sessionId": "s1"}) + "\n")
    drain()
    ep = rows("SELECT id, meta, export_path FROM episodes")[0]
    assert ep["meta"]["title"] == "Thêm cột bảng"
    assert "- Tên phiên: Thêm cột bảng" in (code.data / ep["export_path"]).read_text(encoding="utf-8")
    summary = rows("SELECT text FROM windows WHERE kind = 'summary'")[0]["text"]
    assert summary.startswith('Phiên làm việc với Claude Code "Thêm cột bảng", dự án demo_app')
    assert search(summary).passages[0].origin == 'Claude Code, dự án demo_app, phiên "Thêm cột bảng"'
    assert "· Thêm cột bảng · episode" in (code.data / rows("SELECT path FROM journals")[0]["path"]).read_text(encoding="utf-8")

    # Người dùng tự đặt tên thì tên đó thắng, kể cả khi Claude Code đặt lại tên tự động sau đó.
    with code.session.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "custom-title", "customTitle": "Ngày tạo cho bảng", "sessionId": "s1"}) + "\n")
        fh.write(json.dumps({"type": "ai-title", "aiTitle": "Tên tự động mới", "sessionId": "s1"}) + "\n")
    again()
    assert rows("SELECT meta FROM episodes")[0]["meta"]["title"] == "Ngày tạo cho bảng"

    # Lịch sử đọc trước khi có tên: đọc lại file chỉ để lấy tên.
    with connect() as conn:
        conn.execute("UPDATE episodes SET meta = meta - 'title'")
        conn.execute("DELETE FROM code_sessions")
    assert claude_code.backfill_titles() == 1
    assert rows("SELECT meta FROM episodes")[0]["meta"]["title"] == "Ngày tạo cho bảng"
