"""Sound the Mac plays out loud, and the iPhone speech it overlaps.

A video or music on the Mac reaches the iPhone microphone and would be transcribed as if someone in the room
said it. The Mac app knows what it plays, so it reports it, and the server leaves the iPhone speech of those
minutes out, the same way it leaves out sound from the iPhone's own speaker (`pipeline.step_vad`). The same goes
for the Mac's own room recording (`recorder.MAC_SOURCE`), which hears its speakers even better.

The Mac app sends a `sound` event (source "mac") whenever the set of apps playing out loud changes, again
every minute as a heartbeat, and an "off" one before the Mac sleeps or the app quits. Payload: `on`, `apps`
(names, left out while recording is paused), `output` (the device name). An app counts when Core Audio says
it is playing and it is not using the microphone (a call is a meeting, product 8), and the device it plays on
is not muted, not at zero volume and not headphones.

Every overlapping second is left out, even when the user talked over the sound. The speech before the cut is
kept in the chunk metadata (`speech_before_mac_sound`), with the cut spans (`mac_sound_cut`, seconds from the
chunk start) and the apps (`mac_sound_apps`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from .vad import subtract_spans

SOURCE = "mac"
TYPE = "sound"
STALE_S = 150.0      # a state lasts this long after its last event when the Mac goes quiet (heartbeat: 60 s)
JOIN_S = 5.0         # sounds this close together are one (the gap between two songs)
MIN_S = 8.0          # a shorter sound (a notification) is not cut
PAD_S = 3.0          # the two devices' clocks and the Mac's 2-second poll
WAIT_S = 180.0       # a phone chunk waits this long after its end for the Mac to report those minutes
RECENT_S = 3600.0    # only wait for a Mac that reported sound within this time before the chunk
LOOKBACK_S = 600.0   # read events from this long before the chunk, to know what was already playing


@dataclass
class Sound:
    start: datetime
    end: datetime
    apps: set[str] = field(default_factory=set)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


def spans(events: list[dict]) -> list[Sound]:
    """Times the Mac played out loud, from `sound` events sorted by time."""
    out: list[Sound] = []
    for i, ev in enumerate(events):
        payload = ev["payload"] or {}
        if not payload.get("on"):
            continue
        end = ev["ts"] + timedelta(seconds=STALE_S)
        if i + 1 < len(events):
            end = min(end, events[i + 1]["ts"])
        apps = set(payload.get("apps") or [])
        if out and (ev["ts"] - out[-1].end).total_seconds() <= JOIN_S:
            out[-1].end = max(out[-1].end, end)
            out[-1].apps |= apps
        else:
            out.append(Sound(ev["ts"], end, apps))
    return [s for s in out if s.seconds >= MIN_S]


def phone_state(conn, chunk: dict, now: datetime) -> str:
    """Decide what to do with a phone chunk that has speech: 'hold', 'keep', 'trimmed' or 'empty'.

    'trimmed' and 'empty' have already updated the chunk's speech in the database.
    """
    start, end = chunk["started_at"], chunk["ended_at"]
    events = conn.execute(
        "SELECT ts, payload FROM events WHERE source = %s AND type = %s AND ts >= %s AND ts < %s ORDER BY ts, id",
        (SOURCE, TYPE, start - timedelta(seconds=LOOKBACK_S), end + timedelta(seconds=STALE_S)),
    ).fetchall()
    reported = any(e["ts"] >= end + timedelta(seconds=PAD_S) for e in events)
    if not reported and (now - end).total_seconds() < WAIT_S:
        recent = conn.execute(
            "SELECT 1 FROM events WHERE source = %s AND type = %s AND ts >= %s AND ts < %s LIMIT 1",
            (SOURCE, TYPE, start - timedelta(seconds=RECENT_S), end),
        ).fetchone()
        if recent:
            return "hold"   # the Mac is on and its report of these minutes is still in its queue
    sounds = [s for s in spans(events) if s.end > start and s.start < end]
    if not sounds:
        return "keep"
    cuts = [((s.start - start).total_seconds() - PAD_S, (s.end - start).total_seconds() + PAD_S) for s in sounds]
    speech = subtract_spans(chunk["speech"], cuts)
    if speech == chunk["speech"]:
        return "keep"
    meta = {
        "mac_sound_cut": [[round(a, 3), round(b, 3)] for a, b in cuts],
        "mac_sound_apps": sorted(set().union(*(s.apps for s in sounds))),
        "speech_before_mac_sound": chunk["speech"],
    }
    speech_s = round(sum(e - s for s, e in speech), 2)
    conn.execute(
        "UPDATE chunks SET speech = %s, speech_s = %s, metadata = metadata || %s, stage = %s, updated_at = now() "
        "WHERE id = %s",
        (Jsonb(speech), speech_s, Jsonb(meta), "vad" if speech else "no_speech", chunk["id"]),
    )
    chunk["speech"], chunk["speech_s"] = speech, speech_s
    return "trimmed" if speech else "empty"
