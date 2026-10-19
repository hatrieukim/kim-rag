"""Online meetings recorded by the Mac app (product 8).

While a meeting app (Teams, Zoom, Meet in Chrome…) uses the microphone, the Mac app records two channels and
uploads 5-minute chunks through `POST /chunks` with `source = "mac-meeting"`:

- channel 1 (left) is the microphone the meeting app uses, so it holds the user's voice;
- channel 2 (right) is the sound of the meeting app itself, so it holds everyone else.

Chunk metadata: `meeting_id` (the same for every chunk of one meeting), `app`, `bundle`, `mic`,
`channels` (["mic", "app"]), `end_reason`, and `close_episode` on the last chunk. The Mac app also sends
`meeting_start` and `meeting_end` events (source "mac", payload `meeting_id`, `app`, `mic`).

What the server does with them:

- Chunks of one meeting form one voice episode of their own (`episodes.meta.meeting`), never mixed with the
  iPhone recording of the same hour.
- The iPhone (or the Mac's room recording) keeps recording during the meeting. Its speech inside the time the Mac recording covers is cut out
  before grouping, so the meeting is transcribed once. Phone chunks wait while a meeting that overlaps them is
  still running or its Mac chunks are still arriving; if the Mac recording failed (no chunk, or no speech in it),
  the phone audio is kept.
- After diarization, the voice that is mostly on the microphone channel is the user's: it is linked to the
  speaker marked `is_me` without comparing voiceprints.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
from psycopg.types.json import Jsonb

from .audio import SAMPLE_RATE, AudioError, decode_channels
from .config import get_settings
from .vad import subtract_spans

SOURCE = "mac-meeting"
SETTLE_S = 600.0             # after a meeting ends, its last chunks may still be on their way
OPEN_MAX_S = 12 * 3600.0     # a meeting without an end event stops holding phone audio after this long
MIC_SHARE = 0.75             # a voice with this share of its energy on the mic channel is the user's
MIN_ME_S = 3.0               # and at least this much speech


def is_meeting(chunk: dict) -> bool:
    return chunk["source"] == SOURCE


def is_phone(chunk: dict) -> bool:
    """Only the all-day recording (the iPhone, or the Mac's own microphone while the iPhone is off) overlaps
    meetings; files added by hand are left alone."""
    from .recorder import ALL_DAY

    return chunk["source"] in ALL_DAY


def info(chunk: dict) -> dict:
    """What the episode keeps about the meeting a chunk belongs to."""
    meta = chunk["metadata"] or {}
    return {k: meta[k] for k in ("app", "bundle", "mic") if meta.get(k)} | {
        "id": str(meta.get("meeting_id") or chunk["client_id"] or chunk["id"])}


# ---------------------------------------------------------------- phone audio during a meeting

def phone_state(conn, chunk: dict, now: datetime) -> str:
    """Decide what to do with a phone chunk that has speech: 'hold', 'keep', 'trimmed' or 'empty'.

    'trimmed' and 'empty' have already updated the chunk's speech in the database.
    """
    start, end = chunk["started_at"], chunk["ended_at"]
    starts = conn.execute(
        "SELECT ts, payload->>'meeting_id' AS id FROM events WHERE source = 'mac' AND type = 'meeting_start' "
        "AND ts < %s AND ts > %s",
        (end, start - timedelta(seconds=OPEN_MAX_S)),
    ).fetchall()
    for m in starts:
        stop = conn.execute(
            "SELECT ts FROM events WHERE source = 'mac' AND type = 'meeting_end' AND payload->>'meeting_id' = %s "
            "ORDER BY ts DESC LIMIT 1", (m["id"],)).fetchone()
        if stop is None:
            if (now - m["ts"]).total_seconds() < OPEN_MAX_S:
                return "hold"
        elif stop["ts"] > start and (now - stop["ts"]).total_seconds() < SETTLE_S:
            return "hold"
    rows = conn.execute(
        "SELECT started_at, ended_at, stage FROM chunks WHERE source = %s AND stage <> 'no_speech' "
        "AND started_at < %s AND ended_at > %s ORDER BY started_at",
        (SOURCE, end, start),
    ).fetchall()
    if any(r["stage"] == "received" for r in rows):
        return "hold"   # the Mac chunk is not checked for speech yet
    if not rows:
        return "keep"
    cuts = [((r["started_at"] - start).total_seconds(), (r["ended_at"] - start).total_seconds()) for r in rows]
    speech = subtract_spans(chunk["speech"], cuts)
    if speech == chunk["speech"]:
        return "keep"
    meta = {"meeting_cut": [[round(a, 3), round(b, 3)] for a, b in cuts], "speech_before_meeting": chunk["speech"]}
    speech_s = round(sum(e - s for s, e in speech), 2)
    conn.execute(
        "UPDATE chunks SET speech = %s, speech_s = %s, metadata = metadata || %s, stage = %s, updated_at = now() "
        "WHERE id = %s",
        (Jsonb(speech), speech_s, Jsonb(meta), "vad" if speech else "no_speech", chunk["id"]),
    )
    chunk["speech"], chunk["speech_s"] = speech, speech_s
    return "trimmed" if speech else "empty"


# ---------------------------------------------------------------- whose voice is on the mic

def mic_label(conn, ep: dict, turns) -> str | None:
    """The diarized voice that comes from the microphone channel, or None if no voice clearly does."""
    data_dir = get_settings().data_dir
    energy: dict[str, list[float]] = {}
    spoken: dict[str, float] = {}
    for item in ep["timeline"]:
        path = conn.execute("SELECT path FROM chunks WHERE id = %s", (item["chunk_id"],)).fetchone()["path"]
        lo, hi = item["offset_s"], item["offset_s"] + item["duration_s"]
        inside = [t for t in turns if t.end > lo and t.start < hi]
        if not inside:
            continue
        try:
            stereo = decode_channels(data_dir / path, 2)
        except AudioError:
            continue
        for t in inside:
            a = int((max(t.start, lo) - lo) * SAMPLE_RATE)
            b = int((min(t.end, hi) - lo) * SAMPLE_RATE)
            part = stereo[a:b]
            if not len(part):
                continue
            e = energy.setdefault(t.speaker, [0.0, 0.0])
            e[0] += float(np.sum(part[:, 0] ** 2))
            e[1] += float(np.sum(part[:, 1] ** 2))
            spoken[t.speaker] = spoken.get(t.speaker, 0.0) + (b - a) / SAMPLE_RATE
    mine = [label for label, (mic, app) in energy.items()
            if mic + app > 0 and mic / (mic + app) >= MIC_SHARE and spoken[label] >= MIN_ME_S]
    # Someone sitting next to the user is also on the mic; the user is the one who talks most there.
    return max(mine, key=lambda label: spoken[label]) if mine else None


def label_me(conn, ep: dict, label: str | None) -> None:
    conn.execute("UPDATE episodes SET meta = jsonb_set(meta, '{meeting,mic_label}', %s) WHERE id = %s",
                 (Jsonb(label), ep["id"]))


def describe(meta: dict) -> str:
    """'Microsoft Teams' or 'Google Chrome' for headings and search results."""
    return ((meta or {}).get("meeting") or {}).get("app") or "?"
