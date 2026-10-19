"""Everything known about one person, for the `people` tool (product 9).

Several sources each know a little: the voices named by the user (when and with whom that person was
recorded), the calendar (meetings they were invited to), the Mac log (chats with them), Zalo (direct chats with
them, joined to their voice when Kim linked the two) and the graph (what was said about them). This module puts
them on one page.
"""
from __future__ import annotations

from datetime import datetime

from . import graph, journal, zalo_people
from .config import get_settings
from .db import connect
from .search import Fact, _span, render_fact, with_sources
from .windows import make_window, transcript_rows

RECENT_TALKS = 10
MAX_FACTS = 15
EXCERPT = 240


def _find_speaker(conn, name: str) -> tuple[dict | None, list[str]]:
    """The named speaker called `name`, or else the only one whose name contains it as whole words.
    Several matches return no speaker and their names."""
    rows = conn.execute(
        "SELECT id, name, is_me FROM speakers WHERE status = 'named' AND lower(name) LIKE %s ORDER BY name",
        (f"%{name.lower()}%",)).fetchall()
    exact = [r for r in rows if r["name"].lower() == name.lower()]
    if exact:
        return exact[0], []
    words = [r for r in rows if graph.whole_word(r["name"], name)]
    if len(words) == 1:
        return words[0], []
    return None, [r["name"] for r in words]


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT else text[:EXCERPT].rsplit(" ", 1)[0] + " …"


def _voice(conn, speaker: dict) -> list[str]:
    """How often this voice was heard, with whom, and the latest conversations."""
    stats = conn.execute(
        "SELECT count(DISTINCT es.episode_id) AS n, COALESCE(sum(es.speech_s), 0) AS speech_s, "
        "min(e.started_at) AS first, max(e.started_at) AS last "
        "FROM episode_speakers es JOIN episodes e ON e.id = es.episode_id WHERE es.speaker_id = %s",
        (speaker["id"],)).fetchone()
    if not stats["n"]:
        return ["Chưa nghe thấy giọng người này trong bản ghi nào."]
    lines = [f"Giọng đã được nhận ra trong {stats['n']} bản ghi, tổng khoảng {stats['speech_s'] / 60:.0f} phút nói; "
             f"lần đầu {_span(stats['first'])}, gần nhất {_span(stats['last'])}."]
    together = conn.execute(
        """
        SELECT p.name, count(DISTINCT b.episode_id) AS n
        FROM episode_speakers a
        JOIN episode_speakers b ON b.episode_id = a.episode_id AND b.label <> a.label
        JOIN speakers p ON p.id = b.speaker_id
        WHERE a.speaker_id = %(id)s AND p.status = 'named' AND p.id <> %(id)s
        GROUP BY p.name ORDER BY n DESC, p.name LIMIT 8
        """, {"id": speaker["id"]}).fetchall()
    if together:
        lines.append("Hay có mặt cùng: " + ", ".join(f"{r['name']} ({r['n']} bản ghi)" for r in together) + ".")

    eps = conn.execute(
        """
        SELECT e.id, e.started_at, e.ended_at, e.meta, e.summary, e.asr_backend
        FROM episodes e JOIN episode_speakers es ON es.episode_id = e.id
        WHERE es.speaker_id = %s AND e.stage IN ('transcribed', 'indexed', 'ingested')
        ORDER BY e.started_at DESC LIMIT %s
        """, (speaker["id"], RECENT_TALKS)).fetchall()
    if eps:
        lines += ["", "## Các lần nói chuyện gần nhất"]
    for ep in eps:
        rows = transcript_rows(conn, ep)
        window = make_window(rows) if rows else None
        others = [s for s in (window.speakers if window else []) if s != speaker["name"]]
        lines.append(f"- {_span(ep['started_at'], ep['ended_at'])} · {journal._origin(ep)} · "
                     f"episode {str(ep['id'])[:8]}" + (f" · cùng: {', '.join(others)}" if others else ""))
        if ep["summary"]:
            lines.append(f"  {_excerpt(ep['summary'])}")
        elif window:
            lines.append(f"  Trích: {_excerpt(window.text)}")
    return lines


def _calendar(conn, name: str) -> list[str]:
    now = datetime.now(get_settings().tz)
    rows = conn.execute(
        """
        SELECT title, starts_at, ends_at FROM calendar_events
        WHERE NOT cancelled AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(attendees) a
            WHERE lower(a->>'name') LIKE %(q)s OR lower(a->>'email') LIKE %(q)s)
        ORDER BY starts_at DESC LIMIT 10
        """, {"q": f"%{name.lower()}%"}).fetchall()
    if not rows:
        return []
    lines = ["", "## Lịch có mời người này"]
    for r in rows:
        soon = " (sắp tới)" if r["starts_at"] > now else ""
        lines.append(f"- {_span(r['starts_at'], r['ends_at'])} · {r['title'] or '(riêng tư)'}{soon}")
    return lines


def _chats(conn, name: str) -> list[str]:
    rows = conn.execute(
        "SELECT day, app, sum(seconds) AS seconds FROM mac_spans WHERE kind = 'chat' AND lower(context) LIKE %s "
        "GROUP BY day, app ORDER BY day DESC, seconds DESC LIMIT 10",
        (f"%{name.lower()}%",)).fetchall()
    if not rows:
        return []
    return ["", "## Chat trên Mac"] + [
        f"- {r['day']:%d/%m/%Y}: {r['app']} khoảng {max(1, round(r['seconds'] / 60))} phút" for r in rows]


def _graph(names: list[str]) -> tuple[list[str], list[str]]:
    """Lines about this person from the graph, and notes when it cannot be asked. A person can be in the graph
    under several names: their voice's name and their names on Zalo."""
    if get_settings().graph == "none":
        return [], ["Kho fact đang tắt (GRAPH=none), không có điều đã biết và fact."]
    try:
        graph.check()
        found = [graph.person(n, MAX_FACTS * 2) for n in names]
    except Exception as exc:
        return [], [f"Kho fact không hỏi được: {exc}."]
    name = names[0]
    nodes = list({n["name"]: n for f in found for n in f["nodes"]}.values())
    seen = {(f["fact"], f["valid_at"]): f for x in found for f in x["facts"]}
    newest = sorted(seen.values(), key=lambda f: str(f.get("valid_at") or f.get("created_at") or ""), reverse=True)
    lines: list[str] = []
    notes = [n for n in nodes if n["summary"].strip()]
    if notes:
        lines += ["", "## Điều đã biết"]
        for n in notes:
            text = " ".join(n["summary"].split())
            lines.append(text if len(notes) == 1 and n["name"].lower() == name.lower() else f"- {n['name']}: {text}")
    facts: list[Fact] = with_sources(newest)[:MAX_FACTS]
    if facts:
        lines += ["", f"## Fact: {len(facts)}, mới nhất trước"]
        for f in facts:
            lines += render_fact(f)
    return lines, []


def profile(name: str) -> str:
    """One page about a person, for an agent to read."""
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("tên đang trống")
    with connect() as conn:
        speaker, candidates = _find_speaker(conn, name)
        on_zalo: list[zalo_people.Person] = []
        if not candidates:
            on_zalo, speaker, candidates = zalo_people.find(conn, name, speaker)
        if candidates:
            return (f'Có {len(candidates)} người khớp với "{name}": {", ".join(candidates)}. '
                    "Gọi lại với một tên đầy đủ trong số đó.")
        shown = speaker["name"] if speaker else name
        voice = _voice(conn, speaker) if speaker else []
        # The calendar and the Mac log would list every meeting and chat for the user themselves.
        mine = bool(speaker and speaker["is_me"])
        cal = [] if mine else _calendar(conn, shown)
        chats = [] if mine else _chats(conn, shown)
        zalo = [] if mine else zalo_people.section(conn, on_zalo)
        known = [r["name"] for r in conn.execute(
            "SELECT name FROM speakers WHERE status = 'named' ORDER BY last_seen_at DESC NULLS LAST LIMIT 20")]
    facts, notes = _graph(list(dict.fromkeys([shown] + [p.name for p in on_zalo])))

    if not (speaker or cal or chats or zalo or facts):
        lines = [f'Không tìm thấy ai tên "{name}" trong bản ghi, lịch, chat, Zalo hay fact.']
        if known:
            lines.append("Những người đã biết tên giọng nói: " + ", ".join(known) + ".")
        return "\n".join(lines + [f"Lưu ý: {n}" for n in notes])

    aka = [p.name for p in on_zalo if p.name != shown]
    lines = [f"# {shown}" + (" (chính là người dùng)" if mine else "")
             + (f" (trên Zalo: {', '.join(aka)})" if aka else ""), ""]
    lines += [f"Lưu ý: {n}" for n in notes] + ([""] if notes else [])
    if speaker:
        lines += voice
    else:
        lines.append("Chưa có giọng nói nào được đặt tên này; thông tin dưới đây từ lịch, chat, Zalo hoặc fact.")
    return "\n".join(lines + cal + chats + zalo + facts).strip() + "\n"
