from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lifelog.db import connect
from lifelog.ingest import ingest_file
from lifelog.pipeline import drain
from lifelog.reprocess import reprocess, retry_failed

T0 = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)  # 09:00 giờ Việt Nam


def rows(sql, *params):
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def test_gui_trung_khong_tao_ban_doi(env, audio_dir, tmp_path):
    first = ingest_file(audio_dir / "a.m4a", started_at=T0)
    again = ingest_file(audio_dir / "a.m4a", started_at=T0)
    copy = tmp_path / "ten_khac.m4a"
    copy.write_bytes((audio_dir / "a.m4a").read_bytes())
    renamed = ingest_file(copy)

    assert not first.duplicate and again.duplicate and renamed.duplicate
    assert first.id == again.id == renamed.id
    assert rows("SELECT count(*) AS n FROM chunks")[0]["n"] == 1
    assert len([p for p in (env.data / "raw").rglob("*") if p.is_file()]) == 1
    # File được cất theo ngày giờ địa phương.
    assert first.path.startswith("raw/2026/10/12/090000_")


def test_ca_luong_tu_file_toi_markdown(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()

    ep = rows("SELECT * FROM episodes")
    assert len(ep) == 1 and ep[0]["stage"] == "ingested" and ep[0]["asr_backend"] == "fake"
    segs = rows("SELECT * FROM segments ORDER BY idx")
    assert segs and all(s["speaker"] == "SPEAKER_01" for s in segs)
    assert all(s["end_s"] - s["start_s"] <= 50 for s in segs)
    # Giờ thật của lượt nói đầu nằm sau giờ bắt đầu file khoảng một giây im lặng.
    assert timedelta(0) < segs[0]["started_at"] - T0 < timedelta(seconds=3)

    md = (env.data / ep[0]["export_path"]).read_text(encoding="utf-8")
    assert ep[0]["export_path"].startswith("episodes/2026-10-12/0900_")
    assert "SPEAKER_01" in md and "[fake] lượt 1" in md and "[09:00:0" in md
    assert env.fake.calls == len(segs)


def test_file_im_lang_khong_tao_episode(env, audio_dir):
    ingest_file(audio_dir / "silence.m4a", started_at=T0, close_episode=True)
    drain()
    assert rows("SELECT stage FROM chunks")[0]["stage"] == "no_speech"
    assert rows("SELECT count(*) AS n FROM episodes")[0]["n"] == 0
    assert env.fake.calls == 0


def test_ghep_episode_theo_khoang_im_lang(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0)
    ingest_file(audio_dir / "b.m4a", started_at=T0 + timedelta(minutes=3))   # cách dưới 10 phút
    ingest_file(audio_dir / "c.m4a", started_at=T0 + timedelta(minutes=40))  # cách trên 10 phút
    drain()

    eps = rows("SELECT e.id, e.stage, count(c.id) AS n FROM episodes e JOIN chunks c ON c.episode_id = e.id "
               "GROUP BY e.id ORDER BY e.started_at")
    assert [e["n"] for e in eps] == [2, 1]
    assert all(e["stage"] == "ingested" for e in eps)
    # Lượt nói thuộc file thứ hai mang giờ thật của file thứ hai.
    late = rows("SELECT max(started_at) AS t FROM segments WHERE episode_id = %s", eps[0]["id"])[0]["t"]
    assert late >= T0 + timedelta(minutes=3)


def test_chunk_toi_sai_thu_tu_van_vao_dung_episode(env, audio_dir):
    ingest_file(audio_dir / "b.m4a", started_at=T0 + timedelta(minutes=3))
    ingest_file(audio_dir / "a.m4a", started_at=T0)
    drain()
    eps = rows("SELECT timeline FROM episodes")
    assert len(eps) == 1
    starts = [item["started_at"] for item in eps[0]["timeline"]]
    assert starts == sorted(starts)


def test_doi_dich_vu_chi_chay_lai_buoc_chuyen_chu(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    before_chunk = rows("SELECT updated_at FROM chunks")[0]["updated_at"]
    before_segs = [s["id"] for s in rows("SELECT id FROM segments ORDER BY idx")]
    old_path = rows("SELECT export_path FROM episodes")[0]["export_path"]

    counts = reprocess("transcribed", since=T0.date(), backend="fake2")
    assert counts["episodes"] == 1
    drain()

    ep = rows("SELECT * FROM episodes")[0]
    assert ep["stage"] == "ingested" and ep["asr_backend"] == "fake2"
    assert rows("SELECT updated_at FROM chunks")[0]["updated_at"] == before_chunk       # không cắt im lặng lại
    assert [s["id"] for s in rows("SELECT id FROM segments ORDER BY idx")] == before_segs  # không tách người nói lại
    assert env.fake2.calls == len(before_segs)
    assert "[fake2]" in (env.data / old_path).read_text(encoding="utf-8")
    # Chữ của dịch vụ cũ vẫn còn để so sánh.
    assert rows("SELECT count(*) AS n FROM segment_texts WHERE backend = 'fake'")[0]["n"] == len(before_segs)


def test_chay_lai_tu_buoc_tach_nguoi_noi_va_tu_dau(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    drain()
    calls = env.fake.calls

    reprocess("diarize", since=T0.date())
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "grouped"
    assert rows("SELECT count(*) AS n FROM segments")[0]["n"] == 0
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
    assert env.fake.calls == calls * 2

    old_export = env.data / rows("SELECT export_path FROM episodes")[0]["export_path"]
    counts = reprocess("vad", since=T0.date())
    assert counts == {"episodes": 1, "chunks": 1}
    assert rows("SELECT stage, speech FROM chunks")[0] == {"stage": "received", "speech": None}
    assert not old_export.exists()
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"
    assert rows("SELECT count(*) AS n FROM chunks")[0]["n"] == 1


def test_loi_giua_chung_thi_lam_tiep_tu_cho_do(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0)
    ingest_file(audio_dir / "b.m4a", started_at=T0 + timedelta(seconds=30))
    env.fake.fail_first = 1
    drain()

    ep = rows("SELECT * FROM episodes")[0]
    total = rows("SELECT count(*) AS n FROM segments")[0]["n"]
    assert total >= 2
    assert ep["stage"] == "ingested"   # lần thử thứ hai trong cùng lượt drain đã xong
    # Lượt nói lỗi được gọi lại đúng một lần, các lượt đã xong không bị gọi lại.
    assert env.fake.calls == total + 1


def test_het_luot_thu_thi_dung_lai_va_retry_lam_tiep(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    env.fake.fail_first = 10_000
    drain()
    ep = rows("SELECT * FROM episodes")[0]
    assert ep["stage"] == "diarized" and ep["attempts"] == 3 and "lỗi mạng giả" in ep["error"]

    env.fake.fail_first = 0
    assert drain() == 0                   # đã hết lượt thử, thợ không tự làm nữa
    retry_failed()
    drain()
    assert rows("SELECT stage, error FROM episodes")[0] == {"stage": "ingested", "error": None}


def test_thieu_cau_hinh_thi_cho_chu_khong_tinh_la_hong(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    env.fake.unavailable = "thiếu khóa"
    drain()
    ep = rows("SELECT * FROM episodes")[0]
    assert ep["stage"] == "diarized" and ep["attempts"] == 0 and "thiếu khóa" in ep["error"]
    assert env.fake.calls == 0

    env.fake.unavailable = None
    drain()
    assert rows("SELECT stage, error FROM episodes")[0] == {"stage": "ingested", "error": None}


def test_inbox_nhan_file_va_chuyen_sang_processed(env, audio_dir):
    import os
    import shutil
    import time

    inbox = env.data / "inbox"
    inbox.mkdir(parents=True)
    for name in ("a.m4a", "not_audio.m4a"):
        shutil.copy(audio_dir / name, inbox / name)
        old = time.time() - 60
        os.utime(inbox / name, (old, old))
    drain()

    assert (inbox / "processed" / "a.m4a").exists()
    assert (inbox / "failed" / "not_audio.m4a").exists()
    assert rows("SELECT source FROM chunks")[0]["source"] == "inbox"
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "ingested"


def test_episode_dang_dien_ra_thi_chua_dong(env, audio_dir):
    """File vừa ghi xong: chưa im đủ 10 phút thì episode còn mở, chờ file kế tiếp."""
    now = datetime.now(timezone.utc)
    ingest_file(audio_dir / "a.m4a", started_at=now - timedelta(seconds=40))
    drain()
    assert rows("SELECT stage FROM episodes")[0]["stage"] == "open"
    assert env.fake.calls == 0

    # File kế tiếp báo đây là file cuối: episode đóng và được xử lý ngay.
    ingest_file(audio_dir / "b.m4a", started_at=now - timedelta(seconds=20), close_episode=True)
    drain()
    eps = rows("SELECT stage, jsonb_array_length(timeline) AS n FROM episodes")
    assert eps == [{"stage": "ingested", "n": 2}]


def test_ending_by_hand_after_a_pause_closes_the_conversation_now(env, audio_dir):
    """The user presses "End conversation" after a pause: the last chunk has no speech but still closes it."""
    now = datetime.now(timezone.utc)
    ingest_file(audio_dir / "a.m4a", started_at=now - timedelta(seconds=60))
    ingest_file(audio_dir / "silence.m4a", started_at=now - timedelta(seconds=30), close_episode=True)
    drain()
    assert rows("SELECT stage FROM chunks ORDER BY started_at")[1]["stage"] == "no_speech"
    assert [e["stage"] for e in rows("SELECT stage FROM episodes")] == ["ingested"]


def test_speech_after_ending_by_hand_starts_a_new_conversation(env, audio_dir):
    """Both chunks arrive together (the phone was offline); the second one must not reopen the first conversation."""
    now = datetime.now(timezone.utc)
    ingest_file(audio_dir / "a.m4a", started_at=now - timedelta(seconds=60), close_episode=True)
    ingest_file(audio_dir / "b.m4a", started_at=now - timedelta(seconds=30))
    drain()
    eps = rows("SELECT stage, jsonb_array_length(timeline) AS n FROM episodes ORDER BY started_at")
    assert [e["stage"] for e in eps] == ["ingested", "open"]
    assert eps[0]["n"] == 1


def test_worker_run_stops_taking_work_after_its_time(env, audio_dir):
    ingest_file(audio_dir / "a.m4a", started_at=T0, close_episode=True)
    assert drain(run_s=0) == 1   # one step, then the rest waits for the next run
    assert drain() > 0


def test_watchdog_ends_a_stuck_run():
    import threading

    from lifelog.pipeline import start_watchdog

    codes, fired = [], threading.Event()
    start_watchdog(0.05, exit=lambda code: (codes.append(code), fired.set()))
    assert fired.wait(5) and codes == [3]
