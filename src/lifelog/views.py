"""JSON for the main window of the Mac app (product 11).

Every route here reads what other modules already compute: the day journal, search, people, timeline,
the Zalo counts and `lifelog doctor`. Nothing here writes to the memory except through those modules.
"""
from __future__ import annotations

import time as clock
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from .config import get_settings
from .db import connect

WEEKDAYS = ("Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật")
# A day or week still running is written again by an LLM on every call, so keep it this long.
LIVE_SUMMARY_TTL_S = 15 * 60
MAX_TURN_AUDIO_S = 120

_live: dict[tuple[str, date], tuple[float, str]] = {}


def router(require_token) -> APIRouter:
    r = APIRouter(prefix="/app", dependencies=[Depends(require_token)])
    r.add_api_route("/day", day_view, methods=["GET"])
    r.add_api_route("/summary", summary_view, methods=["GET"])
    r.add_api_route("/episodes/{episode_id}", episode_view, methods=["GET"])
    r.add_api_route("/episodes/{episode_id}/turns/{idx}/audio", turn_audio, methods=["GET"])
    r.add_api_route("/zalo/{thread_id}", zalo_thread, methods=["GET"])
    r.add_api_route("/search", search_view, methods=["GET"])
    r.add_api_route("/timeline", timeline_view, methods=["GET"])
    r.add_api_route("/person", person_view, methods=["GET"])
    r.add_api_route("/status", status_view, methods=["GET"])
    return r


def _day(value: str | None) -> date:
    try:
        return date.fromisoformat(value) if value else datetime.now(get_settings().tz).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from exc


def _bounds(day: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(day, time.min, get_settings().tz)
    return lo, lo + timedelta(days=1)


def _title(ep: dict) -> str:
    meta = ep["meta"] or {}
    if ep["kind"] == "code":
        return meta.get("title") or "Phiên Claude Code"
    if ep["kind"] == "read":
        return meta.get("title") or meta.get("domain") or "Trang web"
    if ep["kind"] == "chat":
        return ("Nhóm " if meta.get("thread_kind") == "group" else "Zalo với ") + (meta.get("title") or "?")
    if meta.get("meeting"):
        return "Họp online qua " + ((meta.get("meeting") or {}).get("app") or "?")
    return "Ghi âm"


def _subtitle(ep: dict) -> str | None:
    meta = ep["meta"] or {}
    if ep["kind"] == "code":
        return meta.get("project")
    if ep["kind"] == "read":
        return meta.get("site") or meta.get("domain")
    if ep["kind"] == "chat":
        return f"Zalo, {meta.get('messages', 0)} tin"
    titles = meta.get("calendar_titles") or []
    return titles[0] if titles else None


def _card(ep: dict, speakers: list[str]) -> dict:
    return {
        "id": str(ep["id"]), "kind": "meeting" if (ep["meta"] or {}).get("meeting") else ep["kind"],
        "stage": ep["stage"], "started_at": ep["started_at"], "ended_at": ep["ended_at"],
        "speech_s": ep["speech_s"], "title": _title(ep), "subtitle": _subtitle(ep), "speakers": speakers,
        "summary": ep["summary"], "error": ep["error"],
    }


def _speakers(conn, ids: list) -> dict[str, list[str]]:
    """Speaker names per voice episode: the named person, else the role the LLM gave, else the label."""
    out: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT es.episode_id, COALESCE(p.name, es.role, es.label) AS name FROM episode_speakers es "
        "LEFT JOIN speakers p ON p.id = es.speaker_id WHERE es.episode_id = ANY(%s) ORDER BY es.speech_s DESC",
        (ids,)):
        names = out.setdefault(str(r["episode_id"]), [])
        if r["name"] not in names:
            names.append(r["name"])
    return out


def day_view(day: str | None = None) -> dict:
    """Everything recorded on one day, without calling an LLM: episodes, Mac time, calendar, iPhone, Zalo."""
    from . import calendar, zalo
    from .mac_log import today_summary
    from .recorder import MAC_SOURCE, day_report

    when = _day(day)
    lo, hi = _bounds(when)
    now = datetime.now(timezone.utc)
    if lo > now:
        raise HTTPException(status_code=400, detail=f"{when:%d/%m/%Y} has not come yet")
    with connect() as conn:
        eps = conn.execute(
            # Zalo chats have their own list below, built from the messages; not listed twice.
            "SELECT id, kind, stage, started_at, ended_at, speech_s, meta, summary, error FROM episodes "
            "WHERE started_at >= %s AND started_at < %s AND kind <> 'chat' ORDER BY started_at", (lo, hi)).fetchall()
        names = _speakers(conn, [e["id"] for e in eps if e["kind"] == "voice"])
        meetings = [{"title": m["title"], "starts_at": m["starts_at"], "ends_at": m["ends_at"],
                     "people": calendar.people(m["attendees"])} for m in calendar.meetings(conn, lo, hi)]
        chats = zalo.threads(conn, lo, hi)
    return {
        "day": when.isoformat(),
        "weekday": WEEKDAYS[when.weekday()],
        "episodes": [_card(e, names.get(str(e["id"]), [])) for e in eps],
        "mac": today_summary(now=min(hi - timedelta(microseconds=1), now), top=8),
        "calendar": meetings,
        "recorder": day_report(when).as_dict(),
        "room": day_report(when, MAC_SOURCE).as_dict(),
        "zalo": [{"id": t.id, "kind": t.kind, "name": t.name, "messages": t.messages, "mine": t.mine,
                  "mentions_me": t.mentions_me, "skipped": t.skipped, "last": t.last} for t in chats],
    }


def summary_view(day: str | None = None, period: str = "day", refresh: bool = False) -> dict:
    """The day journal or the week summary as markdown. Those still running are kept a while, since
    writing them calls an LLM."""
    from .journal import day_summary, week_start, week_summary

    if period not in ("day", "week"):
        raise HTTPException(status_code=400, detail="period must be day or week")
    when = _day(day)
    today = datetime.now(get_settings().tz).date()
    live = when == today if period == "day" else week_start(when) == week_start(today)
    key = (period, week_start(when) if period == "week" else when)
    if live and not refresh and key in _live and clock.time() - _live[key][0] < LIVE_SUMMARY_TTL_S:
        written, text = _live[key]
    else:
        try:
            text = week_summary(when) if period == "week" else day_summary(when)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        written = clock.time()
        if live:
            _live[key] = (written, text)
    return {"day": when.isoformat(), "period": period, "text": text, "live": live,
            "written_at": datetime.fromtimestamp(written, timezone.utc)}


def episode_view(episode_id: uuid.UUID) -> dict:
    """One episode with every turn, named speakers by name."""
    from .windows import transcript_rows

    with connect() as conn:
        ep = conn.execute(
            "SELECT id, kind, stage, started_at, ended_at, speech_s, meta, summary, error, asr_backend, "
            "timeline IS NOT NULL AS has_audio FROM episodes WHERE id = %s", (episode_id,)).fetchone()
        if ep is None:
            raise HTTPException(status_code=404, detail="no such episode")
        rows = transcript_rows(conn, ep)
        names = _speakers(conn, [ep["id"]]) if ep["kind"] == "voice" else {}
    meta = ep["meta"] or {}
    out = _card(ep, names.get(str(ep["id"]), []))
    out.update({
        "has_audio": bool(ep["has_audio"]) and ep["kind"] == "voice",
        "url": meta.get("url"), "project": meta.get("project"), "branch": meta.get("branch"),
        "files": meta.get("files") or [],
        "turns": [{"idx": r["idx"], "speaker": r["speaker"], "known": r["known"], "started_at": r["started_at"],
                   "length_s": r["length_s"], "text": r["text"]} for r in rows],
    })
    return out


def turn_audio(episode_id: uuid.UUID, idx: int) -> Response:
    """The audio of one turn as WAV, cut from the chunks it was recorded in."""
    from .audio import AudioError, wav_bytes
    from .pipeline import episode_audio_range

    with connect() as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id = %s AND kind = 'voice'", (episode_id,)).fetchone()
        seg = conn.execute("SELECT start_s, end_s FROM segments WHERE episode_id = %s AND idx = %s",
                           (episode_id, idx)).fetchone()
        if not ep or not seg or not ep["timeline"]:
            raise HTTPException(status_code=404, detail="no audio for this turn")
        try:
            samples = episode_audio_range(conn, ep, seg["start_s"],
                                          min(seg["end_s"], seg["start_s"] + MAX_TURN_AUDIO_S))
        except (AudioError, FileNotFoundError, TypeError) as exc:
            raise HTTPException(status_code=404, detail=f"audio is no longer available: {exc}") from exc
    return Response(wav_bytes(samples), media_type="audio/wav")


def zalo_thread(thread_id: str, day: str | None = None) -> dict:
    """The messages of one Zalo chat on one day."""
    from . import zalo

    when = _day(day)
    lo, hi = _bounds(when)
    with connect() as conn:
        known = zalo.names(conn).get(thread_id) or {}
        msgs = zalo.messages(conn, thread_id, lo, hi)
    return {"id": thread_id, "kind": known.get("kind"), "name": known.get("name"), "day": when.isoformat(),
            "messages": msgs}


def search_view(q: str, since: str | None = None, until: str | None = None, limit: int = 10) -> dict:
    from dataclasses import asdict

    from .search import search

    try:
        return asdict(search(q, since=since, until=until, limit=limit))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def timeline_view(topic: str, since: str | None = None, until: str | None = None) -> dict:
    from .timeline import timeline

    try:
        return {"text": timeline(topic, since=since, until=until)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def person_view(name: str) -> dict:
    from .people import profile

    try:
        return {"text": profile(name)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def status_view() -> dict:
    """`lifelog doctor` plus what the pipeline still has to do and what failed."""
    from .doctor import run

    settings = get_settings()
    report = run().as_dict()
    try:
        with connect() as conn:
            report["chunks"] = {r["stage"]: r["n"] for r in conn.execute(
                "SELECT stage, count(*) AS n FROM chunks GROUP BY 1")}
            report["episodes"] = {r["stage"]: r["n"] for r in conn.execute(
                "SELECT stage, count(*) AS n FROM episodes GROUP BY 1")}
            report["waiting_events"] = conn.execute(
                "SELECT count(*) AS n FROM events WHERE stage = 'received' AND source <> 'zalo'").fetchone()["n"]
            report["failed"] = conn.execute(
                "SELECT id, kind, started_at, attempts, error FROM episodes WHERE error IS NOT NULL "
                "AND attempts >= %s ORDER BY started_at DESC LIMIT 20", (settings.max_attempts,)).fetchall()
    except Exception as exc:  # the database being down is already a failed check above
        report["db_error"] = str(exc)
    return report
