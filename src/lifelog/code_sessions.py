"""Gom sự kiện của Claude Code thành episode loại 'code'.

Một episode là một quãng làm việc liền mạch trong một phiên: các lượt cách nhau quá `code_gap_s` thì
tách ra. Episode sinh ra đã có chữ sẵn, nên vào thẳng bước `transcribed` và đi tiếp qua bộ nhớ như
episode giọng nói.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from .claude_code import SOURCE
from .config import get_settings
from .db import connect
from .export import export_episode

log = logging.getLogger("lifelog")

CODE_LOCK = 7_316_002   # khóa để chỉ một thợ gom sự kiện tại một thời điểm
BACKEND = "claude-code"  # điền vào chỗ tên dịch vụ chuyển chữ: chữ của episode code có sẵn từ nguồn
ASSISTANT = "Claude"
MAX_TURN = 1000          # lượt dài hơn thế được cắt thành nhiều lượt nhỏ để embed cho sát nghĩa
MIN_CHARS = 200          # cụm ít chữ hơn thế (ví dụ chỉ một câu chào) không đáng thành episode
MAX_ACTIONS = 12


def split_text(text: str, limit: int = MAX_TURN) -> list[str]:
    """Cắt chữ dài thành các khúc không quá `limit`, ưu tiên cắt ở ranh giới đoạn rồi tới dòng, tới câu."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    for sep in ("\n\n", "\n", ". ", " "):
        pieces = [p for p in text.split(sep) if p.strip()]
        if len(pieces) > 1:
            break
    else:
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    out, current = [], ""
    for piece in pieces:
        candidate = f"{current}{sep}{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current.strip())
        current = ""
        if len(piece) > limit:
            out.extend(split_text(piece, limit))
        else:
            current = piece
    if current:
        out.append(current.strip())
    return out


# File nháp trong thư mục tạm không phải là file của dự án.
_TEMP = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")


def _project_file(path: str) -> bool:
    return not path.startswith(_TEMP)


def _short(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return re.sub(r"^/Users/[^/]+/", "~/", path)


def _actions_line(actions: list[dict], cwd: str | None) -> str:
    """Các việc Claude làm giữa hai câu trả lời, gói thành một dòng ngắn."""
    files, others = [], []
    for a in actions:
        if a.get("file"):
            if not _project_file(a["file"]):
                continue
            name = _short(a["file"], cwd)
            if name not in files:
                files.append(name)
        elif a.get("what") and a["what"] not in others:
            others.append(a["what"])
    parts = []
    if files:
        shown = files[:MAX_ACTIONS]
        parts.append("sửa file " + ", ".join(shown) + (f" và {len(files) - len(shown)} file khác" if len(files) > len(shown) else ""))
    if others:
        shown = others[:MAX_ACTIONS]
        parts.append("; ".join(shown) + (f"; và {len(others) - len(shown)} việc khác" if len(others) > len(shown) else ""))
    return f"(Việc đã làm: {'. '.join(parts)})" if parts else ""


def build_turns(events: list[dict], owner: str) -> list[dict]:
    """Sự kiện theo thứ tự thời gian thành các lượt nói: {speaker, text, ts}."""
    turns: list[dict] = []
    pending: list[dict] = []   # các việc Claude làm kể từ câu trả lời gần nhất
    cwd = None

    def flush() -> None:
        line = _actions_line([e["payload"] for e in pending], cwd)
        if line:
            turns.append({"speaker": ASSISTANT, "text": line, "ts": pending[0]["ts"]})
        pending.clear()

    for e in events:
        cwd = e["payload"].get("cwd") or cwd
        if e["type"] == "tool":
            pending.append(e)
            continue
        if pending:
            flush()
        speaker = owner if e["type"] == "prompt" else ASSISTANT
        turns.extend({"speaker": speaker, "text": piece, "ts": e["ts"]}
                     for piece in split_text(e["payload"].get("text") or ""))
    if pending:
        flush()
    return turns


def describe(ep: dict, tz) -> str:
    """Bản tóm tắt của một episode code kèm dòng đầu nói rõ dự án và thời gian."""
    meta = ep["meta"] or {}
    start, end = ep["started_at"].astimezone(tz), ep["ended_at"].astimezone(tz)
    title = f' "{meta["title"]}"' if meta.get("title") else ""
    lines = [f"Phiên làm việc với Claude Code{title}, dự án {meta.get('project') or '?'}, "
             f"ngày {start:%d/%m/%Y} từ {start:%H:%M} tới {end:%H:%M}.", "", (ep["summary"] or "").strip()]
    files = meta.get("files") or []
    if files:
        shown = files[:20]
        lines += ["", "File đã sửa: " + ", ".join(shown) + (f" và {len(files) - len(shown)} file khác" if len(files) > len(shown) else "")]
    return "\n".join(lines).strip()


def _clusters(events: list[dict], gap: timedelta) -> list[list[dict]]:
    out: list[list[dict]] = []
    for e in events:
        if out and e["ts"] - out[-1][-1]["ts"] <= gap:
            out[-1].append(e)
        else:
            out.append([e])
    return out


def _create_episode(conn, events: list[dict], owner: str) -> bool:
    ids = [e["id"] for e in events]
    turns = build_turns(events, owner)
    spoken = [t for t in turns if not t["text"].startswith("(Việc đã làm:")]
    if sum(len(t["text"]) for t in spoken) < MIN_CHARS or not any(e["type"] == "prompt" for e in events):
        conn.execute("UPDATE events SET stage = 'ignored' WHERE id = ANY(%s)", (ids,))
        return False

    first, last = events[0], events[-1]
    payloads = [e["payload"] for e in events]
    cwd = next((p["cwd"] for p in payloads if p.get("cwd")), None)
    files = sorted({_short(p["file"], cwd) for p in payloads if p.get("file") and _project_file(p["file"])})
    session = first["payload"].get("session")
    named = conn.execute(
        "SELECT COALESCE(custom_title, ai_title) AS title FROM code_sessions WHERE session = %s", (session,)
    ).fetchone()
    meta = {
        "project": first["payload"].get("project"),
        "session": session,
        "title": named["title"] if named else None,
        "cwd": cwd,
        "branch": next((p["branch"] for p in payloads if p.get("branch")), None),
        "files": files,
        "prompts": sum(1 for e in events if e["type"] == "prompt"),
        "actions": sum(1 for e in events if e["type"] == "tool"),
    }
    ep = conn.execute(
        "INSERT INTO episodes (kind, stage, started_at, ended_at, duration_s, asr_backend, meta) "
        "VALUES ('code', 'transcribed', %s, %s, %s, %s, %s) RETURNING id",
        (first["ts"], last["ts"], (last["ts"] - first["ts"]).total_seconds(), BACKEND, Jsonb(meta)),
    ).fetchone()
    for idx, t in enumerate(turns):
        offset = (t["ts"] - first["ts"]).total_seconds()
        seg = conn.execute(
            "INSERT INTO segments (episode_id, idx, speaker, start_s, end_s, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (ep["id"], idx, t["speaker"], offset, offset, t["ts"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO segment_texts (segment_id, backend, model, text) VALUES (%s, %s, %s, %s)",
            (seg["id"], BACKEND, BACKEND, t["text"]),
        )
    conn.execute("UPDATE events SET stage = 'grouped', episode_id = %s WHERE id = ANY(%s)", (ep["id"], ids))
    export_episode(conn, ep["id"])
    return True


def group_code() -> bool:
    """Gom các sự kiện Claude Code chưa vào episode nào.

    Một phiên có thể kéo dài nhiều ngày. Các quãng làm việc đã kết thúc được gom ngay; riêng quãng cuối
    chỉ được đóng khi đã im đủ lâu, vì có thể còn đang diễn ra.
    """
    settings = get_settings()
    gap = timedelta(seconds=settings.code_gap_s)
    now = datetime.now(timezone.utc)
    created = changed = 0
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (CODE_LOCK,)).fetchone()["ok"]:
            return False
        sessions = conn.execute(
            "SELECT DISTINCT payload->>'session' AS session FROM events WHERE source = %s AND stage = 'received'",
            (SOURCE,),
        ).fetchall()
        for row in sessions:
            events = conn.execute(
                "SELECT id, type, ts, payload FROM events WHERE source = %s AND stage = 'received' "
                "AND payload->>'session' IS NOT DISTINCT FROM %s ORDER BY ts, id",
                (SOURCE, row["session"]),
            ).fetchall()
            clusters = _clusters(events, gap)
            if now - clusters[-1][-1]["ts"] <= gap:
                clusters.pop()  # quãng cuối còn mới, chờ thêm
            for cluster in clusters:
                created += _create_episode(conn, cluster, settings.owner_name)
                changed += 1
    if created:
        log.info("Claude Code: gom thành %d episode mới", created)
    return changed > 0
