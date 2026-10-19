"""The iPhone recorder (product 5): what reached the server, and where the gaps are.

The app uploads 5-minute chunks through `POST /chunks` with `source = "iphone"`. Its metadata (the sidecar)
says why each chunk ended (`end_reason`: rotate, close, stop, interruption, route_change, media_reset, update
(the Mac reinstalled the app to renew its signature), app_killed, error), which microphone was used, battery,
charging, the last known location, motion activity, whether a phone call happened, and `playback` ([start, end]
seconds while another app played sound on the iPhone's own speaker; that speech is left out, see
`pipeline.step_vad`).
`close` means the user pressed "End conversation": that chunk carries `close_episode`, so the conversation is
processed at once, and recording goes on in the next chunk.

While the iPhone is off, the Mac app can record the room from its own microphone the same way (`source =
"mac-room"`, 5-minute chunks, `end_reason` rotate, close, stop, quit, sleep, route_change, gap, app_killed).
The server treats both as the all-day recording (`ALL_DAY`); the report is per source.

A gap is the time between the end of one chunk and the start of the next. Its reason is the `end_reason` of
the chunk before it. The product is done when a day of 8 hours has under 5 minutes of gaps, all explained.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .config import get_settings
from .db import connect

SOURCE = "iphone"
MAC_SOURCE = "mac-room"
ALL_DAY = (SOURCE, MAC_SOURCE)
GAP_S = 5.0                 # shorter breaks between chunks are just the file rotation
EXPECTED = {"stop", "quit", "sleep"}    # gaps after these reasons are wanted, not losses (the last two: Mac)


@dataclass
class Gap:
    start: datetime
    end: datetime
    reason: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class DayReport:
    day: date
    chunks: int = 0
    recorded_s: float = 0.0
    first: datetime | None = None
    last: datetime | None = None
    gaps: list[Gap] = field(default_factory=list)
    calls: int = 0

    @property
    def lost_s(self) -> float:
        """Gaps while recording was meant to be on."""
        return sum(g.seconds for g in self.gaps if g.reason not in EXPECTED)

    def as_dict(self) -> dict:
        tz = get_settings().tz
        return {
            "day": self.day.isoformat(), "chunks": self.chunks, "recorded_s": round(self.recorded_s),
            "lost_s": round(self.lost_s), "calls": self.calls,
            "first": self.first.astimezone(tz).isoformat() if self.first else None,
            "last": self.last.astimezone(tz).isoformat() if self.last else None,
            "gaps": [{"start": g.start.astimezone(tz).isoformat(), "end": g.end.astimezone(tz).isoformat(),
                      "seconds": round(g.seconds), "reason": g.reason} for g in self.gaps],
        }


def day_report(day: date, source: str = SOURCE) -> DayReport:
    tz = get_settings().tz
    lo = datetime.combine(day, time.min, tz)
    with connect() as conn:
        rows = conn.execute(
            "SELECT started_at, ended_at, duration_s, metadata FROM chunks WHERE source = %s "
            "AND started_at >= %s AND started_at < %s ORDER BY started_at",
            (source, lo, lo + timedelta(days=1))).fetchall()
    report = DayReport(day)
    prev = None
    for r in rows:
        meta = r["metadata"] or {}
        report.chunks += 1
        report.recorded_s += r["duration_s"] or 0
        report.first = report.first or r["started_at"]
        report.last = r["ended_at"]
        report.calls += bool(meta.get("call"))
        if prev and (r["started_at"] - prev["ended_at"]).total_seconds() > GAP_S:
            reason = (prev["metadata"] or {}).get("end_reason") or "unknown"
            report.gaps.append(Gap(prev["ended_at"], r["started_at"], reason))
        prev = r
    return report
