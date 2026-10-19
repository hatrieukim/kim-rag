"""Checks that the machine and the config are ready, shared by `lifelog doctor` and the Mac app window.

Each check is a line: good (True), missing (False) or only a note (None), grouped by section. A check that
fails but does not stop the pipeline (`required=False`, e.g. a sensor that is down) does not fail `doctor`.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Check:
    section: str
    good: bool | None
    label: str
    detail: str = ""
    required: bool = True


@dataclass
class Report:
    checks: list[Check]
    notes: dict[str, list[str]]   # plain lines printed under a section heading
    data_dir: str

    @property
    def ok(self) -> bool:
        return all(c.good is not False for c in self.checks if c.required)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "data_dir": self.data_dir, "notes": self.notes,
                "checks": [c.__dict__ for c in self.checks]}


def run() -> Report:
    import psycopg

    from .asr import AsrUnavailable, backend_names, get_backend
    from .config import get_settings
    from .db import connect

    s = get_settings()
    checks: list[Check] = []
    notes: dict[str, list[str]] = {}
    section = ""

    def line(good: bool | None, label: str, detail: str = "", required: bool = True) -> None:
        checks.append(Check(section, good, label, detail, required))

    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        line(bool(found), tool, found or "cài bằng `brew install ffmpeg`")
    try:
        with connect() as conn:
            version = conn.execute("SHOW server_version").fetchone()["server_version"]
            tables = conn.execute("SELECT to_regclass('chunks') IS NOT NULL AS ok").fetchone()["ok"]
        line(True, "Postgres", version)
        line(bool(tables), "Bảng trong sổ", "" if tables else "chạy `lifelog init-db`")
    except psycopg.OperationalError as exc:
        line(False, "Postgres", str(exc).strip().splitlines()[0] + ". Chạy `docker compose up -d`")

    section = f"Tách người nói: {s.diarizer}"
    if s.diarizer == "pyannote":
        line(bool(s.hf_token), "HF_TOKEN", f"model {s.pyannote_model}" if s.hf_token else
             f"tạo token và bấm đồng ý điều khoản tại huggingface.co/{s.pyannote_model}")
    else:
        line(None, "Không tách người nói", "mọi lượt nói sẽ mang nhãn SPEAKER_01")
    try:
        with connect() as conn:
            me = conn.execute("SELECT name FROM speakers WHERE is_me AND status = 'named'").fetchone()
        line(True if me else None, "Your voice", f"enrolled as {me['name']}" if me else
             "not enrolled; run `lifelog speakers enroll FILE --me` with 30+ seconds of you talking")
    except psycopg.Error:
        pass

    section = f"Chuyển chữ, mặc định: {s.asr_backend}"
    for name in backend_names():
        try:
            get_backend(name).check()
            line(True, name)
        except AsrUnavailable as exc:
            line(False if name == s.asr_backend else None, name, str(exc))
    if s.google_cloud_project:
        notes[section] = [f"Google Cloud project: {s.google_cloud_project} "
                          f"(Gemini ở {s.gemini_location}, Chirp ở {s.chirp_location})"]

    section = "Bộ nhớ"
    try:
        from . import embed

        embed.check()
        line(True, "Vector nguyên văn", f"{s.embed_model}, {embed.EMBED_DIM} chiều, lưu trong Postgres")
    except embed.EmbedUnavailable as exc:
        line(False, "Vector nguyên văn", str(exc))
    if s.graph == "none":
        line(None, "Graphiti", "đang tắt (GRAPH=none), episode dừng ở bước indexed")
    else:
        from . import graph

        try:
            graph.check()
            line(True, "Graphiti", f"FalkorDB ở {s.falkordb_host}:{s.falkordb_port}, rút fact bằng {s.graph_llm_model}")
        except graph.GraphUnavailable as exc:
            line(False, "Graphiti", str(exc))

    section = "Nhật ký làm việc"
    from .claude_code import session_files

    if not s.code_projects:
        line(None, "Claude Code", "chưa chọn dự án nào (CLAUDE_CODE_PROJECTS trong .env)")
    else:
        files = session_files()
        found = {project for _, project in files}
        missing = [p for p in s.code_projects if p not in found]
        line(bool(files), "Claude Code", f"{len(files)} phiên ở {len(found)}/{len(s.code_projects)} dự án được chọn")
        if missing:
            line(None, "Không thấy phiên nào của", ", ".join(missing))

    section = "Hoạt động trên Mac"
    try:
        with connect() as conn:
            last = conn.execute("SELECT max(ts) AS ts, count(*) FILTER (WHERE ts > now() - interval '1 day') AS n "
                                "FROM events WHERE source = 'mac'").fetchone()
        if last["ts"]:
            line(True, "Chương trình ghi trên Mac", f"sự kiện gần nhất lúc {last['ts'].astimezone(s.tz):%d/%m %H:%M}, "
                 f"{last['n']} sự kiện trong 24 giờ qua")
        else:
            line(None, "Chương trình ghi trên Mac", "chưa nhận sự kiện nào; cài bằng mac/install.sh")
    except psycopg.OperationalError:
        pass
    try:
        with connect() as conn:
            web = conn.execute("SELECT max(ts) AS ts, count(*) FILTER (WHERE type = 'read') AS n "
                               "FROM events WHERE source = 'chrome'").fetchone()
        if web["ts"]:
            line(True, "Extension Chrome", f"sự kiện gần nhất lúc {web['ts'].astimezone(s.tz):%d/%m %H:%M}, "
                 f"{web['n']} bài đã đọc")
        else:
            line(None, "Extension Chrome", "chưa nhận sự kiện nào; cài thư mục chrome/ ở chrome://extensions")
    except psycopg.OperationalError:
        pass
    try:
        with connect() as conn:
            cal = conn.execute("SELECT max(received_at) AS ts, (SELECT count(*) FROM calendar_events WHERE NOT cancelled) AS n "
                               "FROM events WHERE source = 'calendar'").fetchone()
        line(True if cal["ts"] else None, "Calendar",
             f"{cal['n']} events, last sync {cal['ts'].astimezone(s.tz):%d/%m %H:%M}" if cal["ts"] else
             "nothing received; the Mac app asks for Calendar access on start")
    except psycopg.OperationalError:
        pass
    try:
        from . import zalo

        with connect() as conn:
            good, detail = zalo.verdict(zalo.health(conn), datetime.now(s.tz), s.tz)
        line(good, "Zalo", detail, required=False)
    except psycopg.OperationalError:
        pass
    line(None if not s.git_since_at else True, "Commit",
         f"đọc từ {s.git_since_at:%d/%m/%Y}" if s.git_since_at else "chưa đọc (đặt GIT_SINCE trong .env)")

    return Report(checks, notes, str(s.data_dir.resolve()))
