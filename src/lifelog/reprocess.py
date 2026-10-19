"""Chạy lại có chọn lọc: đưa việc lùi về một bước để thợ làm lại từ đó."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .config import get_settings
from .db import connect

STEPS = {
    "vad": "vad", "received": "vad",
    "group": "group", "grouped": "group",
    "diarize": "diarize", "diarized": "diarize",
    "transcribe": "transcribe", "transcribed": "transcribe",
    "index": "index", "indexed": "index",
    "ingest": "ingest", "ingested": "ingest",
}
# Các bước mà episode đã có đủ chữ.
HAS_TEXT = ("transcribed", "indexed", "ingested")


def _bounds(since: date | None, until: date | None) -> tuple[datetime | None, datetime | None]:
    tz = get_settings().tz
    lo = datetime.combine(since, time.min, tz) if since else None
    hi = datetime.combine(until + timedelta(days=1), time.min, tz) if until else None
    return lo, hi


def find_episodes(conn, *, since: date | None = None, until: date | None = None,
                  episode: str | None = None) -> list[dict]:
    lo, hi = _bounds(since, until)
    return conn.execute(
        """
        SELECT * FROM episodes
        WHERE (%(lo)s::timestamptz IS NULL OR started_at >= %(lo)s)
          AND (%(hi)s::timestamptz IS NULL OR started_at < %(hi)s)
          AND (%(ep)s::text IS NULL OR id::text LIKE %(ep)s || '%%')
        ORDER BY started_at
        """,
        {"lo": lo, "hi": hi, "ep": episode},
    ).fetchall()


def _drop_episodes(conn, ids: list) -> None:
    data_dir = get_settings().data_dir
    for row in conn.execute("SELECT export_path FROM episodes WHERE id = ANY(%s)", (ids,)).fetchall():
        if row["export_path"]:
            (data_dir / row["export_path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM episodes WHERE id = ANY(%s)", (ids,))


def _forget(conn, episode_id, *, windows: bool = True) -> None:
    """Bộ nhớ dựng từ transcript cũ không còn đúng: xóa vector, đánh dấu phần đã nạp vào Graphiti là lỗi thời."""
    if windows:
        conn.execute("DELETE FROM windows WHERE episode_id = %s", (episode_id,))
        # The episode summary (a code session, or a conversation) will be written again, so the journal
        # of that day must be written again too.
        conn.execute(
            "DELETE FROM journals j USING episodes e WHERE e.id = %s AND e.kind IN ('code', 'voice') "
            "AND j.day = (e.started_at AT TIME ZONE %s)::date",
            (episode_id, get_settings().timezone),
        )
        conn.execute("UPDATE episodes SET summary = NULL, summary_model = NULL WHERE id = %s", (episode_id,))
    # Thợ sẽ gỡ các phần này khỏi Graphiti trước khi nạp bản mới.
    conn.execute("UPDATE graph_parts SET episode_id = NULL WHERE episode_id = %s", (episode_id,))


def reprocess(from_step: str, *, since: date | None = None, until: date | None = None,
              episode: str | None = None, backend: str | None = None) -> dict[str, int]:
    """Lùi việc về trước bước `from_step`. Trả về số episode và chunk bị ảnh hưởng."""
    if from_step not in STEPS:
        raise ValueError(
            f"bước không hợp lệ: {from_step}. Chọn một trong: vad, group, diarize, transcribe, index, ingest")
    step = STEPS[from_step]
    default_backend = get_settings().asr_backend

    with connect() as conn:
        episodes = find_episodes(conn, since=since, until=until, episode=episode)
        ids = [e["id"] for e in episodes]
        counts = {"episodes": 0, "chunks": 0}

        if step == "ingest":
            for e in episodes:
                if e["stage"] not in ("indexed", "ingested"):
                    continue
                _forget(conn, e["id"], windows=False)
                conn.execute(
                    "UPDATE episodes SET stage = 'indexed', attempts = 0, error = NULL, updated_at = now() WHERE id = %s",
                    (e["id"],),
                )
                counts["episodes"] += 1

        elif step == "index":
            for e in episodes:
                if e["stage"] not in HAS_TEXT:
                    continue
                _forget(conn, e["id"])
                conn.execute(
                    "UPDATE episodes SET stage = 'transcribed', attempts = 0, error = NULL, updated_at = now() "
                    "WHERE id = %s",
                    (e["id"],),
                )
                counts["episodes"] += 1

        elif step == "transcribe":
            for e in episodes:
                if e["stage"] not in ("diarized", *HAS_TEXT) or e["kind"] != "voice":
                    continue  # phiên code không có audio để chuyển chữ lại
                _forget(conn, e["id"])
                target = backend or e["asr_backend"] or default_backend
                conn.execute(
                    "DELETE FROM segment_texts WHERE backend = %s AND segment_id IN "
                    "(SELECT id FROM segments WHERE episode_id = %s)",
                    (target, e["id"]),
                )
                conn.execute(
                    "UPDATE episodes SET stage = 'diarized', asr_backend = %s, attempts = 0, error = NULL, "
                    "updated_at = now() WHERE id = %s",
                    (target, e["id"]),
                )
                counts["episodes"] += 1

        elif step == "diarize":
            for e in episodes:
                if e["stage"] not in ("grouped", "diarized", *HAS_TEXT) or e["kind"] != "voice":
                    continue
                _forget(conn, e["id"])
                conn.execute("DELETE FROM segments WHERE episode_id = %s", (e["id"],))
                conn.execute("DELETE FROM episode_speakers WHERE episode_id = %s", (e["id"],))
                conn.execute(
                    "UPDATE episodes SET stage = 'grouped', asr_backend = COALESCE(%s, asr_backend), attempts = 0, "
                    "error = NULL, updated_at = now() WHERE id = %s",
                    (backend, e["id"]),
                )
                counts["episodes"] += 1

        else:
            # Ghép lại hoặc cắt im lặng lại: episode cũ bị xóa, chunk quay về bước trước.
            lo, hi = _bounds(since, until)
            chunk_rows = conn.execute(
                """
                SELECT id FROM chunks
                WHERE episode_id = ANY(%(ids)s)
                   OR (%(ep)s::text IS NULL
                       AND (%(lo)s::timestamptz IS NULL OR started_at >= %(lo)s)
                       AND (%(hi)s::timestamptz IS NULL OR started_at < %(hi)s))
                """,
                {"ids": ids, "ep": episode, "lo": lo, "hi": hi},
            ).fetchall()
            chunk_ids = [c["id"] for c in chunk_rows]
            # Episode nào có chunk bị đụng tới thì phải dựng lại cả episode.
            touched = conn.execute(
                "SELECT DISTINCT episode_id FROM chunks WHERE id = ANY(%s) AND episode_id IS NOT NULL", (chunk_ids,)
            ).fetchall()
            all_ids = list({*ids, *(t["episode_id"] for t in touched)})
            siblings = conn.execute("SELECT id FROM chunks WHERE episode_id = ANY(%s)", (all_ids,)).fetchall()
            chunk_ids = list({*chunk_ids, *(s["id"] for s in siblings)})
            # Phiên code bị xóa thì sự kiện của nó quay về chờ gom lại.
            conn.execute("UPDATE events SET stage = 'received', episode_id = NULL WHERE episode_id = ANY(%s)", (all_ids,))
            conn.execute(
                "DELETE FROM journals j USING episodes e WHERE e.id = ANY(%s) AND e.kind = 'code' "
                "AND j.day = (e.started_at AT TIME ZONE %s)::date",
                (all_ids, get_settings().timezone),
            )
            _drop_episodes(conn, all_ids)
            if step == "group":
                done = conn.execute(
                    "UPDATE chunks SET stage = 'vad', episode_id = NULL, attempts = 0, error = NULL, updated_at = now() "
                    "WHERE id = ANY(%s) AND stage = 'grouped'",
                    (chunk_ids,),
                )
            else:
                done = conn.execute(
                    "UPDATE chunks SET stage = 'received', episode_id = NULL, speech = NULL, speech_s = NULL, "
                    "attempts = 0, error = NULL, updated_at = now() WHERE id = ANY(%s)",
                    (chunk_ids,),
                )
            counts["episodes"] = len(all_ids)
            counts["chunks"] = done.rowcount
        return counts


def retry_failed() -> dict[str, int]:
    """Cho những việc đã hết lượt thử được thử lại."""
    with connect() as conn:
        c = conn.execute("UPDATE chunks SET attempts = 0, error = NULL WHERE attempts > 0 OR error IS NOT NULL")
        e = conn.execute("UPDATE episodes SET attempts = 0, error = NULL WHERE attempts > 0 OR error IS NOT NULL")
        return {"chunks": c.rowcount, "episodes": e.rowcount}
