"""Speaker identity across recordings (product 6).

Diarization labels voices SPEAKER_01, SPEAKER_02 within one recording only. This module links those voices
to people over time:

- Every voice of a new episode is compared with the stored voiceprints. A match at or above SPEAKER_MATCH
  takes that speaker. A voice that matches nobody becomes a new 'unknown' speaker, so a stranger heard
  again on another day is recognised as the same stranger.
- The user is only asked about voices worth naming: heard on several days, talking for a long time in
  one conversation, or recorded during a calendar meeting with other attendees (they are offered as names). The others keep a role label the LLM reads from the conversation ("nhân viên tổng đài").
- Naming a voice stores its voiceprints and a short audio sample forever, re-matches older voices, and
  rebuilds the transcripts, the index and the graph of the episodes whose labels changed.
- "Not this person" on a voice keeps it as a counter-example: a voice closer to it than to the person's own
  voiceprints is not matched to them. The voice is matched anew and the person's other automatic matches
  are checked again.
- "Not important" is not forever: a voice heard again afterwards is asked about again when its voices since
  then pass the same rule.
- Unnamed voices (unknown or ignored) are forgotten after STRANGER_KEEP_DAYS without being heard.

Voiceprints are the cluster embeddings pyannote returns, so enrolment runs the same diarization pipeline.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import soundfile as sf
from psycopg.types.json import Jsonb

from .audio import SAMPLE_RATE, AudioError, cut, decode
from .config import get_settings
from .db import connect
from .embed import vector_literal

log = logging.getLogger("lifelog")

VOICE_LOCK = 7_316_006
MIN_VOICE_S = 5.0          # shorter voices give unreliable embeddings: never matched or stored
MAX_PRINTS = 30            # voiceprints kept per speaker; the oldest automatic ones go first
ASK_MIN_DAYS = 2           # ask about a voice heard on at least this many days
ASK_MIN_SPEECH_S = 60.0    # or one that talked this long in a single conversation
CLIP_S = 6.0
SAMPLE_S = 10.0
MIN_ENROLL_S = 20.0      # enrolment needs at least this much speech of the person
LABEL_LIMIT = 80


class VoiceError(ValueError):
    """The request cannot be done, with a message for the user."""


def _lock(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (VOICE_LOCK,))


def _usable(vec) -> bool:
    arr = np.asarray(vec, dtype=np.float64)
    return arr.size > 0 and bool(np.all(np.isfinite(arr))) and float(np.linalg.norm(arr)) > 1e-6


def _matches(conn, embedding: str, model: str, *,
             statuses: tuple[str, ...] = ("named", "unknown", "ignored")) -> list[dict]:
    """Speakers ranked by their closest voiceprint to `embedding`, leaving out a speaker when the voice is at
    least as close to a voice the user said is not them."""
    return conn.execute(
        """
        SELECT v.speaker_id, p.status, max(1 - (v.embedding <=> %(e)s::vector)) AS score
        FROM voiceprints v JOIN speakers p ON p.id = v.speaker_id
        WHERE v.embedding_model = %(m)s AND p.status = ANY(%(st)s)
        GROUP BY v.speaker_id, p.status
        HAVING max(1 - (v.embedding <=> %(e)s::vector)) > COALESCE(
            (SELECT max(1 - (r.embedding <=> %(e)s::vector)) FROM voice_rejections r
             WHERE r.speaker_id = v.speaker_id AND r.embedding_model = %(m)s), -2)
        ORDER BY score DESC LIMIT 5
        """,
        {"e": embedding, "m": model, "st": list(statuses)},
    ).fetchall()


def _add_print(conn, speaker_id, embedding: str, model: str, origin: str, *, episode_id=None, label=None,
               speech_s=None) -> None:
    exists = conn.execute(
        "SELECT 1 FROM voiceprints WHERE speaker_id = %s AND episode_id IS NOT DISTINCT FROM %s "
        "AND label IS NOT DISTINCT FROM %s AND episode_id IS NOT NULL",
        (speaker_id, episode_id, label)).fetchone()
    if exists:
        return
    conn.execute(
        "INSERT INTO voiceprints (speaker_id, embedding, embedding_model, origin, episode_id, label, speech_s) "
        "VALUES (%s, %s::vector, %s, %s, %s, %s, %s)",
        (speaker_id, embedding, model, origin, episode_id, label, speech_s))
    conn.execute(
        """
        DELETE FROM voiceprints WHERE id IN (
            SELECT id FROM voiceprints WHERE speaker_id = %s AND origin <> 'enroll'
            ORDER BY (origin = 'confirm') DESC, created_at DESC OFFSET %s)
        """, (speaker_id, MAX_PRINTS))


def _drop_orphans(conn) -> None:
    """Unknown speakers no voice points to any more (episode re-diarized or re-matched)."""
    conn.execute("DELETE FROM speakers p WHERE status = 'unknown' AND NOT EXISTS "
                 "(SELECT 1 FROM episode_speakers es WHERE es.speaker_id = p.id)")


# ---------------------------------------------------------------- matching a new episode

def identify(conn, ep: dict) -> None:
    """Link each voice of a voice episode to a speaker. Assignments made by the user are kept.

    Running it again gives the same result: a voice matches its own stored voiceprint.
    """
    threshold = get_settings().speaker_match
    _lock(conn)
    rows = conn.execute(
        "SELECT label, speech_s, embedding::text AS embedding, embedding_model, speaker_id, assigned_by "
        "FROM episode_speakers WHERE episode_id = %s ORDER BY label", (ep["id"],)).fetchall()
    taken = {r["speaker_id"] for r in rows if r["assigned_by"] == "user" and r["speaker_id"]}
    open_rows = [r for r in rows if r["assigned_by"] != "user"]
    # Online meeting (product 8): the voice on the microphone channel is the user, whatever the voiceprints say.
    mic = ((ep.get("meta") or {}).get("meeting") or {}).get("mic_label")
    me = conn.execute("SELECT id FROM speakers WHERE is_me AND status = 'named'").fetchone()
    if mic and me and conn.execute(
            "SELECT 1 FROM voice_rejections WHERE speaker_id = %s AND episode_id = %s AND label = %s",
            (me["id"], ep["id"], mic)).fetchone():
        mic = None   # the user said this microphone voice is not them
    if mic and me and me["id"] not in taken and any(r["label"] == mic for r in open_rows):
        conn.execute("UPDATE episode_speakers SET speaker_id = %s, match_score = NULL, assigned_by = 'auto' "
                     "WHERE episode_id = %s AND label = %s", (me["id"], ep["id"], mic))
        conn.execute("UPDATE speakers SET last_seen_at = GREATEST(last_seen_at, %s) WHERE id = %s",
                     (ep["ended_at"], me["id"]))
        taken.add(me["id"])
        open_rows = [r for r in open_rows if r["label"] != mic]
    usable = {r["label"] for r in open_rows if r["embedding"] and (r["speech_s"] or 0) >= MIN_VOICE_S
              and _usable(json.loads(r["embedding"]))}

    # The voices of one episode are different people, so each speaker goes to at most one voice. Named people
    # win over strangers, then the best score.
    pairs = []
    for r in open_rows:
        if r["label"] in usable:
            for m in _matches(conn, r["embedding"], r["embedding_model"]):
                if m["score"] >= threshold:
                    pairs.append((m["status"] == "named", m["score"], r["label"], m["speaker_id"], m["status"]))
    chosen: dict[str, tuple] = {}
    for named, score, label, speaker_id, status in sorted(pairs, key=lambda p: (not p[0], -p[1])):
        if label not in chosen and speaker_id not in taken:
            chosen[label] = (speaker_id, score, status)
            taken.add(speaker_id)

    for r in open_rows:
        label = r["label"]
        if label in chosen:
            speaker_id, score, status = chosen[label]
        elif label in usable:   # nobody known: a new stranger
            speaker_id = conn.execute("INSERT INTO speakers (status) VALUES ('unknown') RETURNING id").fetchone()["id"]
            score, status = None, "unknown"
        else:
            speaker_id, score, status = None, None, None
        conn.execute(
            "UPDATE episode_speakers SET speaker_id = %s, match_score = %s, assigned_by = %s "
            "WHERE episode_id = %s AND label = %s",
            (speaker_id, score, "auto" if speaker_id else None, ep["id"], label))
        if not speaker_id:
            continue
        if status != "named":   # strangers learn every voice; named people only learn confirmed ones
            _add_print(conn, speaker_id, r["embedding"], r["embedding_model"], "auto",
                       episode_id=ep["id"], label=label, speech_s=r["speech_s"])
        conn.execute("UPDATE speakers SET last_seen_at = GREATEST(last_seen_at, %s) WHERE id = %s",
                     (ep["ended_at"], speaker_id))
    _drop_orphans(conn)
    # Meetings at that time: their attendees are offered as names on the page and given to the LLM.
    from .calendar import context

    conn.execute("UPDATE episodes SET meta = meta || %s WHERE id = %s",
                 (Jsonb(context(conn, ep["started_at"], ep["ended_at"])), ep["id"]))


def suggest(conn, ep: dict) -> None:
    """Ask the LLM once per episode for names and roles of the voices that are not named yet."""
    from . import summarize

    rows = conn.execute(
        """
        SELECT es.label FROM episode_speakers es LEFT JOIN speakers p ON p.id = es.speaker_id
        WHERE es.episode_id = %s AND NOT es.llm_checked AND p.name IS NULL
        """, (ep["id"],)).fetchall()
    labels = [r["label"] for r in rows]
    if not labels:
        return
    turns = conn.execute(
        """
        SELECT COALESCE(p.name, s.speaker) AS speaker, btrim(t.text) AS text
        FROM segments s
        JOIN segment_texts t ON t.segment_id = s.id AND t.backend = %s
        LEFT JOIN episode_speakers es ON es.episode_id = s.episode_id AND es.label = s.speaker
        LEFT JOIN speakers p ON p.id = es.speaker_id
        WHERE s.episode_id = %s AND btrim(t.text) <> '' ORDER BY s.idx
        """, (ep["asr_backend"], ep["id"])).fetchall()
    if not turns:
        return
    meta = conn.execute("SELECT meta FROM episodes WHERE id = %s", (ep["id"],)).fetchone()["meta"] or {}
    context = ""
    if meta.get("calendar_people"):
        context = (f'meeting "{"; ".join(meta.get("calendar_titles") or []) or "?"}", invited: '
                   + ", ".join(meta["calendar_people"]))
    try:
        summarize.check()
        found = summarize.suggest_speakers("\n".join(f"{t['speaker']}: {t['text']}" for t in turns), labels,
                                           context)
    except Exception as exc:  # suggestions are optional: never block indexing
        log.warning("episode %s: no speaker suggestions: %s", str(ep["id"])[:8], exc)
        return
    for label in labels:
        item = found.get(label) if isinstance(found.get(label), dict) else {}
        name = _clean(item.get("name"))
        role = _clean(item.get("role"))
        conn.execute("UPDATE episode_speakers SET suggested_name = %s, role = %s, llm_checked = true "
                     "WHERE episode_id = %s AND label = %s", (name, role, ep["id"], label))


def _clean(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()).strip(" .,:;\"'")
    return value[:LABEL_LIMIT] or None


# ---------------------------------------------------------------- after the user names someone

def _rematch(conn) -> set:
    """Link voices that are not named yet to named speakers whose voiceprints now match. Returns episode ids."""
    threshold = get_settings().speaker_match
    rows = conn.execute(
        """
        SELECT es.episode_id, es.label, es.embedding::text AS embedding, es.embedding_model
        FROM episode_speakers es LEFT JOIN speakers p ON p.id = es.speaker_id
        WHERE es.embedding IS NOT NULL AND es.speech_s >= %s AND es.assigned_by IS DISTINCT FROM 'user'
          AND p.name IS NULL
        """, (MIN_VOICE_S,)).fetchall()
    changed = set()
    for r in rows:
        best = next(iter(_matches(conn, r["embedding"], r["embedding_model"], statuses=("named",))), None)
        if not best or best["score"] < threshold:
            continue
        busy = conn.execute("SELECT 1 FROM episode_speakers WHERE episode_id = %s AND speaker_id = %s",
                            (r["episode_id"], best["speaker_id"])).fetchone()
        if busy:
            continue
        conn.execute("UPDATE episode_speakers SET speaker_id = %s, match_score = %s, assigned_by = 'auto' "
                     "WHERE episode_id = %s AND label = %s",
                     (best["speaker_id"], best["score"], r["episode_id"], r["label"]))
        changed.add(r["episode_id"])
    _drop_orphans(conn)
    return changed


def _refresh(conn, episode_ids) -> None:
    """Labels changed: rewrite the markdown now, and send indexed episodes back to be indexed and ingested again."""
    from .export import export_episode
    from .reprocess import _forget

    for ep_id in episode_ids:
        ep = conn.execute("SELECT id, stage, kind FROM episodes WHERE id = %s", (ep_id,)).fetchone()
        if not ep or ep["kind"] != "voice":
            continue
        if ep["stage"] in ("transcribed", "indexed", "ingested"):
            export_episode(conn, ep_id)
        if ep["stage"] in ("indexed", "ingested"):
            _forget(conn, ep_id)
            conn.execute("UPDATE episodes SET stage = 'transcribed', attempts = 0, error = NULL, updated_at = now() "
                         "WHERE id = %s", (ep_id,))


def _episodes_of(conn, speaker_id) -> set:
    return {r["episode_id"] for r in conn.execute(
        "SELECT episode_id FROM episode_speakers WHERE speaker_id = %s", (speaker_id,))}


def name_speaker(speaker_id, name: str) -> dict:
    """Give a voice a name. A name that already exists merges the two into one person."""
    name = " ".join((name or "").split())
    if not name:
        raise VoiceError("name is empty")
    if len(name) > LABEL_LIMIT:
        raise VoiceError(f"name is longer than {LABEL_LIMIT} characters")
    with connect() as conn:
        _lock(conn)
        sp = conn.execute("SELECT * FROM speakers WHERE id = %s", (speaker_id,)).fetchone()
        if not sp:
            raise VoiceError("no such speaker")
        affected = _episodes_of(conn, sp["id"])
        target = conn.execute("SELECT * FROM speakers WHERE status = 'named' AND lower(name) = lower(%s) AND id <> %s",
                              (name, sp["id"])).fetchone()
        if target:
            conn.execute("UPDATE voiceprints SET speaker_id = %s WHERE speaker_id = %s", (target["id"], sp["id"]))
            conn.execute("UPDATE voice_rejections SET speaker_id = %s WHERE speaker_id = %s", (target["id"], sp["id"]))
            # A recording with both voices keeps the one already linked to the target.
            conn.execute(
                "UPDATE episode_speakers es SET speaker_id = %s WHERE speaker_id = %s AND NOT EXISTS "
                "(SELECT 1 FROM episode_speakers o WHERE o.episode_id = es.episode_id AND o.speaker_id = %s)",
                (target["id"], sp["id"], target["id"]))
            conn.execute("UPDATE episode_speakers SET speaker_id = NULL, assigned_by = 'user' WHERE speaker_id = %s",
                         (sp["id"],))
            conn.execute("UPDATE speakers SET last_seen_at = GREATEST(last_seen_at, %s), updated_at = now(), "
                         "sample_path = COALESCE(sample_path, %s) WHERE id = %s",
                         (sp["last_seen_at"], sp["sample_path"], target["id"]))
            conn.execute("DELETE FROM speakers WHERE id = %s", (sp["id"],))
            sid, merged = target["id"], True
        else:
            conn.execute("UPDATE speakers SET name = %s, status = 'named', updated_at = now() WHERE id = %s",
                         (name, sp["id"]))
            sid, merged = sp["id"], False
        conn.execute("UPDATE voiceprints SET origin = 'confirm' WHERE speaker_id = %s AND origin = 'auto'", (sid,))
        _save_sample_from_episodes(conn, sid)
        affected |= _rematch(conn)
        affected |= _episodes_of(conn, sid)
        _refresh(conn, affected)
    log.info("speaker %s named %r%s, %d episode(s) to rebuild", str(sid)[:8], name, " (merged)" if merged else "",
             len(affected))
    return {"id": str(sid), "name": name, "merged": merged, "episodes": len(affected)}


def ignore_speaker(speaker_id) -> None:
    """The user does not care who this is: stop asking until the voice is heard enough again (see queue()).
    The voice keeps its role label."""
    with connect() as conn:
        _lock(conn)
        done = conn.execute("UPDATE speakers SET status = 'ignored', ignored_at = now(), updated_at = now() "
                            "WHERE id = %s AND status IN ('unknown', 'ignored')", (speaker_id,)).rowcount
        if not done:
            raise VoiceError("no such unnamed speaker")


def detach(episode_id, label: str) -> dict:
    """A voice was linked to the wrong person. Remember that it is not them, so it and voices like it are never
    matched to them again, then match it anew (another person, or a stranger to ask about) and check the
    person's other automatic matches again. Returns how many other voices left that person."""
    with connect() as conn:
        _lock(conn)
        row = conn.execute("SELECT speaker_id, embedding::text AS embedding, embedding_model "
                           "FROM episode_speakers WHERE episode_id = %s AND label = %s",
                           (episode_id, label)).fetchone()
        if not row:
            raise VoiceError("no such voice")
        sid = row["speaker_id"]
        recheck = {episode_id}
        if sid:
            conn.execute("DELETE FROM voiceprints WHERE speaker_id = %s AND episode_id = %s AND label = %s",
                         (sid, episode_id, label))
            if row["embedding"] and _usable(json.loads(row["embedding"])):
                conn.execute("INSERT INTO voice_rejections (speaker_id, embedding, embedding_model, episode_id, label) "
                             "VALUES (%s, %s::vector, %s, %s, %s)",
                             (sid, row["embedding"], row["embedding_model"], episode_id, label))
            recheck |= {r["episode_id"] for r in conn.execute(
                "SELECT episode_id FROM episode_speakers WHERE speaker_id = %s AND assigned_by = 'auto'", (sid,))}
        count = "SELECT count(*) AS n FROM episode_speakers WHERE speaker_id = %s"
        before_count = conn.execute(count, (sid,)).fetchone()["n"]
        conn.execute("UPDATE episode_speakers SET speaker_id = NULL, match_score = NULL, assigned_by = NULL "
                     "WHERE episode_id = %s AND label = %s", (episode_id, label))
        changed = {episode_id}
        for ep_id in recheck:
            ep = conn.execute("SELECT * FROM episodes WHERE id = %s", (ep_id,)).fetchone()
            before = _labels(conn, ep_id)
            identify(conn, ep)
            if _labels(conn, ep_id) != before:
                changed.add(ep_id)
        others = max(before_count - 1 - conn.execute(count, (sid,)).fetchone()["n"], 0) if sid else 0
        _refresh(conn, changed)
    if sid:
        log.info("voice %s/%s is not speaker %s; %d other voice(s) unlinked too", str(episode_id)[:8], label,
                 str(sid)[:8], others)
    return {"others": others}


def forget_strangers(now: datetime | None = None) -> int:
    """Delete unnamed voices (and their voiceprints) not heard for STRANGER_KEEP_DAYS."""
    settings = get_settings()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=settings.stranger_keep_days)
    with connect() as conn:
        _lock(conn)
        gone = conn.execute(
            "DELETE FROM speakers WHERE status IN ('unknown', 'ignored') AND COALESCE(last_seen_at, created_at) < %s "
            "RETURNING sample_path", (cutoff,)).fetchall()
    for g in gone:
        if g["sample_path"]:
            (settings.data_dir / g["sample_path"]).unlink(missing_ok=True)
    if gone:
        log.info("forgot %d unnamed voice(s) not heard for %d days", len(gone), settings.stranger_keep_days)
    return len(gone)


def _labels(conn, episode_id) -> dict:
    return {r["label"]: r["shown"] for r in conn.execute(
        "SELECT es.label, COALESCE(p.name, es.role) AS shown FROM episode_speakers es "
        "LEFT JOIN speakers p ON p.id = es.speaker_id WHERE es.episode_id = %s", (episode_id,))}


def rescan(since: datetime | None = None, *, llm: bool = True) -> dict:
    """Match voices of episodes indexed before speaker matching existed (or after a threshold change).

    Episodes whose labels change are rebuilt. With `llm`, unnamed voices also get role labels, which rebuilds
    those episodes too (the graph re-ingest costs LLM tokens).
    """
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM episodes WHERE kind = 'voice' AND stage IN ('indexed', 'ingested') "
            "AND (%s::timestamptz IS NULL OR started_at >= %s) ORDER BY started_at", (since, since))]
    changed = 0
    for ep_id in ids:
        with connect() as conn:
            ep = conn.execute("SELECT * FROM episodes WHERE id = %s FOR NO KEY UPDATE", (ep_id,)).fetchone()
            before = _labels(conn, ep_id)
            identify(conn, ep)
            if llm:
                suggest(conn, ep)
            if _labels(conn, ep_id) != before:
                _refresh(conn, {ep_id})
                changed += 1
    return {"episodes": len(ids), "rebuilt": changed}


# ---------------------------------------------------------------- audio clips

def _voice_audio(conn, episode_id, label: str, seconds: float) -> np.ndarray:
    """About `seconds` of one voice, from the middle of its longest turn."""
    from .pipeline import episode_audio_range

    ep = conn.execute("SELECT * FROM episodes WHERE id = %s", (episode_id,)).fetchone()
    seg = conn.execute(
        "SELECT start_s, end_s FROM segments WHERE episode_id = %s AND speaker = %s "
        "ORDER BY end_s - start_s DESC LIMIT 1", (episode_id, label)).fetchone()
    if not ep or not seg or not ep["timeline"]:
        raise VoiceError("no audio for this voice")
    mid = (seg["start_s"] + seg["end_s"]) / 2
    half = min(seconds, seg["end_s"] - seg["start_s"]) / 2
    try:
        return episode_audio_range(conn, ep, mid - half, mid + half)
    except (AudioError, FileNotFoundError, TypeError) as exc:
        raise VoiceError(f"audio is no longer available: {exc}") from exc


def _write_wav(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, samples, SAMPLE_RATE, format="WAV", subtype="PCM_16")


def clip_path(episode_id, label: str) -> Path:
    """A short clip of one voice in one episode, cut once and cached under data/speakers/clips."""
    settings = get_settings()
    path = settings.data_dir / "speakers" / "clips" / f"{episode_id}_{label}.wav"
    if not path.exists():
        with connect() as conn:
            _write_wav(path, _voice_audio(conn, episode_id, label, CLIP_S))
    return path


def best_voice(conn, speaker_id) -> dict | None:
    """The voice of this speaker with the most speech, for clips and samples."""
    return conn.execute(
        "SELECT episode_id, label, speech_s FROM episode_speakers WHERE speaker_id = %s "
        "ORDER BY speech_s DESC LIMIT 1", (speaker_id,)).fetchone()


def _save_sample(conn, speaker_id, samples: np.ndarray) -> None:
    rel = Path("speakers") / "samples" / f"{speaker_id}.wav"
    _write_wav(get_settings().data_dir / rel, samples)
    conn.execute("UPDATE speakers SET sample_path = %s WHERE id = %s", (str(rel), speaker_id))


def _save_sample_from_episodes(conn, speaker_id) -> None:
    has = conn.execute("SELECT sample_path FROM speakers WHERE id = %s", (speaker_id,)).fetchone()
    if has and has["sample_path"] and (get_settings().data_dir / has["sample_path"]).exists():
        return
    voice = best_voice(conn, speaker_id)
    if not voice:
        return
    try:
        _save_sample(conn, speaker_id, _voice_audio(conn, voice["episode_id"], voice["label"], SAMPLE_S))
    except VoiceError as exc:
        log.warning("speaker %s: no audio sample saved: %s", str(speaker_id)[:8], exc)


# ---------------------------------------------------------------- enrolment

@dataclass
class Enrolled:
    id: str
    name: str
    speech_s: float
    other_s: float       # speech of other voices in the file, ignored
    episodes: int        # episodes relabelled because of this voice


def enroll(path: Path, name: str | None = None, *, me: bool = False) -> Enrolled:
    """Learn a voice from an audio file in which this person does most of the talking."""
    from .diarize import DiarizerUnavailable, get_diarizer
    from .turns import build_turns

    settings = get_settings()
    name = " ".join((name or (settings.owner_name if me else "")).split())
    if not name:
        raise VoiceError("give a name, or use --me")
    try:
        samples = decode(path)
    except AudioError as exc:
        raise VoiceError(str(exc)) from exc
    try:
        diarizer = get_diarizer(settings.diarizer)
    except DiarizerUnavailable as exc:
        raise VoiceError(str(exc)) from exc
    result = diarizer.run(samples, [])
    turns = build_turns(result.turns, merge_gap_s=settings.turn_merge_gap_s, max_s=1e9, min_s=settings.turn_min_s)
    spoken: dict[str, float] = {}
    for t in turns:
        spoken[t.speaker] = spoken.get(t.speaker, 0.0) + t.length
    if not spoken:
        raise VoiceError("no speech found in the file")
    main = max(spoken, key=spoken.get)
    vec = result.embeddings.get(main)
    if vec is None or not _usable(vec):
        raise VoiceError(f"the diarizer gave no voice embedding (diarizer: {settings.diarizer})")
    if spoken[main] < MIN_ENROLL_S:
        raise VoiceError(f"only {spoken[main]:.0f} seconds of speech; record at least 30 seconds")
    longest = max((t for t in turns if t.speaker == main), key=lambda t: t.length)
    mid, half = (longest.start + longest.end) / 2, min(SAMPLE_S, longest.length) / 2

    with connect() as conn:
        _lock(conn)
        sp = conn.execute("SELECT id FROM speakers WHERE status = 'named' AND lower(name) = lower(%s)",
                          (name,)).fetchone()
        sid = sp["id"] if sp else conn.execute(
            "INSERT INTO speakers (name, status) VALUES (%s, 'named') RETURNING id", (name,)).fetchone()["id"]
        if me:
            conn.execute("UPDATE speakers SET is_me = (id = %s)", (sid,))
        conn.execute(
            "INSERT INTO voiceprints (speaker_id, embedding, embedding_model, origin, speech_s) "
            "VALUES (%s, %s::vector, %s, 'enroll', %s)", (sid, vector_literal(vec), result.model, spoken[main]))
        _save_sample(conn, sid, cut(samples, mid - half, mid + half))
        affected = _rematch(conn)
        _refresh(conn, affected)
    return Enrolled(str(sid), name, spoken[main], sum(spoken.values()) - spoken[main], len(affected))


# ---------------------------------------------------------------- what the page and the CLI show

def queue() -> list[dict]:
    """Unnamed voices worth asking about, most talkative first. A voice marked not important counts only what
    was heard after that, so it comes back when it keeps turning up."""
    tz = get_settings().timezone
    with connect() as conn:
        return conn.execute(
            """
            SELECT p.id, p.ignored_at, count(DISTINCT es.episode_id) AS episodes,
                   count(DISTINCT (e.started_at AT TIME ZONE %(tz)s)::date) AS days,
                   sum(es.speech_s) AS speech_s, max(es.speech_s) AS longest_s, max(e.started_at) AS last_seen,
                   array_remove(array_agg(DISTINCT es.suggested_name), NULL) AS names,
                   array_remove(array_agg(DISTINCT es.role), NULL) AS roles,
                   COALESCE((SELECT array_agg(DISTINCT n) FROM episode_speakers es2 JOIN episodes e2 ON e2.id = es2.episode_id,
                             jsonb_array_elements_text(COALESCE(e2.meta->'calendar_people', '[]')) n
                             WHERE es2.speaker_id = p.id
                               AND (p.ignored_at IS NULL OR e2.started_at > p.ignored_at)), '{}') AS invited
            FROM speakers p
            JOIN episode_speakers es ON es.speaker_id = p.id
            JOIN episodes e ON e.id = es.episode_id
            WHERE p.status = 'unknown' OR (p.status = 'ignored' AND e.started_at > p.ignored_at)
            GROUP BY p.id
            HAVING count(DISTINCT (e.started_at AT TIME ZONE %(tz)s)::date) >= %(days)s OR max(es.speech_s) >= %(long)s
                OR bool_or(jsonb_array_length(COALESCE(e.meta->'calendar_people', '[]')) > 0)
            ORDER BY sum(es.speech_s) DESC
            """, {"tz": tz, "days": ASK_MIN_DAYS, "long": ASK_MIN_SPEECH_S}).fetchall()


def people() -> list[dict]:
    """Named speakers with how much they were heard."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT p.id, p.name, p.is_me, p.sample_path IS NOT NULL AS has_sample,
                   count(DISTINCT es.episode_id) AS episodes, COALESCE(sum(es.speech_s), 0) AS speech_s,
                   max(p.last_seen_at) AS last_seen,
                   (SELECT count(*) FROM voiceprints v WHERE v.speaker_id = p.id) AS prints
            FROM speakers p LEFT JOIN episode_speakers es ON es.speaker_id = p.id
            WHERE p.status = 'named'
            GROUP BY p.id ORDER BY p.is_me DESC, speech_s DESC
            """).fetchall()


def voices_of(speaker_id) -> list[dict]:
    """Every voice linked to a speaker, newest first."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT es.episode_id, es.label, es.speech_s, es.match_score, es.assigned_by, e.started_at
            FROM episode_speakers es JOIN episodes e ON e.id = es.episode_id
            WHERE es.speaker_id = %s ORDER BY e.started_at DESC
            """, (speaker_id,)).fetchall()


def coverage(days: int = 14) -> dict:
    """Share of recorded speech in the last `days` days that carries a name, and how much of it was automatic."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(sum(es.speech_s), 0) AS total,
                   COALESCE(sum(es.speech_s) FILTER (WHERE p.name IS NOT NULL), 0) AS named,
                   COALESCE(sum(es.speech_s) FILTER (WHERE p.name IS NOT NULL AND es.assigned_by = 'auto'), 0) AS auto,
                   COALESCE(sum(es.speech_s) FILTER (WHERE p.status = 'ignored'), 0) AS ignored
            FROM episode_speakers es JOIN episodes e ON e.id = es.episode_id
            LEFT JOIN speakers p ON p.id = es.speaker_id
            WHERE e.kind = 'voice' AND e.started_at >= now() - make_interval(days => %s)
            """, (days,)).fetchone()
    return {k: float(v) for k, v in row.items()}
