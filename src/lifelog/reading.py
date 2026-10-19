"""Trang đã đọc trên Chrome (Sản phẩm 7).

Extension trong chrome/ gửi về ba loại sự kiện (source 'chrome'):
- visit: đã ở một trang bao lâu (`url`, `title`, `domain`, `seconds`), gửi theo từng phút đọc
- read: phần chữ chính của trang đã ở quá 30 giây (`url`, `title`, `domain`, `site`, `text`...)
- selection: đoạn chữ đã bôi đen rồi copy trên trang

Mỗi sự kiện read thành một episode loại 'read': chữ của bài chia thành các đoạn, rồi đi qua bước
`indexed` như phiên Claude Code để `search` tìm lại được. Bài đọc không vào Graphiti và không được LLM
tóm tắt: chỉ tốn tiền embed.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta

from psycopg.types.json import Jsonb

from .code_sessions import split_text
from .config import get_settings
from .db import connect
from .export import export_episode

log = logging.getLogger("lifelog")

SOURCE = "chrome"
BACKEND = "chrome"      # điền vào chỗ tên dịch vụ chuyển chữ: chữ của bài có sẵn từ trang
READ_LOCK = 7_316_005
MAX_TURN = 1000
MIN_CHARS = 300
MAX_LABEL = 80


def chunks(text: str, limit: int = MAX_TURN) -> list[str]:
    """Gộp các đoạn văn liền nhau thành khúc không quá `limit` ký tự. Đoạn dài hơn thế được cắt nhỏ."""
    out, current = [], ""
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        for piece in split_text(para, limit):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    out.append(current)
                current = piece
    if current:
        out.append(current)
    return out


def clean_title(title: str | None) -> str | None:
    """Tiêu đề gọn một dòng, bỏ số thông báo chưa đọc ở đầu. Extension cũng làm vậy; làm lại cho dữ liệu cũ."""
    t = re.sub(r"^\(\d+\+?\)\s+", "", " ".join((title or "").split()))
    return (t[:199].rstrip() + "…" if len(t) > 200 else t) or None


def _label(title: str, domain: str) -> str:
    """Nhãn đứng đầu mỗi đoạn trong chỉ mục: tên bài, để đoạn nào cũng mang theo chủ đề của bài."""
    title = " ".join((title or "").split())
    if len(title) > MAX_LABEL:
        title = title[:MAX_LABEL - 1].rstrip() + "…"
    return title or domain or "Trang web"


def _create_episode(conn, ev: dict) -> bool:
    p = ev["payload"] or {}
    parts = chunks(p.get("text") or "")
    if sum(len(c) for c in parts) < MIN_CHARS:
        conn.execute("UPDATE events SET stage = 'ignored' WHERE id = %s", (ev["id"],))
        return False
    meta = {k: p.get(k) for k in ("url", "domain", "site", "byline", "lang")}
    meta["title"] = clean_title(p.get("title"))
    meta["chars"] = sum(len(c) for c in parts)
    ep = conn.execute(
        "INSERT INTO episodes (kind, stage, started_at, ended_at, duration_s, asr_backend, meta) "
        "VALUES ('read', 'transcribed', %s, %s, 0, %s, %s) RETURNING id",
        (ev["ts"], ev["ts"], BACKEND, Jsonb(meta)),
    ).fetchone()
    speaker = _label(meta["title"], p.get("domain"))
    for idx, text in enumerate(parts):
        seg = conn.execute(
            "INSERT INTO segments (episode_id, idx, speaker, start_s, end_s, started_at) "
            "VALUES (%s, %s, %s, 0, 0, %s) RETURNING id",
            (ep["id"], idx, speaker, ev["ts"]),
        ).fetchone()
        conn.execute("INSERT INTO segment_texts (segment_id, backend, model, text) VALUES (%s, %s, %s, %s)",
                     (seg["id"], BACKEND, BACKEND, text))
    conn.execute("UPDATE events SET stage = 'grouped', episode_id = %s WHERE id = %s", (ep["id"], ev["id"]))
    export_episode(conn, ep["id"])
    return True


def group_reading() -> bool:
    """Bài đọc mới thành episode. Lượt ở trang và đoạn copy chỉ cần đánh dấu đã xem."""
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (READ_LOCK,)).fetchone()["ok"]:
            return False
        reads = conn.execute(
            "SELECT id, ts, payload FROM events WHERE source = %s AND stage = 'received' AND type = 'read' "
            "ORDER BY ts, id", (SOURCE,)).fetchall()
        created = sum(_create_episode(conn, ev) for ev in reads)
        marked = conn.execute(
            "UPDATE events SET stage = 'grouped' WHERE source = %s AND stage = 'received' AND type <> 'read'",
            (SOURCE,)).rowcount
    if created:
        log.info("Chrome: %d bài đọc mới", created)
    return bool(reads or marked)


# ---------------------------------------------------------------- mục nhật ký

MIN_VISIT_S = 60    # trang ở ít hơn thế mà không đọc thì không đưa vào nhật ký
MAX_PAGES = 25


def _duration(seconds: float) -> str:
    from .mac_log import duration

    return duration(seconds)


def render_day(conn, day: date) -> list[str]:
    """Mục "Đã đọc" của nhật ký ngày: những trang đã ở lâu hoặc đã lấy được chữ, theo giờ."""
    tz = get_settings().tz
    lo = datetime.combine(day, time.min, tz)
    hi = lo + timedelta(days=1)
    visits = conn.execute(
        "SELECT ts, payload FROM events WHERE source = %s AND type = 'visit' AND ts >= %s AND ts < %s ORDER BY ts",
        (SOURCE, lo, hi)).fetchall()
    reads = conn.execute(
        "SELECT started_at AS ts, meta FROM episodes WHERE kind = 'read' AND started_at >= %s AND started_at < %s ORDER BY started_at",
        (lo, hi)).fetchall()
    pages: dict[str, dict] = {}
    for v in visits:
        p = v["payload"]
        page = pages.setdefault(p.get("url") or "?", {"ts": v["ts"], "title": clean_title(p.get("title")),
                                                      "domain": p.get("domain"), "seconds": 0.0, "read": False})
        page["seconds"] += p.get("seconds") or 0
        page["title"] = page["title"] or clean_title(p.get("title"))
    for r in reads:
        m = r["meta"] or {}
        page = pages.setdefault(m.get("url") or "?", {"ts": r["ts"], "title": m.get("title"), "domain": m.get("domain"),
                                                      "seconds": 0.0, "read": True})
        page["read"] = True
        page["title"] = m.get("title") or page["title"]
    shown = sorted((p for p in pages.values() if p["read"] or p["seconds"] >= MIN_VISIT_S), key=lambda p: p["ts"])
    if not shown:
        return []
    out = ["## Đã đọc", ""]
    for p in shown[:MAX_PAGES]:
        note = [p["domain"]] if p["domain"] else []
        if p["seconds"] >= 60:
            note.append(_duration(p["seconds"]))
        out.append(f"- {p['ts'].astimezone(tz):%H:%M} {p['title'] or '(không có tiêu đề)'}"
                   + (f" ({', '.join(note)})" if note else ""))
    if len(shown) > MAX_PAGES:
        out.append(f"- và {len(shown) - MAX_PAGES} trang khác")
    out.append("")
    return out
