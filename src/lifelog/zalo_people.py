"""Zalo people (product 10, round 3): who Kim chats with, which named voice each one is, and the Zalo part of
the `people` page.

A Zalo person is the other side of a direct chat; their Zalo user id is the thread id. Kim links a Zalo person to
a named voice by hand (`lifelog zalo-link`): names differ too much between the two to guess ("Hà" in the
recordings is "Thu Hà" on Zalo, and "Hiếu acme" could be "Lê Hiếu" or "Hiếu IT"). Only suggestions are
computed, from shared words in the names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from . import graph, zalo
from .config import get_settings
from .search import _span

SOURCE = "zalo"
RECENT_DAYS = 10
EXCERPT = 240
# Words that say how Kim addresses someone, not who they are. ZALO_NAME_TAGS adds more (e.g. workplace tags).
_TAGS = {"a", "c", "e", "anh", "chị", "chi", "em", "cô", "chú", "bác", "it"}


@dataclass
class Person:
    id: str
    name: str
    messages: int = 0
    mine: int = 0
    days: int = 0
    first: datetime | None = None
    last: datetime | None = None
    voice: str | None = None                       # name of the linked voice
    suggested: list[str] = field(default_factory=list)


def _core(name: str) -> set[str]:
    return {w for w in re.split(r"[\s.]+", (name or "").lower()) if w and w not in _TAGS | get_settings().name_tags}


def contacts(conn) -> list[Person]:
    """Everyone Kim has a direct chat with, busiest first, with the voice each is linked to."""
    known = zalo.names(conn)
    linked = {r["zalo_id"]: r["name"] for r in conn.execute(
        "SELECT l.zalo_id, s.name FROM zalo_links l JOIN speakers s ON s.id = l.speaker_id")}
    rows = conn.execute(
        """
        SELECT payload->>'thread_id' AS id, count(*) AS messages,
               count(*) FILTER (WHERE (payload->>'is_self')::boolean) AS mine,
               count(DISTINCT (ts AT TIME ZONE %s)::date) AS days, min(ts) AS first, max(ts) AS last,
               (array_agg(payload->>'from_name') FILTER (WHERE NOT (payload->>'is_self')::boolean
                                                         AND payload->>'from_name' <> ''))[1] AS sender
        FROM events WHERE source = %s AND type = 'message' AND payload->>'thread_kind' = 'user'
        GROUP BY 1 ORDER BY 2 DESC, 1
        """, (get_settings().timezone, SOURCE)).fetchall()
    out = []
    for r in rows:
        name = (known.get(r["id"]) or {}).get("name") or r["sender"]
        if name:
            out.append(Person(id=r["id"], name=name, messages=r["messages"], mine=r["mine"], days=r["days"],
                              first=r["first"], last=r["last"], voice=linked.get(r["id"])))
    return out


def suggest(conn, people: list[Person]) -> list[Person]:
    """Fills `suggested` for people not linked yet: named voices whose name shares its words with theirs."""
    voices = [r["name"] for r in conn.execute(
        "SELECT name FROM speakers WHERE status = 'named' AND NOT is_me ORDER BY name")]
    for p in people:
        if p.voice:
            continue
        mine = _core(p.name)
        p.suggested = [v for v in voices if mine and _core(v) and (_core(v) <= mine or mine <= _core(v))]
    return people


def resolve(conn, name: str) -> Person:
    """The Zalo person with this id or name: exact name, else the only one whose name has it as whole words."""
    people = contacts(conn)
    found = [p for p in people if p.id == name or p.name.lower() == name.lower()]
    if not found:
        found = [p for p in people if graph.whole_word(p.name, name)]
    if not found:
        raise ValueError(f'no Zalo person called "{name}"; `lifelog zalo-link` lists them')
    if len(found) > 1:
        raise ValueError(f'several Zalo people match "{name}": ' + ", ".join(f"{p.name} ({p.id})" for p in found))
    return found[0]


def link(conn, zalo_name: str, voice: str) -> tuple[Person, str]:
    """Links a Zalo person to a named voice. Returns them and the voice's exact name."""
    person = resolve(conn, zalo_name)
    rows = conn.execute("SELECT id, name FROM speakers WHERE status = 'named' AND lower(name) LIKE %s",
                        (f"%{voice.lower()}%",)).fetchall()
    found = [r for r in rows if r["name"].lower() == voice.lower()] or [
        r for r in rows if graph.whole_word(r["name"], voice)]
    if len(found) != 1:
        raise ValueError(f'several named voices match "{voice}": ' + ", ".join(r["name"] for r in found) if found
                         else f'no named voice called "{voice}"')
    speaker = found[0]
    conn.execute("INSERT INTO zalo_links (zalo_id, speaker_id) VALUES (%s, %s) "
                 "ON CONFLICT (zalo_id) DO UPDATE SET speaker_id = EXCLUDED.speaker_id, created_at = now()",
                 (person.id, speaker["id"]))
    return person, speaker["name"]


def unlink(conn, zalo_name: str) -> Person:
    person = resolve(conn, zalo_name)
    conn.execute("DELETE FROM zalo_links WHERE zalo_id = %s", (person.id,))
    return person


# ---------------------------------------------------------------- the people page

def find(conn, name: str, speaker: dict | None) -> tuple[list[Person], dict | None, list[str]]:
    """The Zalo people behind a name asked for in `people`.

    With a named voice: the Zalo people linked to it. Without one: the Zalo person with that name (exact, else
    whole words), and the voice they are linked to if any. Returns (people, voice, names when several match)."""
    people = contacts(conn)
    if speaker:
        ids = {r["zalo_id"] for r in conn.execute("SELECT zalo_id FROM zalo_links WHERE speaker_id = %s",
                                                  (speaker["id"],))}
        return [p for p in people if p.id in ids], speaker, []
    found = [p for p in people if p.name.lower() == name.lower()] or [
        p for p in people if graph.whole_word(p.name, name)]
    if len(found) > 1:
        return [], None, [p.name for p in found]
    if not found:
        return [], None, []
    row = conn.execute("SELECT s.id, s.name, s.is_me FROM zalo_links l JOIN speakers s ON s.id = l.speaker_id "
                       "WHERE l.zalo_id = %s AND s.status = 'named'", (found[0].id,)).fetchone()
    if row:
        return find(conn, name, row)
    return found, None, []


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT else text[:EXCERPT].rsplit(" ", 1)[0] + " …"


def section(conn, people: list[Person]) -> list[str]:
    """The "Zalo" part of a person's page: how much they chat with Kim, the groups they write in, the latest
    chat-days with what each was about."""
    from .windows import make_window, transcript_rows

    if not people:
        return []
    lines = ["", "## Zalo"]
    for p in people:
        lines.append(f'Chat riêng với "{p.name}": {p.messages} tin trong {p.days} ngày ({p.mine} tin Kim gửi), '
                     f"lần đầu {_span(p.first)}, gần nhất {_span(p.last)}.")
    ids = [p.id for p in people]
    known = zalo.names(conn)
    groups = conn.execute(
        "SELECT payload->>'thread_id' AS id, count(*) AS n FROM events WHERE source = %s AND type = 'message' "
        "AND payload->>'thread_kind' = 'group' AND payload->>'from_id' = ANY(%s) GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
        (SOURCE, ids)).fetchall()
    if groups:
        lines.append("Viết trong các nhóm: " + ", ".join(
            f"{(known.get(g['id']) or {}).get('name') or '?'} ({g['n']} tin)" for g in groups) + ".")
    eps = conn.execute(
        "SELECT id, started_at, ended_at, meta, summary, asr_backend FROM episodes WHERE kind = 'chat' "
        "AND meta->>'thread_kind' = 'user' AND meta->>'thread_id' = ANY(%s) ORDER BY started_at DESC LIMIT %s",
        (ids, RECENT_DAYS)).fetchall()
    if eps:
        lines += ["", "## Các ngày chat gần nhất"]
    for ep in eps:
        meta = ep["meta"] or {}
        lines.append(f"- {_span(ep['started_at'], ep['ended_at'])} · {meta.get('messages', 0)} tin · "
                     f"episode {str(ep['id'])[:8]}")
        if ep["summary"]:
            lines.append(f"  {_excerpt(ep['summary'])}")
        else:
            rows = transcript_rows(conn, ep)
            if rows:
                lines.append(f"  Trích: {_excerpt(make_window(rows).text)}")
    return lines
