"""Chạy các lệnh người dùng gõ hằng ngày, với dịch vụ chuyển chữ giả."""
from __future__ import annotations

from typer.testing import CliRunner

from lifelog.cli import app
from lifelog.db import connect

runner = CliRunner()


def test_cac_lenh_hang_ngay(env, audio_dir):
    src = audio_dir / "a.m4a"

    first = runner.invoke(app, ["transcribe", str(src), "--start", "2026-10-12T09:00:00"])
    assert first.exit_code == 0, first.output
    assert "a.m4a: đã nhận" in first.output
    md_line = [ln for ln in first.output.splitlines() if ln.endswith(".md")][0]
    md_path = md_line.split(": ", 1)[1]
    assert "episodes/2026-10-12/0900_" in md_path
    assert "[fake] lượt 1" in open(md_path, encoding="utf-8").read()

    # Gửi lại cùng file: không nhận lại, không gọi dịch vụ lần nữa, vẫn chỉ ra kết quả cũ.
    calls = env.fake.calls
    again = runner.invoke(app, ["transcribe", str(src)])
    assert again.exit_code == 0 and "đã có từ trước" in again.output and md_path in again.output
    assert env.fake.calls == calls

    with connect() as conn:
        ep_id = str(conn.execute("SELECT id FROM episodes").fetchone()["id"])

    status = runner.invoke(app, ["status"])
    assert status.exit_code == 0 and "ingested 1" in status.output and ep_id[:8] in status.output

    shown = runner.invoke(app, ["show", ep_id[:8]])
    assert shown.exit_code == 0 and "SPEAKER_01" in shown.output

    cmp_ = runner.invoke(app, ["compare", ep_id[:8], "--backends", "fake,fake2"])
    assert cmp_.exit_code == 0, cmp_.output
    cmp_path = [ln for ln in cmp_.output.splitlines() if ln.endswith(".compare.md")][0]
    text = open(cmp_path, encoding="utf-8").read()
    assert "**fake:** [fake] lượt 1" in text and "**fake2:** [fake2] lượt 1" in text
    assert env.fake.calls == calls            # dịch vụ đã có chữ thì không gọi lại
    # So sánh không đổi dịch vụ chính của episode.
    with connect() as conn:
        assert conn.execute("SELECT asr_backend FROM episodes").fetchone()["asr_backend"] == "fake"

    redo = runner.invoke(app, ["reprocess", "--from", "transcribed", "--since", "2026-10-12", "--backend", "fake2"])
    assert redo.exit_code == 0, redo.output
    assert "[fake2]" in open(md_path, encoding="utf-8").read()

    assert runner.invoke(app, ["reprocess", "--from", "transcribed"]).exit_code != 0       # phải nói rõ phạm vi
    assert runner.invoke(app, ["reprocess", "--from", "khong-co", "--all"]).exit_code != 0


def test_bao_ro_khi_thieu_cau_hinh_va_khi_khong_phai_audio(env, audio_dir):
    bad = runner.invoke(app, ["transcribe", str(audio_dir / "not_audio.m4a")])
    assert bad.exit_code == 1 and "không phải audio" in bad.output

    env.fake.unavailable = "thiếu khóa giả"
    blocked = runner.invoke(app, ["transcribe", str(audio_dir / "b.m4a")])
    assert blocked.exit_code == 1 and "dừng ở bước 'diarized'" in blocked.output and "thiếu khóa giả" in blocked.output

    env.fake.unavailable = None
    retry = runner.invoke(app, ["retry"])
    assert retry.exit_code == 0
    with connect() as conn:
        assert conn.execute("SELECT stage FROM episodes").fetchone()["stage"] == "ingested"

    silent = runner.invoke(app, ["transcribe", str(audio_dir / "silence.m4a")])
    assert silent.exit_code == 0 and "không có tiếng nói" in silent.output
