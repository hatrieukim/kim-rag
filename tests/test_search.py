"""Tool `search`: hỏi cả hai kho, lọc theo thời gian và người nói, trả kết quả kèm nguồn."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from lifelog import mcp_server
from lifelog.config import get_settings
from lifelog.db import connect
from lifelog.ingest import ingest_file
from lifelog.pipeline import drain
from lifelog.search import _merge, parse_when, render, search

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)  # 09:00 giờ Việt Nam, ngày 12
T1 = T0 + timedelta(days=1)                              # 09:00 ngày 13


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture()
def two_days(env, audio_dir):
    """Hai episode ở hai ngày khác nhau, đã vào cả hai kho."""
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    ingest_file(audio_dir / "b.m4a", started_at=T1, close_episode=True)
    drain()
    eps = rows("SELECT id FROM episodes ORDER BY started_at")
    assert len(eps) == 2
    env.day1, env.day2 = str(eps[0]["id"]), str(eps[1]["id"])
    return env


def test_tim_thay_dung_doan_va_fact_lan_duoc_ve_nguon(two_days):
    target = rows("SELECT * FROM windows WHERE episode_id = %s ORDER BY idx", two_days.day2)[0]
    res = search(target["text"])          # dịch vụ embed giả: cùng chữ thì cùng vector

    top = res.passages[0]
    assert top.episode_id == two_days.day2 and top.score == pytest.approx(1.0, abs=1e-4)
    assert (top.first_idx, top.last_idx) == (target["first_idx"], target["last_idx"])
    assert top.text == target["text"] and top.speakers == ["SPEAKER_01"]

    assert {f.sources[0].episode_id for f in res.facts} == {two_days.day1, two_days.day2}
    newest = res.facts[0]
    part = rows("SELECT * FROM graph_parts WHERE episode_id = %s", two_days.day2)[0]
    assert (newest.sources[0].first_idx, newest.sources[0].last_idx) == (part["first_idx"], part["last_idx"])

    text = render(res)
    assert f"episode {two_days.day2[:8]}" in text and "13/10/2026 09:00" in text and "độ gần 1.00" in text
    assert f"- fact từ episode {two_days.day2[:8]}" in text


def test_loc_theo_thoi_gian_ap_cho_ca_hai_kho(two_days):
    only2 = search("kế hoạch", since="2026-10-13")
    assert {p.episode_id for p in only2.passages} == {two_days.day2}
    assert [f.sources[0].episode_id for f in only2.facts] == [two_days.day2]

    # Mốc kết thúc là một ngày trơn thì tính hết ngày đó.
    only1 = search("kế hoạch", until="2026-10-12")
    assert {p.episode_id for p in only1.passages} == {two_days.day1}
    assert [f.sources[0].episode_id for f in only1.facts] == [two_days.day1]

    morning = search("kế hoạch", since="2026-10-13T08:00", until="2026-10-13T10:00")
    assert {p.episode_id for p in morning.passages} == {two_days.day2}
    nothing = search("kế hoạch", since="2026-10-13T12:00")
    assert nothing.passages == [] and nothing.facts == []
    assert "Không có đoạn nào" in render(nothing) and "Không có fact nào" in render(nothing)


def test_loc_theo_nguoi_noi(two_days):
    assert search("kế hoạch", speakers=["speaker_01"]).passages     # không phân biệt hoa thường
    nobody = search("kế hoạch", speakers=["Anh Tuấn"])
    assert nobody.passages == [] and nobody.facts == []

    # Người nói được gán tên thì fact tìm được theo tên đó.
    with connect() as conn:
        person = conn.execute("INSERT INTO speakers (name) VALUES ('Anh Tuấn') RETURNING id").fetchone()["id"]
        conn.execute("UPDATE episode_speakers SET speaker_id = %s WHERE episode_id = %s", (person, two_days.day2))
    named = search("kế hoạch", speakers=["anh tuấn"])
    assert [f.sources[0].episode_id for f in named.facts] == [two_days.day2]


def test_fact_cua_ban_da_bi_thay_thi_khong_tra_ve(two_days):
    with connect() as conn:
        conn.execute("UPDATE graph_parts SET episode_id = NULL WHERE episode_id = %s", (two_days.day1,))
    res = search("kế hoạch")
    assert [f.sources[0].episode_id for f in res.facts] == [two_days.day2]


def test_mot_kho_hong_thi_kho_kia_van_tra_loi(two_days, monkeypatch):
    two_days.graph.unavailable = "FalkorDB giả đang tắt"
    res = search("kế hoạch")
    assert res.passages and res.facts == []
    assert any("FalkorDB giả đang tắt" in n for n in res.notes) and "Lưu ý: Kho fact không hỏi được" in render(res)

    two_days.graph.unavailable = None
    monkeypatch.setenv("GRAPH", "none")
    get_settings.cache_clear()
    off = search("kế hoạch")
    assert off.passages and off.facts == [] and two_days.graph.asked == []
    assert any("GRAPH=none" in n for n in off.notes)


def test_bo_doan_kem_han_va_gop_doan_chong_nhau(two_days):
    target = rows("SELECT text FROM windows WHERE episode_id = %s ORDER BY idx", two_days.day1)[0]["text"]
    res = search(target)
    # Đoạn trùng khít được 1,0; các đoạn khác với vector giả chỉ quanh 0,75 nên bị bỏ.
    assert len(res.passages) == 1 and any("kém liên quan" in n for n in res.notes)

    hits = [
        {"episode_id": "a", "first_idx": 4, "last_idx": 6, "score": 0.6},
        {"episode_id": "a", "first_idx": 0, "last_idx": 4, "score": 0.7},
        {"episode_id": "a", "first_idx": 9, "last_idx": 10, "score": 0.9},
        {"episode_id": "b", "first_idx": 5, "last_idx": 6, "score": 0.8},
    ]
    merged = _merge(hits)
    assert [(m["episode_id"], m["first_idx"], m["last_idx"], m["score"], m["merged"]) for m in merged] == [
        ("a", 9, 10, 0.9, False), ("b", 5, 6, 0.8, False), ("a", 0, 6, 0.7, True),
    ]


def test_doc_thoi_gian_va_bao_loi_ro(env):
    tz = get_settings().tz
    assert parse_when("2026-10-13") == datetime(2026, 10, 13, tzinfo=tz)
    assert parse_when("2026-10-13", end=True) == datetime(2026, 10, 14, tzinfo=tz)
    assert parse_when("2026-10-13T09:30") == datetime(2026, 10, 13, 9, 30, tzinfo=tz)
    assert parse_when("2026-10-13T02:30:00Z") == datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc)
    assert parse_when(None) is None and parse_when("  ") is None

    with pytest.raises(ValueError, match="không đọc được thời gian"):
        search("kế hoạch", since="hôm qua")
    with pytest.raises(ValueError, match="trống"):
        search("   ")
    with pytest.raises(ValueError, match="trước mốc kết thúc"):
        search("kế hoạch", since="2026-10-13", until="2026-10-12")


def test_tool_cua_mcp_tra_ve_chu_cho_agent(two_days):
    out = mcp_server.search("kế hoạch", since="2026-10-13", limit=3)
    assert out.startswith('Tìm "kế hoạch" (từ 13/10/2026 00:00)')
    assert "## Nguyên văn" in out and "## Fact" in out and f"episode {two_days.day2[:8]}" in out
    assert two_days.day1[:8] not in out

    # Tham số sai thì agent nhận được lời giải thích để tự sửa, không phải một lỗi trống.
    with pytest.raises(ToolError, match="không đọc được thời gian 'hôm qua'"):
        mcp_server.search("kế hoạch", since="hôm qua")
