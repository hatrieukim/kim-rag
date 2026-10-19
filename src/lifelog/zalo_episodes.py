"""Zalo messages become episodes (product 10, round 2).

One chat on one day is one episode of kind 'chat': chats are bursts with no natural pause to cut at (85% of
gaps are under 30 minutes, the rest run to hours or days), and people remember chats by day ("on Tuesday I
messaged Nam"). Each message is one segment whose speaker is the sender's real name, so the episode then
goes through indexing like a Claude Code session: windows of a few messages, embedded for `search`.

Round 3: direct chats with enough words also go into Graphiti, so that what people promise, ask and decide in
chat becomes facts about them. Groups do not: they are many voices about many things, and stay in search and
the journal. In the graph the other person is called by the name of the voice Kim linked them to (`zalo_links`),
so a chat and a recording meet on one person.

Rules:
- Only days that are over are grouped; today's messages wait until tomorrow.
- Messages of groups Kim did not allow (`skipped`) are only counted and never become episodes.
- A message its sender took back (an `undo` event) is left out. When an undo or a late message lands on a day
  already grouped, that chat-day is built again.
- A chat-day with nothing but stickers, photos or other labels is ignored; a short line of text is kept,
  because "see you at 9 tomorrow" is a promise.
- A message sent again (same sender and words within a minute, or a second copy Zalo keeps of the same
  client message) is kept once: about 4% of messages are such repeats.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta

from psycopg.types.json import Jsonb

from . import zalo
from .config import get_settings
from .db import connect
from .export import export_episode

log = logging.getLogger("lifelog")

SOURCE = "zalo"
KIND = "chat"
BACKEND = "zalo"            # stands in for the speech-to-text service: the words come from Zalo itself
LOCK = 7_316_010
BATCH = 200                 # chat-days built per call, so the worker loop stays responsive
MIN_REAL_CHARS = 5          # less real text than this (only stickers, photos, "ok") is not worth an episode
REPEAT_S = 60               # the same words from the same person again within this is a re-send, not news
QUOTE_CHARS = 100
# A direct chat-day with less real text than this stays out of Graphiti, whose cost is per part, not per word.
# Measured on three months of chats: keeps 93% of the words for 63% of the calls.
GRAPH_MIN_CHARS = 200
# Which episodes the ingest step takes, as one SQL condition on `episodes`.
GRAPH_FILTER = ("(kind <> 'chat' OR (meta->>'thread_kind' = 'user' "
                f"AND COALESCE((meta->>'chars')::int, 0) >= {GRAPH_MIN_CHARS}))")
GRAPH_INSTRUCTIONS = """\
Đây là tin nhắn Zalo trong một ngày giữa Kim (người dùng) và một người khác, chép nguyên văn.
- Tên người gửi đã đúng, không phải nghe nhầm. "Kim" là người dùng.
- Nhãn trong ngoặc vuông như [photo], [sticker], [file: tên], [link: tiêu đề], [call, 3 min] thay cho nội dung
  không phải chữ. "[reply to X: …]" là đoạn trích tin đang được trả lời. Đừng tạo thực thể từ nhãn.
- Ưu tiên những điều còn dùng được về sau: ai hứa gì, nhờ gì, hẹn lúc nào, quyết định gì, con số, địa điểm,
  quan hệ giữa người với người. Bỏ qua chào hỏi, cảm ơn, sticker.
"""
# Labels normalize.js puts in place of content that is not text.
_LABEL = re.compile(r"\[(?:photo|sticker|voice message|video|gif|drawing|location|missed call|call[^\]]*|"
                    r"file[^\]]*|link[^\]]*|contact card|poll|card|task|other|message|zalo\.[^\]]*)\]")


def real_chars(text: str) -> int:
    """Characters of a message that are not a label."""
    return len(_LABEL.sub("", text or "").strip())


def _day_bounds(day: date, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def _own_id(conn) -> str | None:
    row = conn.execute(
        "SELECT payload->>'from_id' AS id FROM events WHERE source = %s AND type = 'message' "
        "AND (payload->>'is_self')::boolean AND payload->>'from_id' <> '' LIMIT 1", (SOURCE,)).fetchone()
    return row["id"] if row else None


def _turn_text(p: dict) -> str:
    text = (p.get("text") or "").strip()
    q = p.get("quote") or {}
    if q.get("text"):
        quoted = q["text"].strip().replace("\n", " ")
        quoted = quoted if len(quoted) <= QUOTE_CHARS else quoted[:QUOTE_CHARS].rstrip() + "…"
        text = f"[reply to {q.get('from_name') or '?'}: {quoted}] {text}"
    return text


def _build(conn, thread_id: str, day: date, names: dict, own_id: str | None) -> bool:
    """Builds the episode of one chat on one day from its raw events. Returns True when one was made."""
    settings = get_settings()
    lo, hi = _day_bounds(day, settings.tz)
    events = conn.execute(
        "SELECT id, ts, payload FROM events WHERE source = %s AND type = 'message' AND payload->>'thread_id' = %s "
        "AND ts >= %s AND ts < %s ORDER BY ts, id", (SOURCE, thread_id, lo, hi)).fetchall()
    undone = {r["m"] for r in conn.execute(
        "SELECT payload->>'msg_id' AS m FROM events WHERE source = %s AND type = 'undo' AND payload->>'thread_id' = %s",
        (SOURCE, thread_id))}
    kept: list[dict] = []
    seen_cli: set[str] = set()
    for e in events:
        p = e["payload"]
        if p.get("skipped") or p.get("msg_id") in undone:
            continue
        cli = p.get("cli_msg_id")
        prev = kept[-1] if kept else None
        if (cli and cli in seen_cli) or (
                prev and prev["payload"].get("from_id") == p.get("from_id") and prev["payload"].get("text") == p.get("text")
                and (e["ts"] - prev["ts"]).total_seconds() <= REPEAT_S):
            continue
        if cli:
            seen_cli.add(cli)
        kept.append(e)
    left = [e["id"] for e in events if e not in kept]
    if left:
        conn.execute("UPDATE events SET stage = 'ignored', episode_id = NULL WHERE id = ANY(%s)", (left,))
    if not kept or sum(real_chars(e["payload"].get("text")) for e in kept) < MIN_REAL_CHARS:
        if kept:
            conn.execute("UPDATE events SET stage = 'ignored', episode_id = NULL WHERE id = ANY(%s)",
                         ([e["id"] for e in kept],))
        return False

    first, last = kept[0], kept[-1]
    kind = first["payload"].get("thread_kind") or "user"

    def speaker(p: dict) -> str:
        if p.get("is_self"):
            return settings.owner_name
        return p.get("from_name") or (names.get(p.get("from_id")) or {}).get("name") or "?"

    others = [speaker(e["payload"]) for e in kept if not e["payload"].get("is_self")]
    name = (names.get(thread_id) or {}).get("name") or (others[0] if kind == "user" and others else None)
    meta = {
        "thread_id": thread_id,
        "thread_kind": kind,
        "title": name,
        "day": day.isoformat(),
        "messages": len(kept),
        "from_kim": sum(1 for e in kept if e["payload"].get("is_self")),
        "mentions_kim": sum(1 for e in kept if own_id and own_id in (e["payload"].get("mentions") or [])),
        "participants": sorted({speaker(e["payload"]) for e in kept}),
        "chars": sum(real_chars(e["payload"].get("text")) for e in kept),
        "from_backup": sum(1 for e in kept if e["payload"].get("origin") == "backup"),
    }
    ep = conn.execute(
        "INSERT INTO episodes (kind, stage, started_at, ended_at, duration_s, asr_backend, meta) "
        "VALUES (%s, 'transcribed', %s, %s, %s, %s, %s) RETURNING id",
        (KIND, first["ts"], last["ts"], (last["ts"] - first["ts"]).total_seconds(), BACKEND, Jsonb(meta)),
    ).fetchone()
    for idx, e in enumerate(kept):
        offset = (e["ts"] - first["ts"]).total_seconds()
        seg = conn.execute(
            "INSERT INTO segments (episode_id, idx, speaker, start_s, end_s, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (ep["id"], idx, speaker(e["payload"]), offset, offset, e["ts"]),
        ).fetchone()
        conn.execute("INSERT INTO segment_texts (segment_id, backend, model, text) VALUES (%s, %s, %s, %s)",
                     (seg["id"], BACKEND, BACKEND, _turn_text(e["payload"])))
    conn.execute("UPDATE events SET stage = 'grouped', episode_id = %s WHERE id = ANY(%s)",
                 (ep["id"], [e["id"] for e in kept]))
    export_episode(conn, ep["id"])
    return True


def _drop_episode(conn, thread_id: str, day: date) -> None:
    """Removes the episode of a chat-day so it can be built again; its events go back to waiting."""
    for ep in conn.execute(
            "SELECT id FROM episodes WHERE kind = %s AND meta->>'thread_id' = %s AND meta->>'day' = %s",
            (KIND, thread_id, day.isoformat())).fetchall():
        conn.execute("UPDATE events SET stage = 'received', episode_id = NULL WHERE episode_id = %s", (ep["id"],))
        conn.execute("DELETE FROM episodes WHERE id = %s", (ep["id"],))


def group_zalo() -> bool:
    """Turns the Zalo events of past days into chat episodes. Returns True when something changed."""
    settings = get_settings()
    tz = settings.tz
    today = datetime.now(tz).date()
    start_today, _ = _day_bounds(today, tz)
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (LOCK,)).fetchone()["ok"]:
            return False
        # The sensor's own state and the names of chats are not part of any day.
        housekeeping = conn.execute(
            "UPDATE events SET stage = 'ignored' WHERE source = %s AND stage = 'received' "
            "AND type NOT IN ('message', 'undo')", (SOURCE,)).rowcount

        # A message taken back after its day was grouped: that chat-day is built again.
        days: set[tuple[str, date]] = set()
        for u in conn.execute(
                "SELECT u.id, m.payload->>'thread_id' AS thread, m.ts FROM events u "
                "LEFT JOIN events m ON m.source = u.source AND m.type = 'message' AND m.stage = 'grouped' "
                " AND m.payload->>'thread_id' = u.payload->>'thread_id' AND m.payload->>'msg_id' = u.payload->>'msg_id' "
                "WHERE u.source = %s AND u.type = 'undo' AND u.stage = 'received'", (SOURCE,)).fetchall():
            if u["thread"]:
                days.add((u["thread"], u["ts"].astimezone(tz).date()))
        conn.execute("UPDATE events SET stage = 'grouped' WHERE source = %s AND type = 'undo' AND stage = 'received'",
                     (SOURCE,))

        for r in conn.execute(
                "SELECT DISTINCT payload->>'thread_id' AS thread, (ts AT TIME ZONE %s)::date AS day FROM events "
                "WHERE source = %s AND type = 'message' AND stage = 'received' AND ts < %s",
                (settings.timezone, SOURCE, start_today)).fetchall():
            days.add((r["thread"], r["day"]))
        if not days and not housekeeping:
            return False

        names = zalo.names(conn)
        own_id = _own_id(conn)
        todo = sorted(days, key=lambda x: (x[1], x[0]))[:BATCH]
        made = 0
        for thread_id, day in todo:
            _drop_episode(conn, thread_id, day)
            made += _build(conn, thread_id, day, names, own_id)
    if todo:
        log.info("Zalo: %d chat-days grouped, %d episodes, %d left", len(todo), made, max(0, len(days) - len(todo)))
    return True


def rebuild(since: date | None = None) -> int:
    """Drops chat episodes (from `since` on, or all) and sends their messages back to be grouped again,
    after a change of rules. Returns how many episodes were dropped."""
    settings = get_settings()
    lo = _day_bounds(since, settings.tz)[0] if since else None
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM episodes WHERE kind = %s AND (%s::timestamptz IS NULL OR started_at >= %s)",
            (KIND, lo, lo)).fetchall()]
        conn.execute("UPDATE events SET stage = 'received', episode_id = NULL WHERE source = %s "
                     "AND type IN ('message', 'undo') AND (%s::timestamptz IS NULL OR ts >= %s)", (SOURCE, lo, lo))
        conn.execute("DELETE FROM episodes WHERE id = ANY(%s)", (ids,))
    return len(ids)


# ---------------------------------------------------------------- graph and people

def linked_names(conn) -> dict[str, str]:
    """Zalo user id -> the name of the voice Kim linked it to."""
    return {r["zalo_id"]: r["name"] for r in conn.execute(
        "SELECT l.zalo_id, s.name FROM zalo_links l JOIN speakers s ON s.id = l.speaker_id "
        "WHERE s.status = 'named'")}


def graph_input(conn, ep: dict):
    """The parts of a direct chat-day for Graphiti, and their description."""
    from .windows import build_windows, transcript_rows

    settings = get_settings()
    meta = ep["meta"] or {}
    thread = str(meta.get("thread_id") or "")
    title = meta.get("title") if meta.get("title") not in (None, "?") else None
    other = (linked_names(conn).get(thread) or title
             or f"người chưa rõ tên trên Zalo (chat {thread[-6:]})")
    rows = [{**r, "speaker": r["speaker"] if r["speaker"] == settings.owner_name else other}
            for r in transcript_rows(conn, ep)]
    day = ep["started_at"].astimezone(settings.tz)
    description = (f"Tin nhắn Zalo giữa {settings.owner_name} và {other} ngày {day:%d/%m/%Y}"
                   + (f' (tên trên Zalo: "{title}")' if title and title != other else "") + ", chép nguyên văn.")
    return build_windows(rows, max_chars=settings.graph_part_chars), description


# ---------------------------------------------------------------- day journal

def describe(ep: dict) -> str:
    """'chat với Anh Nam' or 'nhóm Kế toán', as the journal and search name a chat."""
    meta = ep["meta"] or {}
    return ("nhóm " if meta.get("thread_kind") == "group" else "chat với ") + (meta.get("title") or "?")


def _matters(meta: dict) -> bool:
    """Chats that belong in Kim's journal: direct chats, and groups where Kim spoke or was named."""
    return meta.get("thread_kind") == "user" or bool(meta.get("from_kim")) or bool(meta.get("mentions_kim"))


def render_day(conn, day: date) -> list[str]:
    """The "Zalo" section of a day journal: what each chat that matters was about, then the other groups
    by name. Long chats are summarised once and the summary is kept on the episode."""
    from . import summarize
    from .journal import SUMMARIZED
    from .windows import make_window, transcript_rows

    settings = get_settings()
    lo, hi = _day_bounds(day, settings.tz)
    eps = conn.execute(
        "SELECT id, started_at, ended_at, meta, summary, asr_backend FROM episodes WHERE kind = %s "
        "AND stage = ANY(%s) AND started_at >= %s AND started_at < %s ORDER BY started_at",
        (KIND, list(SUMMARIZED), lo, hi)).fetchall()
    out: list[str] = []
    others: list[str] = []
    for ep in eps:
        meta = ep["meta"] or {}
        if not _matters(meta):
            others.append(f"{meta.get('title') or '?'} ({meta.get('messages', 0)} tin)")
            continue
        rows = transcript_rows(conn, ep)
        if not rows:
            continue
        window = make_window(rows)
        what = f"chat Zalo, {describe(ep)}"
        if len(window.text) <= summarize.SHORT_CONVERSATION:
            body = "\n".join(f"> {line}" for line in window.text.splitlines())
        else:
            if not ep["summary"]:
                summarize.check()
                ep["summary"] = summarize.summarize_conversation(window.text, what)
                conn.execute("UPDATE episodes SET summary = %s, summary_model = %s WHERE id = %s",
                             (ep["summary"], summarize.model_name(), ep["id"]))
            body = ep["summary"].strip()
        span = f"{ep['started_at'].astimezone(settings.tz):%H:%M}–{ep['ended_at'].astimezone(settings.tz):%H:%M}"
        out += [f"### {span} · {describe(ep)} · {meta.get('messages', 0)} tin · episode `{str(ep['id'])[:8]}`",
                "", body, ""]
    if others:
        out += ["Nhóm khác: " + ", ".join(others) + ".", ""]
    return ["## Zalo", ""] + out if out else []
