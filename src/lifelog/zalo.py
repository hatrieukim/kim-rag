"""Zalo messages (product 10), first round: raw events only.

The sensor in zalo/ listens to Kim's Zalo account with zca-js and sends events with source 'zalo':
- message: one message. Direct chats and allowed groups carry the words (`text`), the sender and the
  thread; other groups carry only `skipped`, the type, the length and whether Kim was mentioned.
- undo: a message taken back by its sender (`msg_id`).
- contact: the current name of a person or group (`thread_id`, `thread_kind`, `name`).
- status: the sensor's state (connected, closed, needs_login, tick, stopped).

In this round nothing turns the events into episodes yet. They stay in stage 'received' and are only
counted, so that a week of real numbers decides which groups to keep, where to cut a chat into episodes
and what goes into Graphiti. Names come from the latest contact event rather than a table of their own:
the raw events are the record, everything else is computed from them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

SOURCE = "zalo"
TICK_EVERY = timedelta(minutes=5)


def names(conn) -> dict[str, dict]:
    """Latest known name of every person and group, by thread id."""
    rows = conn.execute(
        "SELECT DISTINCT ON (payload->>'thread_id') payload->>'thread_id' AS id, payload->>'thread_kind' AS kind, "
        "payload->>'name' AS name FROM events WHERE source = %s AND type = 'contact' "
        "ORDER BY payload->>'thread_id', ts DESC, id DESC", (SOURCE,)).fetchall()
    return {r["id"]: {"kind": r["kind"], "name": r["name"]} for r in rows}


@dataclass
class Day:
    day: date
    direct: int          # messages in direct chats
    groups: int          # messages kept in full from allowed groups
    counted: int         # messages from other groups, counted only
    chars: int           # characters of kept text
    mine: int            # kept messages Kim wrote
    undone: int


def daily(conn, since: datetime, tz: str) -> list[Day]:
    rows = conn.execute(
        """
        SELECT (ts AT TIME ZONE %(tz)s)::date AS day,
               count(*) FILTER (WHERE type = 'message' AND payload->>'thread_kind' = 'user') AS direct,
               count(*) FILTER (WHERE type = 'message' AND payload->>'thread_kind' = 'group'
                                AND NOT (payload ? 'skipped')) AS groups,
               count(*) FILTER (WHERE type = 'message' AND payload ? 'skipped') AS counted,
               COALESCE(sum(length(payload->>'text')) FILTER (WHERE type = 'message'), 0) AS chars,
               count(*) FILTER (WHERE type = 'message' AND NOT (payload ? 'skipped')
                                AND (payload->>'is_self')::boolean) AS mine,
               count(*) FILTER (WHERE type = 'undo') AS undone
        FROM events WHERE source = %(src)s AND type IN ('message', 'undo') AND ts >= %(since)s
        GROUP BY 1 ORDER BY 1
        """, {"tz": tz, "src": SOURCE, "since": since}).fetchall()
    return [Day(**r) for r in rows]


@dataclass
class Thread:
    id: str
    kind: str
    name: str | None
    messages: int
    chars: int
    mine: int
    mentions_me: int
    skipped: bool
    last: datetime


def threads(conn, since: datetime, until: datetime | None = None) -> list[Thread]:
    """Every chat with messages since `since` (and before `until`), busiest first."""
    known = names(conn)
    rows = conn.execute(
        """
        SELECT payload->>'thread_id' AS id, payload->>'thread_kind' AS kind, count(*) AS messages,
               COALESCE(sum(COALESCE(length(payload->>'text'), (payload->>'chars')::int)), 0) AS chars,
               count(*) FILTER (WHERE (payload->>'is_self')::boolean) AS mine,
               count(*) FILTER (WHERE (payload->>'mentions_me')::boolean) AS mentions_me,
               bool_or(payload ? 'skipped') AS skipped, max(ts) AS last
        FROM events WHERE source = %s AND type = 'message' AND ts >= %s AND (%s::timestamptz IS NULL OR ts < %s)
        GROUP BY 1, 2 ORDER BY 3 DESC, 1
        """, (SOURCE, since, until, until)).fetchall()
    return [Thread(**r, name=(known.get(r["id"]) or {}).get("name")) for r in rows]


def messages(conn, thread_id: str, since: datetime, until: datetime) -> list[dict]:
    """The messages of one chat between two moments, oldest first, without those taken back.
    Messages of groups that are only counted come without sender or words."""
    return conn.execute(
        """
        SELECT m.ts, m.payload->>'msg_id' AS msg_id, m.payload->>'from_name' AS from_name,
               COALESCE((m.payload->>'is_self')::boolean, false) AS is_self, m.payload->>'msg_type' AS msg_type,
               m.payload->>'text' AS text, m.payload->'quote'->>'text' AS quote, m.payload ? 'skipped' AS skipped
        FROM events m
        WHERE m.source = %(src)s AND m.type = 'message' AND m.payload->>'thread_id' = %(thread)s
          AND m.ts >= %(since)s AND m.ts < %(until)s
          AND NOT EXISTS (SELECT 1 FROM events u WHERE u.source = %(src)s AND u.type = 'undo'
                          AND u.payload->>'thread_id' = %(thread)s AND u.payload->>'msg_id' = m.payload->>'msg_id')
        ORDER BY m.ts, m.id
        """, {"src": SOURCE, "thread": thread_id, "since": since, "until": until}).fetchall()


@dataclass
class Health:
    state: str | None        # last state the sensor reported
    at: datetime | None      # when it reported it
    detail: dict
    last_message: datetime | None
    messages_24h: int


def health(conn) -> Health:
    last = conn.execute(
        "SELECT ts, payload FROM events WHERE source = %s AND type = 'status' ORDER BY ts DESC, id DESC LIMIT 1",
        (SOURCE,)).fetchone()
    msgs = conn.execute(
        "SELECT max(ts) AS last, count(*) FILTER (WHERE ts > now() - interval '1 day') AS n "
        "FROM events WHERE source = %s AND type = 'message'", (SOURCE,)).fetchone()
    payload = dict(last["payload"]) if last else {}
    return Health(state=payload.pop("state", None), at=last["ts"] if last else None, detail=payload,
                  last_message=msgs["last"], messages_24h=msgs["n"])


def verdict(h: Health, now: datetime, tz) -> tuple[bool | None, str]:
    """One line for `lifelog doctor`: (ok, message). ok is None when the sensor was never installed."""
    if h.state is None:
        return None, "no events yet; install with zalo/install.sh, then cd zalo && npm run login"
    at = h.at.astimezone(tz)
    if h.state == "needs_login":
        return False, "the Zalo session expired or was refused; run cd zalo && npm run login"
    if h.state == "closed" and h.detail.get("kicked"):
        return False, "Zalo opened in a browser took the session; close it, the sensor retries every 30 minutes"
    if h.state in ("closed", "stopped") or (h.state == "tick" and not h.detail.get("connected")):
        code = f" (code {h.detail['code']})" if h.detail.get("code") else ""
        return False, f"sensor not connected since {at:%d/%m %H:%M}{code}"
    if now - h.at > 3 * TICK_EVERY:
        return False, f"no word from the sensor since {at:%d/%m %H:%M}; is com.kim.kimrag.zalo running?"
    last = f", last message {h.last_message.astimezone(tz):%d/%m %H:%M}" if h.last_message else ""
    return True, f"listening, {h.messages_24h} messages in 24 hours{last}"
