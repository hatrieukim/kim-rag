"""Đọc lịch sử phiên Claude Code trong ~/.claude/projects và đổi thành sự kiện.

Mỗi phiên là một file JSONL, mỗi dòng một việc. Chỉ giữ ba loại: lời người dùng gõ (`prompt`), lời
Claude trả lời (`reply`), và việc Claude làm (`tool`: tên file đã sửa, mô tả lệnh đã chạy).
Kết quả của tool không được giữ: dài, nhiễu, và có thể chứa khóa hay nội dung file .env.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .config import get_settings
from .db import connect
from .events import store_events

log = logging.getLogger("lifelog")

SOURCE = "claude-code"
MAX_PROMPT = 4000   # lời người dùng dài hơn thế thường là log hay dữ liệu dán vào
MAX_REPLY = 12000

# Dòng do chính Claude Code chèn vào, không phải lời người dùng.
_NOISE = ("<task-notification", "<local-command-", "<command-name", "<command-message", "<command-args",
          "[Request interrupted", "<bash-input", "<bash-stdout", "<bash-stderr", "<user-prompt-submit-hook")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TAG = re.compile(r"</?pasted_content[^>]*>")

_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
_ASSIGNED = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\s*[=:]\s*)(['\"]?)[^\s'\"]{8,}")

# Tool cho biết Claude đã làm gì. Các tool chỉ đọc (Read, Grep...) bị bỏ qua.
_FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def scrub(text: str) -> str:
    """Che các chuỗi trông giống khóa hay mật khẩu trước khi chữ rời khỏi máy."""
    for pattern in _SECRETS:
        text = pattern.sub("[đã che]", text)
    return _ASSIGNED.sub(lambda m: f"{m.group(1)}{m.group(2)}[đã che]", text)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit].rstrip()} … (cắt bớt {len(text) - limit} ký tự)"


def _clean(text: str, limit: int) -> str:
    text = _TAG.sub("", _REMINDER.sub("", text)).strip()
    if not text or text.startswith(_NOISE):
        return ""
    return _clip(scrub(text), limit)


def _norm(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def project_of(folder: str, projects: list[str]) -> str | None:
    """Thư mục phiên của Claude Code là đường dẫn dự án đã đổi mọi ký tự lạ thành dấu gạch.

    Khớp cả worktree của dự án (`<dự án>--claude-worktrees-<tên>`).
    """
    for p in projects:
        tail = "-" + _norm(p)
        if folder.endswith(tail) or f"{tail}--claude-worktrees-" in folder:
            return p
    return None


def _tool_event(block: dict) -> dict | None:
    name, args = block.get("name") or "", block.get("input") or {}
    if name in _FILE_TOOLS and args.get("file_path"):
        return {"tool": name, "file": str(args["file_path"])}
    if name == "Bash":
        what = args.get("description") or _clip(str(args.get("command") or ""), 200)
        return {"tool": name, "what": scrub(str(what))} if what else None
    if name in ("Agent", "Task") and args.get("description"):
        return {"tool": "Agent", "what": scrub(str(args["description"]))}
    if name == "WebSearch" and args.get("query"):
        return {"tool": name, "what": scrub(str(args["query"]))}
    if name == "WebFetch" and args.get("url"):
        return {"tool": name, "what": str(args["url"]).split("?")[0]}
    return None


def parse_line(obj: dict, project: str) -> list[dict]:
    """Một dòng JSONL thành không, một hoặc vài sự kiện."""
    kind = obj.get("type")
    if kind not in ("user", "assistant") or not obj.get("uuid") or not obj.get("timestamp"):
        return []
    # Bỏ lời của agent con, dòng nội bộ, và bản tóm tắt khi phiên bị nén ngữ cảnh.
    if obj.get("isSidechain") or obj.get("isMeta") or obj.get("isCompactSummary"):
        return []
    content = (obj.get("message") or {}).get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    base = {"session": obj.get("sessionId"), "project": project, "cwd": obj.get("cwd"), "branch": obj.get("gitBranch")}

    out = []
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        payload = None
        if block.get("type") == "text":
            text = _clean(block.get("text") or "", MAX_PROMPT if kind == "user" else MAX_REPLY)
            if text:
                etype, payload = ("prompt" if kind == "user" else "reply"), {"text": text}
        elif block.get("type") == "tool_use" and kind == "assistant":
            payload = _tool_event(block)
            etype = "tool"
        if payload:
            out.append({"uid": f"{SOURCE}:{obj['uuid']}:{i}", "source": SOURCE, "type": etype,
                        "ts": obj["timestamp"], "payload": {**base, **payload}})
    return out


_TITLE_KINDS = {"custom-title": ("customTitle", "custom_title"), "ai-title": ("aiTitle", "ai_title")}


def title_of(obj: dict) -> tuple[str, str, str] | None:
    """Dòng đặt tên phiên thành (mã phiên, cột, tên). Dòng khác trả về None."""
    kind = _TITLE_KINDS.get(obj.get("type"))
    if not kind or not obj.get("sessionId") or not str(obj.get(kind[0]) or "").strip():
        return None
    return obj["sessionId"], kind[1], scrub(str(obj[kind[0]]).strip())[:300]


def save_titles(conn, titles: dict[tuple[str, str], str], projects: dict[str, str]) -> int:
    """Ghi tên phiên rồi chép sang các episode của phiên đó. Trả về số episode được đổi tên."""
    changed = 0
    for (session, column), title in titles.items():
        conn.execute(
            f"INSERT INTO code_sessions (session, project, {column}) VALUES (%s, %s, %s) "
            f"ON CONFLICT (session) DO UPDATE SET {column} = EXCLUDED.{column}, updated_at = now()",
            (session, projects.get(session), title),
        )
    for session in {s for s, _ in titles}:
        done = conn.execute(
            """
            UPDATE episodes e SET meta = e.meta || jsonb_build_object('title', COALESCE(c.custom_title, c.ai_title))
            FROM code_sessions c
            WHERE c.session = %s AND e.kind = 'code' AND e.meta->>'session' = c.session
              AND e.meta->>'title' IS DISTINCT FROM COALESCE(c.custom_title, c.ai_title)
            """,
            (session,),
        )
        changed += done.rowcount
    return changed


def backfill_titles() -> int:
    """Đọc lại toàn bộ file phiên chỉ để lấy tên phiên. Dùng một lần cho lịch sử đã đọc trước khi có tên."""
    titles: dict[tuple[str, str], str] = {}
    projects: dict[str, str] = {}
    for path, project in session_files():
        with path.open("rb") as fh:
            for raw in fh:
                if b'-title"' not in raw:
                    continue
                try:
                    found = title_of(json.loads(raw))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if found:
                    titles[(found[0], found[1])] = found[2]
                    projects[found[0]] = project
    with connect() as conn:
        return save_titles(conn, titles, projects)


def session_files() -> list[tuple[Path, str]]:
    """Các file phiên thuộc những dự án được phép đọc, kèm tên dự án."""
    settings = get_settings()
    root = settings.claude_code_dir.expanduser()
    projects = settings.code_projects
    if not projects or not root.is_dir():
        return []
    found = []
    for folder in sorted(root.iterdir()):
        project = project_of(folder.name, projects) if folder.is_dir() else None
        if project:
            # Chỉ lấy file ngay trong thư mục: file của agent con nằm sâu hơn.
            found.extend((f, project) for f in sorted(folder.glob("*.jsonl")))
    return found


def scan() -> int:
    """Đọc phần mới của các file phiên. Trả về số sự kiện mới ghi vào sổ."""
    files = session_files()
    if not files:
        return 0
    with connect() as conn:
        seen = {r["path"]: r["read_bytes"] for r in conn.execute(
            "SELECT path, read_bytes FROM event_files WHERE source = %s", (SOURCE,))}
    total = 0
    for path, project in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = seen.get(str(path), 0)
        if size < start:
            start = 0  # file bị ghi lại từ đầu; sự kiện cũ vẫn bị chặn trùng theo uid
        if size == start:
            continue
        with path.open("rb") as fh:
            fh.seek(start)
            data = fh.read()
        # Dòng cuối có thể đang được ghi dở: chỉ đọc tới ký tự xuống dòng cuối cùng.
        end = data.rfind(b"\n") + 1
        events = []
        titles: dict[tuple[str, str], str] = {}
        for raw in data[:end].splitlines():
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            found = title_of(obj)
            if found:
                titles[(found[0], found[1])] = found[2]   # dòng sau đè dòng trước: tên mới nhất
            else:
                events.extend(parse_line(obj, project))
        with connect() as conn:
            new = store_events(conn, events)
            save_titles(conn, titles, {s: project for s, _ in titles})
            conn.execute(
                "INSERT INTO event_files (path, source, read_bytes) VALUES (%s, %s, %s) "
                "ON CONFLICT (path) DO UPDATE SET read_bytes = EXCLUDED.read_bytes, updated_at = now()",
                (str(path), SOURCE, start + end),
            )
        total += new
    if total:
        log.info("Claude Code: %d sự kiện mới", total)
    return total
