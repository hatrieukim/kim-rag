"""Sản phẩm 2: transcript thành vector nguyên văn trong Postgres và thành các phần nạp vào Graphiti."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lifelog.config import get_settings
from lifelog.db import connect
from lifelog.graph import speaker_name
from lifelog.ingest import ingest_file
from lifelog.pipeline import drain
from lifelog.reprocess import reprocess, retry_failed
from lifelog.windows import build_windows

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)  # 09:00 giờ Việt Nam


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def turn(idx: int, speaker: str, text: str) -> dict:
    return {"idx": idx, "speaker": speaker, "text": text, "started_at": T0 + timedelta(seconds=10 * idx), "length_s": 8.0}


def small_parts(monkeypatch, chars: int = 5) -> None:
    """Ép mỗi lượt nói thành một phần riêng khi nạp vào Graphiti."""
    monkeypatch.setenv("GRAPH_PART_CHARS", str(chars))
    get_settings.cache_clear()


# ---------------------------------------------------------------- cắt cửa sổ

def test_cua_so_gom_luot_noi_toi_khi_du_dai():
    turns = [turn(i, "A" if i % 2 == 0 else "B", "x" * 40) for i in range(5)]
    wins = build_windows(turns, max_chars=100)
    assert [(w.first_idx, w.last_idx) for w in wins] == [(0, 1), (2, 3), (4, 4)]
    assert wins[0].speakers == ["A", "B"] and wins[0].text == f"A: {'x' * 40}\nB: {'x' * 40}"
    assert wins[0].started_at == T0 and wins[0].ended_at == T0 + timedelta(seconds=18)


def test_cua_so_lap_lai_luot_cuoi_va_khong_ket_khi_luot_qua_dai():
    turns = [turn(i, "A", "x" * 40) for i in range(5)]
    wins = build_windows(turns, max_chars=100, overlap=1)
    assert [(w.first_idx, w.last_idx) for w in wins] == [(0, 1), (1, 2), (2, 3), (3, 4)]
    # Các lượt liền nhau của cùng một người gộp thành một dòng.
    assert wins[0].text.count("A:") == 1

    # Một lượt dài hơn giới hạn vẫn đứng riêng, không lặp vô tận.
    long = [turn(0, "A", "x" * 500), turn(1, "B", "y" * 500), turn(2, "A", "z" * 10)]
    assert [(w.first_idx, w.last_idx) for w in build_windows(long, max_chars=100, overlap=1)] == [(0, 0), (1, 1), (2, 2)]
    assert build_windows([], max_chars=100) == []


def test_nhan_nguoi_noi_chi_co_nghia_trong_mot_episode():
    assert speaker_name("SPEAKER_02", "ab12cd34-0000") == "Người nói 2 (bản ghi ab12cd34)"
    assert speaker_name("SPEAKER_02", "ffff0000-0000") != speaker_name("SPEAKER_02", "ab12cd34-0000")
    assert speaker_name("Anh Tuấn", "ab12cd34-0000", known=True) == "Anh Tuấn"   # đã có tên thật thì giữ nguyên
    # A role label of an unnamed voice is still one person in one recording.
    assert speaker_name("nhân viên tổng đài", "ab12cd34-0000") == "nhân viên tổng đài (bản ghi ab12cd34)"


# ---------------------------------------------------------------- cả luồng

def test_transcript_vao_ca_hai_kho(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()

    ep = rows("SELECT * FROM episodes")[0]
    assert ep["stage"] == "ingested" and ep["error"] is None
    segs = rows("SELECT idx FROM segments ORDER BY idx")

    wins = rows("SELECT *, vector_dims(embedding) AS dims FROM windows ORDER BY idx")
    assert wins and all(w["dims"] == 1536 and w["embedding_model"] == "fake-embed" for w in wins)
    assert wins[0]["first_idx"] == 0 and wins[-1]["last_idx"] == segs[-1]["idx"]
    assert wins[0]["text"].startswith("SPEAKER_01: [fake] lượt")
    assert timedelta(0) < wins[0]["started_at"] - T0 < timedelta(seconds=3)

    parts = rows("SELECT * FROM graph_parts ORDER BY part")
    assert len(parts) == len(env.graph.added) == 1
    assert str(parts[0]["uuid"]) == env.graph.added[0]["uuid"]         # lần được từ fact về episode
    assert parts[0]["episode_id"] == ep["id"] and (parts[0]["first_idx"], parts[0]["last_idx"]) == (0, segs[-1]["idx"])
    assert (parts[0]["model"], parts[0]["input_tokens"], parts[0]["output_tokens"]) == ("fake-llm", 100, 50)
    # Trong graph, nhãn người nói mang mã episode để không bị gộp nhầm giữa các episode.
    assert env.graph.added[0]["text"].startswith(f"Người nói 1 (bản ghi {str(ep['id'])[:8]}): ")


def test_tat_graph_thi_dung_o_buoc_indexed(env, audio_dir, monkeypatch):
    monkeypatch.setenv("GRAPH", "none")
    get_settings.cache_clear()
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "indexed"
    assert rows("SELECT count(*) AS n FROM windows")[0]["n"] > 0 and env.graph.added == []


def test_falkordb_chua_chay_thi_cho_chu_khong_tinh_la_hong(env, audio_dir):
    env.graph.unavailable = "không kết nối được FalkorDB giả"
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    ep = rows("SELECT stage, attempts, error FROM episodes")[0]
    assert ep["stage"] == "indexed" and ep["attempts"] == 0 and "FalkorDB giả" in ep["error"]

    env.graph.unavailable = None
    retry_failed()
    drain()
    assert rows("SELECT stage, error FROM episodes")[0] == {"stage": "ingested", "error": None}


def test_loi_giua_chung_khi_nap_graph_thi_khong_nap_lai_phan_da_xong(env, audio_dir, monkeypatch):
    small_parts(monkeypatch)
    ingest_file(audio_dir / "a.m4a", started_at=T0)
    ingest_file(audio_dir / "b.m4a", started_at=T0 + timedelta(seconds=30), close_episode=True)
    env.graph.fail_after = 1
    drain()

    ep = rows("SELECT stage FROM episodes")
    assert ep == [{"stage": "ingested"}]
    total = rows("SELECT count(*) AS n FROM segments")[0]["n"]
    assert total >= 2
    # Mỗi lượt nói là một phần, mỗi phần chỉ được nạp đúng một lần dù lần chạy đầu hỏng giữa chừng.
    assert sorted(a["n"] for a in env.graph.added) == list(range(total))
    assert [p["part"] for p in rows("SELECT part FROM graph_parts ORDER BY part")] == list(range(total))


# ---------------------------------------------------------------- chạy lại

def test_nap_lai_graph_thi_go_ban_cu_truoc_va_giu_nguyen_vector(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    old_uuid = env.graph.added[0]["uuid"]
    old_windows = rows("SELECT id FROM windows ORDER BY idx")
    calls = env.fake.calls

    assert reprocess("ingest", since=T0.date())["episodes"] == 1
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "indexed"
    drain()

    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
    assert env.graph.removed == [old_uuid]
    assert len(env.graph.live) == 1 and env.graph.live[0]["uuid"] != old_uuid
    assert [str(p["uuid"]) for p in rows("SELECT uuid FROM graph_parts")] == [env.graph.live[0]["uuid"]]
    assert rows("SELECT id FROM windows ORDER BY idx") == old_windows   # không embed lại
    assert env.fake.calls == calls                                       # không chuyển chữ lại


def test_doi_chu_thi_ca_hai_kho_duoc_dung_lai(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    old_uuid = env.graph.added[0]["uuid"]

    reprocess("transcribe", since=T0.date(), backend="fake2")
    assert rows("SELECT count(*) AS n FROM windows")[0]["n"] == 0
    drain()

    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
    assert all("[fake2]" in w["text"] for w in rows("SELECT text FROM windows"))
    assert env.graph.removed == [old_uuid]
    assert len(env.graph.live) == 1 and "[fake2]" in env.graph.live[0]["text"]

    # Chỉ embed lại: graph cũng phải nạp lại vì vẫn dựng từ cùng transcript, nhưng không chuyển chữ lại.
    calls = env.fake2.calls
    reprocess("index", since=T0.date())
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
    assert env.fake2.calls == calls and len(env.graph.live) == 1 and len(env.graph.removed) == 2


def test_episode_bi_xoa_thi_graph_cung_duoc_go(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    old_uuid = env.graph.added[0]["uuid"]

    reprocess("vad", since=T0.date())      # episode cũ bị xóa, sẽ được dựng lại từ đầu
    assert rows("SELECT episode_id FROM graph_parts") == [{"episode_id": None}]
    assert rows("SELECT count(*) AS n FROM windows")[0]["n"] == 0
    drain()

    assert env.graph.removed == [old_uuid]
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
    assert [str(p["uuid"]) for p in rows("SELECT uuid FROM graph_parts")] == [env.graph.live[0]["uuid"]]


def test_chua_go_duoc_ban_cu_thi_khong_nap_ban_moi(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    reprocess("ingest", since=T0.date())

    env.graph.unavailable = "FalkorDB giả đang tắt"
    drain()
    ep = rows("SELECT stage, attempts FROM episodes")[0]
    assert ep == {"stage": "indexed", "attempts": 0}
    assert len(env.graph.added) == 1 and env.graph.removed == []     # không có bản thứ hai trong graph

    env.graph.unavailable = None
    retry_failed()
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested" and len(env.graph.live) == 1


def test_graph_chi_nhan_episode_tu_mot_moc(env, audio_dir, monkeypatch):
    monkeypatch.setenv("GRAPH_SINCE", "2026-10-13")
    get_settings.cache_clear()
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)                       # ngày 12: cũ hơn mốc
    ingest_file(audio_dir / "b.m4a", started_at=T0 + timedelta(days=1), close_episode=True)   # ngày 13
    drain()

    eps = rows("SELECT stage, attempts, error FROM episodes ORDER BY started_at")
    assert eps == [{"stage": "indexed", "attempts": 0, "error": None}, {"stage": "ingested", "attempts": 0, "error": None}]
    assert len(env.graph.added) == 1
    assert rows("SELECT count(DISTINCT episode_id) AS n FROM windows")[0]["n"] == 2   # cả hai vẫn có vector nguyên văn

    # Bỏ mốc thì episode cũ được nạp nốt.
    monkeypatch.setenv("GRAPH_SINCE", "")
    get_settings.cache_clear()
    drain()
    assert [e["stage"] for e in rows("SELECT stage FROM episodes ORDER BY started_at")] == ["ingested", "ingested"]


def test_hai_tho_khong_nap_graph_cung_luc(env, audio_dir):
    from lifelog.db import connect as raw_connect
    from lifelog.pipeline import INGEST_LOCK

    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    with raw_connect(autocommit=True) as other:          # một thợ khác đang giữ quyền nạp
        other.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK,))
        drain()
        assert rows("SELECT stage FROM episodes")[0]["stage"] == "indexed" and env.graph.added == []
    drain()                                              # thợ kia xong thì thợ này nạp tiếp
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
