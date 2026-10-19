"""Calendar events from the Mac app (source 'calendar').

The Mac app reads the macOS calendar (EventKit) for the last few days and the next week, and sends:
- event: one occurrence with title, time, location, calendar name and attendees. An edited event is sent again
  with the same `key` and replaces the old row.
- snapshot: the keys of every event currently in a time window. Rows in that window whose key is missing were
  deleted from the calendar and are marked cancelled.

Notes and meeting links are never sent. Attendees of a meeting overlapping a recording are suggested as the
people speaking in it (product 6), and the day journal lists the day's meetings.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from psycopg.types.json import Jsonb

from .config import get_settings
from .db import connect

log = logging.getLogger("lifelog")

SOURCE = "calendar"
CALENDAR_LOCK = 7_316_007
SKIP_STATUS = {"declined"}
MAX_MEETINGS = 30


def _ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_me(person: dict, mine: set[str]) -> bool:
    return bool(person.get("me")) or str(person.get("email") or "").strip().lower() in mine


def _mark_me(person: dict, mine: set[str]) -> dict:
    """EventKit only knows the calendar's own account; an invite sent to another of the user's emails is also me."""
    return {**person, "me": True} if _is_me(person, mine) else person


def _store(conn, p: dict) -> bool:
    start, end = _ts(p.get("start")), _ts(p.get("end"))
    if not p.get("key") or not start or not end:
        return False
    mine = get_settings().own_emails
    conn.execute(
        """
        INSERT INTO calendar_events (key, title, starts_at, ends_at, all_day, location, calendar, attendees, organizer)
        VALUES (%(key)s, %(title)s, %(start)s, %(end)s, %(all_day)s, %(location)s, %(calendar)s, %(attendees)s, %(organizer)s)
        ON CONFLICT (key) DO UPDATE SET title = EXCLUDED.title, starts_at = EXCLUDED.starts_at,
            ends_at = EXCLUDED.ends_at, all_day = EXCLUDED.all_day, location = EXCLUDED.location,
            calendar = EXCLUDED.calendar, attendees = EXCLUDED.attendees, organizer = EXCLUDED.organizer,
            cancelled = false, updated_at = now()
        """,
        {"key": p["key"], "title": p.get("title"), "start": start, "end": end, "all_day": bool(p.get("all_day")),
         "location": p.get("location"), "calendar": p.get("calendar"),
         "attendees": Jsonb([_mark_me(a, mine) for a in p.get("attendees") or [] if isinstance(a, dict)]),
         "organizer": Jsonb(_mark_me(p["organizer"], mine)) if isinstance(p.get("organizer"), dict) else None})
    return True


def group_calendar() -> bool:
    """Apply received calendar events and snapshots in the order they arrived."""
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (CALENDAR_LOCK,)).fetchone()["ok"]:
            return False
        rows = conn.execute("SELECT id, type, payload FROM events WHERE source = %s AND stage = 'received' ORDER BY id",
                            (SOURCE,)).fetchall()
        for r in rows:
            p = r["payload"] or {}
            ok = False
            if r["type"] == "event":
                ok = _store(conn, p)
            elif r["type"] == "snapshot" and _ts(p.get("from")) and _ts(p.get("to")):
                conn.execute(
                    "UPDATE calendar_events SET cancelled = true, updated_at = now() WHERE NOT cancelled "
                    "AND starts_at >= %s AND starts_at < %s AND key <> ALL(%s)",
                    (_ts(p["from"]), _ts(p["to"]), [str(k) for k in p.get("keys") or []]))
                ok = True
            conn.execute("UPDATE events SET stage = %s WHERE id = %s", ("grouped" if ok else "ignored", r["id"]))
    return bool(rows)


def people(attendees: list[dict]) -> list[str]:
    """Names of the other people invited, skipping the user and those who declined."""
    out: list[str] = []
    mine = get_settings().own_emails
    for a in attendees or []:
        if _is_me(a, mine) or a.get("status") in SKIP_STATUS:
            continue
        name = " ".join(str(a.get("name") or "").split()) or str(a.get("email") or "").split("@")[0]
        if name and name not in out:
            out.append(name)
    return out


def meetings(conn, lo: datetime, hi: datetime) -> list[dict]:
    """Timed (not all-day) events overlapping [lo, hi), earliest first."""
    return conn.execute(
        "SELECT key, title, starts_at, ends_at, location, attendees FROM calendar_events "
        "WHERE NOT cancelled AND NOT all_day AND starts_at < %s AND ends_at > %s ORDER BY starts_at",
        (hi, lo)).fetchall()


def context(conn, lo: datetime, hi: datetime, slack_s: float = 600) -> dict:
    """Meetings around a recording: their titles and the other people invited."""
    found = meetings(conn, lo - timedelta(seconds=slack_s), hi + timedelta(seconds=slack_s))
    names: list[str] = []
    for m in found:
        names += [n for n in people(m["attendees"]) if n not in names]
    return {"calendar_titles": [m["title"] for m in found if m["title"]], "calendar_people": names}


def render_day(conn, day: date) -> list[str]:
    """The "Lịch" section of the day journal."""
    tz = get_settings().tz
    lo = datetime.combine(day, time.min, tz)
    found = meetings(conn, lo, lo + timedelta(days=1))
    if not found:
        return []
    out = ["## Lịch", ""]
    for m in found[:MAX_MEETINGS]:
        who = people(m["attendees"])
        out.append(f"- {m['starts_at'].astimezone(tz):%H:%M}–{m['ends_at'].astimezone(tz):%H:%M} "
                   f"{m['title'] or '(riêng tư)'}" + (f" (với {', '.join(who[:8])})" if who else ""))
    if len(found) > MAX_MEETINGS:
        out.append(f"- và {len(found) - MAX_MEETINGS} sự kiện khác")
    out.append("")
    return out


def count_by_day(conn, tz: str) -> dict:
    return {r["day"]: r["n"] for r in conn.execute(
        "SELECT (starts_at AT TIME ZONE %s)::date AS day, count(*) AS n FROM calendar_events "
        "WHERE NOT cancelled AND NOT all_day GROUP BY 1", (tz,))}
