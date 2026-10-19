"""Người thợ: mỗi bước chỉ lấy việc đang ở bước kế trước, xong thì ghi vào sổ.

Chunk:   received -> vad -> grouped   (hoặc no_speech)
Episode: open -> grouped -> diarized -> transcribed -> indexed -> ingested

Hai bước cuối là bộ nhớ (Sản phẩm 2): `indexed` là đã có vector nguyên văn trong Postgres,
`ingested` là đã nạp vào Graphiti.

Lỗi ở bước nào thì việc đứng lại ở bước đó, lần sau làm tiếp từ đó.
"""
from __future__ import annotations

import faulthandler
import logging
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable

import numpy as np
from psycopg.types.json import Jsonb

from . import (calendar, claude_code, embed, git_log, graph, mac_log, mac_sound, meeting, reading,
               summarize, voices, zalo_episodes)
from .asr import AsrBackend, AsrUnavailable, get_backend
from .code_sessions import describe, group_code
from .audio import AUDIO_EXTS, SAMPLE_RATE, AudioError, cut, decode, wav_bytes
from .config import get_settings
from .db import connect
from .diarize import DiarizerUnavailable, get_diarizer
from .export import export_episode
from .ingest import ingest_file
from .turns import build_turns, relabel, snap_cuts
from .vad import speech_intervals, subtract_spans
from .windows import Window, build_windows, transcript_rows

log = logging.getLogger("lifelog")

GROUP_LOCK = 7_316_001  # khóa để chỉ một thợ ghép episode tại một thời điểm


class Blocked(RuntimeError):
    """Chưa làm được vì thiếu cấu hình. Không tính là một lần thử hỏng."""


# ---------------------------------------------------------------- khung chạy bước

def _record_failure(table: str, row_id, message: str, *, count: bool) -> None:
    with connect() as conn:
        conn.execute(
            f"UPDATE {table} SET attempts = attempts + %s, error = %s, updated_at = now() WHERE id = %s",
            (1 if count else 0, message[:2000], row_id),
        )


def _run_step(table: str, stage: str, fn: Callable, only: list | None = None,
              since: datetime | None = None, kinds: list[str] | None = None, where: str | None = None) -> bool:
    """Lấy một việc ở `stage`, giữ khóa dòng đó trong lúc làm. Trả về True nếu có tiến triển.

    Đặt `only` là danh sách mã để chỉ làm những việc đó, bỏ qua việc tồn đọng khác.
    Đặt `since` để chỉ làm những việc bắt đầu từ mốc đó trở đi. `kinds` chỉ dùng cho episode: chỉ làm
    những loại này. `where` is one more SQL condition on the row, with no parameters.
    """
    settings = get_settings()
    noun = {"chunks": "file", "episodes": "episode"}.get(table, table)
    row_id = None
    try:
        with connect() as conn:
            row = conn.execute(
                # NO KEY UPDATE vẫn chặn thợ khác lấy trùng việc, nhưng không chặn kết nối phụ
                # chèn dòng con trỏ tới dòng này (ví dụ graph_parts) trong lúc bước còn đang chạy.
                f"SELECT * FROM {table} WHERE stage = %s AND attempts < %s AND (%s::uuid[] IS NULL OR id = ANY(%s)) "
                "AND (%s::timestamptz IS NULL OR started_at >= %s) "
                + ("AND kind = ANY(%s) " if kinds else "")
                + (f"AND {where} " if where else "")
                + "ORDER BY started_at LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED",
                (stage, settings.max_attempts, only, only, since, since) + ((kinds,) if kinds else ()),
            ).fetchone()
            if row is None:
                return False
            row_id = row["id"]
            fn(conn, row)
        return True
    except Blocked as exc:
        log.warning("%s %s đang chờ cấu hình: %s", noun, str(row_id)[:8], exc)
        _record_failure(table, row_id, str(exc), count=False)
        return False
    except Exception as exc:
        if row_id is None:
            raise
        log.error("%s %s lỗi khi đang ở bước '%s': %s", noun, str(row_id)[:8], stage, exc)
        log.debug("chi tiết lỗi", exc_info=True)
        _record_failure(table, row_id, f"{type(exc).__name__}: {exc}", count=True)
        return True


# ---------------------------------------------------------------- bước 1: cắt im lặng

def step_vad(conn, chunk: dict) -> None:
    samples = decode(get_settings().data_dir / chunk["path"])
    speech = speech_intervals(samples)
    meta = {}
    # The iPhone marks when another app played sound on its own speaker (a video); that is not a conversation.
    playback = (chunk["metadata"] or {}).get("playback") or []
    if playback and (kept := subtract_spans(speech, playback)) != speech:
        meta = {"speech_before_playback": speech}
        speech = kept
    speech_s = round(sum(e - s for s, e in speech), 2)
    conn.execute(
        "UPDATE chunks SET speech = %s, speech_s = %s, metadata = metadata || %s, stage = %s, error = NULL, "
        "updated_at = now() WHERE id = %s",
        (Jsonb(speech), speech_s, Jsonb(meta), "vad" if speech else "no_speech", chunk["id"]),
    )
    log.info("chunk %s: %.0f giây, có tiếng nói %.0f giây", str(chunk["id"])[:8], chunk["duration_s"], speech_s)


# ---------------------------------------------------------------- bước 2: ghép episode

def _ended_between(conn, after: datetime, until: datetime) -> bool:
    """Did the user end a conversation by hand (Stop, or "End conversation" on the iPhone) in (after, until]?
    The chunk carrying that flag ends where the press was, whether or not it holds speech; Mac meetings close
    only themselves."""
    return conn.execute(
        "SELECT 1 FROM chunks WHERE close_episode AND source <> %s AND ended_at > %s AND ended_at <= %s LIMIT 1",
        (meeting.SOURCE, after, until),
    ).fetchone() is not None


def step_group() -> bool:
    """Gắn chunk có tiếng nói vào episode, rồi đóng những episode đã im đủ lâu."""
    settings = get_settings()
    gap = timedelta(seconds=settings.episode_gap_s)
    changed = False
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (GROUP_LOCK,)).fetchone()["ok"]:
            return False

        now = datetime.now(timezone.utc)
        chunks = conn.execute("SELECT * FROM chunks WHERE stage = 'vad' ORDER BY started_at FOR UPDATE").fetchall()
        for c in chunks:
            # A Mac meeting recording replaces the phone's audio of the same minutes (product 8), and what
            # the Mac plays out loud (a video, music) is not a conversation.
            if meeting.is_phone(c):
                state = meeting.phone_state(conn, c, now)
                if state not in ("hold", "empty"):
                    state = mac_sound.phone_state(conn, c, now)
                if state == "hold":
                    continue
                if state == "empty":
                    changed = True
                    continue
            first = c["started_at"] + timedelta(seconds=c["speech"][0][0])
            last = c["started_at"] + timedelta(seconds=c["speech"][-1][1])
            # A few seconds of speech in a phone chunk is mostly noise: it may join a conversation that is
            # already open around it, but it does not start one or keep one open.
            weak = meeting.is_phone(c) and (c["speech_s"] or 0) < settings.weak_chunk_speech_s
            if meeting.is_meeting(c):
                # One meeting is one episode however long its silences, and never shares it with the phone.
                info = meeting.info(c)
                ep = conn.execute(
                    "SELECT id FROM episodes WHERE stage = 'open' AND meta->'meeting'->>'id' = %s LIMIT 1 FOR UPDATE",
                    (info["id"],),
                ).fetchone()
                new_meta = {"meeting": info}
            else:
                # Speech after the user ended a conversation by hand starts a new one, however soon it comes.
                ep = conn.execute(
                    "SELECT id FROM episodes WHERE stage = 'open' AND kind = 'voice' AND NOT meta ? 'meeting' "
                    "AND started_at <= %s AND ended_at >= %s AND NOT EXISTS (SELECT 1 FROM chunks k "
                    "WHERE k.close_episode AND k.source <> %s AND k.ended_at > episodes.started_at "
                    "AND k.ended_at <= %s) ORDER BY started_at LIMIT 1 FOR UPDATE",
                    (last + gap, first - gap, meeting.SOURCE, first),
                ).fetchone()
                new_meta = {}
            if ep and weak:
                conn.execute(
                    "UPDATE episodes SET close_requested = close_requested OR %s, updated_at = now() WHERE id = %s",
                    (c["close_episode"], ep["id"]),
                )
            elif ep:
                conn.execute(
                    "UPDATE episodes SET started_at = LEAST(started_at, %s), ended_at = GREATEST(ended_at, %s), "
                    "close_requested = close_requested OR %s, updated_at = now() WHERE id = %s",
                    (first, last, c["close_episode"], ep["id"]),
                )
            elif weak:
                conn.execute(
                    "UPDATE chunks SET stage = 'no_speech', metadata = metadata || '{\"weak\": true}', error = NULL, "
                    "updated_at = now() WHERE id = %s",
                    (c["id"],),
                )
                changed = True
                continue
            else:
                ep = conn.execute(
                    "INSERT INTO episodes (started_at, ended_at, close_requested, meta) VALUES (%s, %s, %s, %s) "
                    "RETURNING id",
                    (first, last, c["close_episode"], Jsonb(new_meta)),
                ).fetchone()
            conn.execute(
                "UPDATE chunks SET stage = 'grouped', episode_id = %s, error = NULL, updated_at = now() WHERE id = %s",
                (ep["id"], c["id"]),
            )
            changed = True

        # Chunk tới không theo thứ tự có thể nối hai episode đang mở lại với nhau.
        opens = conn.execute("SELECT * FROM episodes WHERE stage = 'open' ORDER BY started_at FOR UPDATE").fetchall()
        kept: list[dict] = []
        for ep in opens:
            stream = (ep["meta"] or {}).get("meeting", {}).get("id")
            prev = next((k for k in reversed(kept) if (k["meta"] or {}).get("meeting", {}).get("id") == stream), None)
            if prev and (stream or (ep["started_at"] - prev["ended_at"] <= gap
                                    and not _ended_between(conn, prev["started_at"], ep["started_at"]))):
                prev["ended_at"] = max(prev["ended_at"], ep["ended_at"])
                prev["close_requested"] = prev["close_requested"] or ep["close_requested"]
                conn.execute("UPDATE chunks SET episode_id = %s WHERE episode_id = %s", (prev["id"], ep["id"]))
                conn.execute("DELETE FROM episodes WHERE id = %s", (ep["id"],))
                conn.execute(
                    "UPDATE episodes SET ended_at = %s, close_requested = %s, updated_at = now() WHERE id = %s",
                    (prev["ended_at"], prev["close_requested"], prev["id"]),
                )
                changed = True
            else:
                kept.append(dict(ep))

        for ep in kept:
            # The flagged chunk may hold no speech (ended after a pause), so it never joins the episode itself.
            ended = not (ep["meta"] or {}).get("meeting") and _ended_between(conn, ep["ended_at"], ep["ended_at"] + gap)
            if not (ep["close_requested"] or ended or now - ep["ended_at"] > gap):
                continue
            pending = conn.execute(
                "SELECT 1 FROM chunks WHERE stage = 'received' AND attempts < %s "
                "AND started_at <= %s AND ended_at >= %s LIMIT 1",
                (settings.max_attempts, ep["ended_at"] + gap, ep["started_at"] - gap),
            ).fetchone()
            if pending:
                continue  # còn file lân cận chưa cắt im lặng xong, chờ thêm
            members = conn.execute(
                "SELECT id, started_at, duration_s, speech_s FROM chunks WHERE episode_id = %s ORDER BY started_at",
                (ep["id"],),
            ).fetchall()
            timeline, offset = [], 0.0
            for m in members:
                timeline.append({"chunk_id": str(m["id"]), "offset_s": round(offset, 3),
                                 "duration_s": m["duration_s"], "started_at": m["started_at"].isoformat()})
                offset += m["duration_s"]
            conn.execute(
                "UPDATE episodes SET stage = 'grouped', timeline = %s, duration_s = %s, speech_s = %s, "
                "updated_at = now() WHERE id = %s",
                (Jsonb(timeline), round(offset, 3), round(sum(m["speech_s"] or 0 for m in members), 2), ep["id"]),
            )
            changed = True
    return changed


# ---------------------------------------------------------------- audio của một episode

def episode_audio(conn, ep: dict) -> np.ndarray:
    """Nối audio các chunk của episode. Mỗi chunk dài đúng bằng `duration_s` đã ghi trong timeline."""
    data_dir = get_settings().data_dir
    parts = []
    for item in ep["timeline"]:
        path = conn.execute("SELECT path FROM chunks WHERE id = %s", (item["chunk_id"],)).fetchone()["path"]
        samples = decode(data_dir / path)
        want = int(round(item["duration_s"] * SAMPLE_RATE))
        if len(samples) < want:
            samples = np.pad(samples, (0, want - len(samples)))
        parts.append(samples[:want])
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def episode_audio_range(conn, ep: dict, start_s: float, end_s: float) -> np.ndarray:
    """The same samples as episode_audio(...)[start_s:end_s], decoding only the chunks that cover them."""
    data_dir = get_settings().data_dir
    parts, offset, first = [], 0.0, None
    for item in ep["timeline"]:
        want = int(round(item["duration_s"] * SAMPLE_RATE))
        lo, hi = offset, offset + want / SAMPLE_RATE
        offset = hi
        if hi <= start_s or lo >= end_s:
            continue
        first = lo if first is None else first
        path = conn.execute("SELECT path FROM chunks WHERE id = %s", (item["chunk_id"],)).fetchone()["path"]
        samples = decode(data_dir / path)
        if len(samples) < want:
            samples = np.pad(samples, (0, want - len(samples)))
        parts.append(samples[:want])
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return cut(np.concatenate(parts), start_s - first, end_s - first)


def episode_speech(conn, ep: dict) -> list[list[float]]:
    """Các khoảng có tiếng nói, tính theo giây từ đầu audio của episode."""
    out = []
    for item in ep["timeline"]:
        speech = conn.execute("SELECT speech FROM chunks WHERE id = %s", (item["chunk_id"],)).fetchone()["speech"]
        out.extend([item["offset_s"] + s, item["offset_s"] + e] for s, e in speech or [])
    return out


def wall_time(timeline: list[dict], t: float) -> datetime:
    """Đổi giây trong audio của episode thành giờ thật."""
    chosen = timeline[0]
    for item in timeline:
        if item["offset_s"] <= t:
            chosen = item
    return datetime.fromisoformat(chosen["started_at"]) + timedelta(seconds=t - chosen["offset_s"])


# ---------------------------------------------------------------- bước 3: tách người nói

def step_diarize(conn, ep: dict) -> None:
    settings = get_settings()
    try:
        diarizer = get_diarizer(settings.diarizer)
    except DiarizerUnavailable as exc:
        raise Blocked(str(exc)) from exc

    samples = episode_audio(conn, ep)
    started = time.monotonic()
    result = diarizer.run(samples, episode_speech(conn, ep))
    built = build_turns(
        result.turns,
        merge_gap_s=settings.turn_merge_gap_s, max_s=settings.turn_max_s, min_s=settings.turn_min_s,
    )
    turns, mapping = relabel(snap_cuts(built, samples, SAMPLE_RATE))
    if (ep["meta"] or {}).get("meeting"):
        meeting.label_me(conn, ep, meeting.mic_label(conn, ep, turns))

    conn.execute("DELETE FROM segments WHERE episode_id = %s", (ep["id"],))
    conn.execute("DELETE FROM episode_speakers WHERE episode_id = %s", (ep["id"],))
    spoken: dict[str, float] = {}
    for idx, t in enumerate(turns):
        conn.execute(
            "INSERT INTO segments (episode_id, idx, speaker, start_s, end_s, started_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (ep["id"], idx, t.speaker, round(t.start, 3), round(t.end, 3), wall_time(ep["timeline"], t.start)),
        )
        spoken[t.speaker] = spoken.get(t.speaker, 0.0) + t.length
    vectors = {mapping[raw]: vec for raw, vec in result.embeddings.items() if raw in mapping}
    for label, seconds in spoken.items():
        vec = vectors.get(label)
        conn.execute(
            "INSERT INTO episode_speakers (episode_id, label, speech_s, embedding, embedding_model) "
            "VALUES (%s, %s, %s, %s::vector, %s)",
            (ep["id"], label, round(seconds, 2),
             "[" + ",".join(f"{x:.6f}" for x in vec) + "]" if vec else None,
             result.model if vec else None),
        )
    conn.execute(
        "UPDATE episodes SET stage = 'diarized', diarizer = %s, error = NULL, attempts = 0, updated_at = now() WHERE id = %s",
        (result.model or diarizer.name, ep["id"]),
    )
    log.info("episode %s: %d lượt nói, %d người, tách trong %.0f giây",
             str(ep["id"])[:8], len(turns), len(spoken), time.monotonic() - started)


# ---------------------------------------------------------------- bước 4: chuyển chữ

def transcribe_missing(episode_id, backend: AsrBackend) -> int:
    """Chuyển chữ các lượt nói chưa có chữ của `backend`. Lưu ngay từng lượt để lỗi giữa chừng không mất tiền."""
    settings = get_settings()
    with connect(autocommit=True) as conn:
        ep = conn.execute("SELECT * FROM episodes WHERE id = %s", (episode_id,)).fetchone()
        segs = conn.execute(
            """
            SELECT s.id, s.start_s, s.end_s FROM segments s
            LEFT JOIN segment_texts t ON t.segment_id = s.id AND t.backend = %s
            WHERE s.episode_id = %s AND t.segment_id IS NULL ORDER BY s.idx
            """,
            (backend.name, episode_id),
        ).fetchall()
        if not segs:
            return 0
        samples = episode_audio(conn, ep)

        # Nới mỗi lượt ra một chút để không cụt chữ đầu và cuối, nhưng không lấn sang lượt bên cạnh:
        # lấn sang thì chữ ở ranh giới bị chuyển hai lần.
        bounds = conn.execute(
            "SELECT id, start_s, end_s FROM segments WHERE episode_id = %s ORDER BY idx", (episode_id,)
        ).fetchall()
        pads: dict[int, tuple[float, float]] = {}
        for i, b in enumerate(bounds):
            before = b["start_s"] - bounds[i - 1]["end_s"] if i > 0 else settings.turn_pad_s * 2
            after = bounds[i + 1]["start_s"] - b["end_s"] if i + 1 < len(bounds) else settings.turn_pad_s * 2
            pads[b["id"]] = (min(settings.turn_pad_s, max(0.0, before) / 2),
                             min(settings.turn_pad_s, max(0.0, after) / 2))

        def work(seg: dict):
            return backend.transcribe(wav_bytes(cut(samples, seg["start_s"], seg["end_s"], *pads[seg["id"]])))

        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=max(1, settings.asr_concurrency)) as pool:
            futures = {pool.submit(work, s): s for s in segs}
            for fut in as_completed(futures):
                seg = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    errors.append(exc)
                    continue
                conn.execute(
                    """
                    INSERT INTO segment_texts (segment_id, backend, model, text, confidence)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (segment_id, backend) DO UPDATE
                    SET model = EXCLUDED.model, text = EXCLUDED.text, confidence = EXCLUDED.confidence, created_at = now()
                    """,
                    (seg["id"], backend.name, backend.model, res.text, res.confidence),
                )
        if errors:
            raise RuntimeError(f"{len(errors)}/{len(segs)} lượt nói chưa chuyển được thành chữ. Lỗi đầu: {errors[0]}")
        return len(segs)


def step_transcribe(conn, ep: dict) -> None:
    name = ep["asr_backend"] or get_settings().asr_backend
    try:
        backend = get_backend(name)
        backend.check()
    except AsrUnavailable as exc:
        raise Blocked(f"{name}: {exc}") from exc
    started = time.monotonic()
    done = transcribe_missing(ep["id"], backend)
    conn.execute(
        "UPDATE episodes SET stage = 'transcribed', asr_backend = %s, error = NULL, attempts = 0, updated_at = now() "
        "WHERE id = %s",
        (name, ep["id"]),
    )
    path = export_episode(conn, ep["id"])
    log.info("episode %s: chuyển chữ %d lượt bằng %s trong %.0f giây -> %s",
             str(ep["id"])[:8], done, name, time.monotonic() - started, path)


# ---------------------------------------------------------------- bước 5: chỉ mục nguyên văn

def _summary_window(conn, ep: dict, rows: list[dict]) -> Window:
    """Cửa sổ tóm tắt của một episode code. Chưa có tóm tắt thì nhờ LLM viết và lưu lại."""
    settings = get_settings()
    if not ep["summary"]:
        try:
            summarize.check()
        except summarize.SummaryUnavailable as exc:
            raise Blocked(str(exc)) from exc
        transcript = "\n".join(f"{r['speaker']}: {r['text']}" for r in rows)
        ep["summary"] = summarize.summarize_episode(transcript, (ep["meta"] or {}).get("project") or "?")
        conn.execute("UPDATE episodes SET summary = %s, summary_model = %s WHERE id = %s",
                     (ep["summary"], summarize.model_name(), ep["id"]))
    return Window(
        first_idx=rows[0]["idx"], last_idx=rows[-1]["idx"], started_at=ep["started_at"], ended_at=ep["ended_at"],
        speakers=sorted({r["speaker"] for r in rows}), text=describe(ep, settings.tz), kind="summary",
    )


def step_index(conn, ep: dict) -> None:
    """Cắt transcript thành cửa sổ vài lượt nói, embed từng cửa sổ, lưu vector cạnh chữ trong Postgres."""
    settings = get_settings()
    try:
        embed.check()
    except embed.EmbedUnavailable as exc:
        raise Blocked(str(exc)) from exc
    started = time.monotonic()
    if ep["kind"] == "voice":
        # Names and role labels must be settled before the text is embedded and sent to Graphiti.
        voices.identify(conn, ep)
        voices.suggest(conn, ep)
        export_episode(conn, ep["id"])   # the markdown written after transcription still has SPEAKER_xx
    rows = transcript_rows(conn, ep)
    wins = build_windows(rows, max_chars=settings.window_max_chars, overlap=settings.window_overlap)
    if ep["kind"] == "code" and rows:
        # Phiên code có thêm một cửa sổ là bản tóm tắt cả phiên: nó nêu tên dự án, file và quyết định,
        # là những thứ câu hỏi hay nhắc tới mà từng lượt nói riêng lẻ thì không.
        wins.insert(0, _summary_window(conn, ep, rows))
    vectors = embed.embed_texts([w.text for w in wins])

    conn.execute("DELETE FROM windows WHERE episode_id = %s", (ep["id"],))
    for idx, (w, vec) in enumerate(zip(wins, vectors, strict=True)):
        conn.execute(
            "INSERT INTO windows (episode_id, idx, kind, first_idx, last_idx, started_at, ended_at, speakers, text, "
            "embedding, embedding_model) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
            (ep["id"], idx, w.kind, w.first_idx, w.last_idx, w.started_at, w.ended_at, w.speakers, w.text,
             embed.vector_literal(vec), settings.embed_model),
        )
    conn.execute(
        "UPDATE episodes SET stage = 'indexed', error = NULL, attempts = 0, updated_at = now() WHERE id = %s",
        (ep["id"],),
    )
    log.info("episode %s: %d cửa sổ đã có vector (%s) trong %.0f giây",
             str(ep["id"])[:8], len(wins), settings.embed_model, time.monotonic() - started)


# ---------------------------------------------------------------- bước 6: nạp vào Graphiti

def purge_graph() -> bool:
    """Gỡ khỏi Graphiti những phần đã lỗi thời (episode bị xóa hoặc được chạy lại)."""
    with connect() as conn:
        rows = conn.execute("SELECT uuid FROM graph_parts WHERE episode_id IS NULL FOR UPDATE SKIP LOCKED").fetchall()
        if not rows:
            return False
        try:
            graph.check()
            graph.remove_parts([str(r["uuid"]) for r in rows])
        except Exception as exc:
            log.warning("chưa gỡ được %d phần cũ khỏi Graphiti: %s", len(rows), exc)
            return False
        conn.execute("DELETE FROM graph_parts WHERE uuid = ANY(%s)", ([r["uuid"] for r in rows],))
        log.info("đã gỡ %d phần cũ khỏi Graphiti", len(rows))
    return True


INGEST_LOCK = 7_316_003  # chỉ một thợ được nạp vào Graphiti tại một thời điểm


# Loại episode được nạp vào Graphiti. Bài đọc trên Chrome chỉ dừng ở bước indexed. Of the Zalo chats, only
# direct chats with enough words go in (zalo_episodes.GRAPH_FILTER).
GRAPH_KINDS = ["voice", "code", "chat"]


def run_ingest(only: list | None = None) -> bool:
    """Nạp một episode vào Graphiti. Episode cũ hơn GRAPH_SINCE được để yên ở bước indexed.

    Graphiti gộp thực thể trùng bằng cách đọc graph rồi mới ghi; hai thợ nạp cùng lúc có thể cùng tạo
    một người hay một dự án hai lần. Vì vậy dù chạy bao nhiêu thợ, bước này chỉ một thợ làm.
    """
    with connect(autocommit=True) as lock:
        if not lock.execute("SELECT pg_try_advisory_lock(%s) AS ok", (INGEST_LOCK,)).fetchone()["ok"]:
            return False
        settings = get_settings()
        try:
            # Older chats (GRAPH_CHAT_SINCE) only once nothing from GRAPH_SINCE on is waiting, so a backlog of
            # chat history never holds up today's recordings.
            return (_run_step("episodes", "indexed", step_ingest, only=only, since=settings.graph_since_at,
                              kinds=GRAPH_KINDS, where=zalo_episodes.GRAPH_FILTER)
                    or (settings.graph_chat_since_at != settings.graph_since_at
                        and _run_step("episodes", "indexed", step_ingest, only=only,
                                      since=settings.graph_chat_since_at, kinds=["chat"],
                                      where=zalo_episodes.GRAPH_FILTER)))
        finally:
            lock.execute("SELECT pg_advisory_unlock(%s)", (INGEST_LOCK,))


def step_ingest(conn, ep: dict) -> None:
    """Nạp transcript vào Graphiti, mỗi lần một phần. Lưu ngay từng phần để lỗi giữa chừng không mất tiền."""
    settings = get_settings()
    try:
        graph.check()
    except graph.GraphUnavailable as exc:
        raise Blocked(str(exc)) from exc
    if conn.execute("SELECT 1 FROM graph_parts WHERE episode_id IS NULL LIMIT 1").fetchone():
        raise Blocked("còn phần cũ chưa gỡ được khỏi Graphiti, nạp bây giờ sẽ thành hai bản")

    local = ep["started_at"].astimezone(settings.tz)
    code = ep["kind"] == "code"
    instructions = None
    if ep["kind"] == "chat":
        parts, description = zalo_episodes.graph_input(conn, ep)
        instructions = zalo_episodes.GRAPH_INSTRUCTIONS
    elif code:
        # Phiên code nạp bản tóm tắt chứ không nạp từng lời thoại: lời thoại rất dài, phần lớn là chi tiết
        # kỹ thuật không thành fact, mà mỗi phần nạp vào Graphiti tốn hàng chục nghìn token.
        summary = conn.execute(
            "SELECT first_idx, last_idx, started_at, ended_at, speakers, text FROM windows "
            "WHERE episode_id = %s AND kind = 'summary'", (ep["id"],)
        ).fetchone()
        parts = [Window(**summary, kind="summary")] if summary else []
        title = (ep["meta"] or {}).get("title")
        description = (f"Tóm tắt một phiên làm việc với Claude Code ngày {local:%d/%m/%Y}"
                       + (f', tên phiên "{title}"' if title else "") + ", do máy viết.")
    else:
        rows = [{**r, "speaker": graph.speaker_name(r["speaker"], ep["id"], known=r["known"])}
                for r in transcript_rows(conn, ep)]
        parts = build_windows(rows, max_chars=settings.graph_part_chars)
        what = (f"cuộc họp online qua {meeting.describe(ep['meta'])}" if (ep["meta"] or {}).get("meeting")
                else "hội thoại")
        description = (f"Ghi âm {what} ngày {local:%d/%m/%Y}, bắt đầu lúc {local:%H:%M} giờ Việt Nam. "
                       "Máy tự chuyển giọng thành chữ.")
    started = time.monotonic()
    totals = {"parts": 0, "nodes": 0, "edges": 0, "input": 0, "output": 0}

    with connect(autocommit=True) as side:
        done = {r["part"] for r in side.execute("SELECT part FROM graph_parts WHERE episode_id = %s", (ep["id"],))}

        def record(n: int, w, res: graph.PartResult) -> None:
            side.execute(
                "INSERT INTO graph_parts (uuid, episode_id, part, first_idx, last_idx, started_at, model, nodes, "
                "edges, llm_calls, input_tokens, output_tokens, seconds) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (res.uuid, ep["id"], n, w.first_idx, w.last_idx, w.started_at, settings.graph_llm_model, res.nodes,
                 res.edges, res.usage.calls, res.usage.input_tokens, res.usage.output_tokens, res.seconds),
            )
            totals["parts"] += 1
            totals["nodes"] += res.nodes
            totals["edges"] += res.edges
            totals["input"] += res.usage.input_tokens
            totals["output"] += res.usage.output_tokens

        graph.add_parts(
            [(n, w) for n, w in enumerate(parts) if n not in done],
            name=f"episode {str(ep['id'])[:8]}",
            description=description,
            on_done=record,
            prose=code,
            instructions=instructions,
        )
    conn.execute(
        "UPDATE episodes SET stage = 'ingested', error = NULL, attempts = 0, updated_at = now() WHERE id = %s",
        (ep["id"],),
    )
    log.info("episode %s: nạp %d phần vào Graphiti bằng %s trong %.0f giây, %d thực thể, %d fact, token vào %d ra %d",
             str(ep["id"])[:8], totals["parts"], settings.graph_llm_model, time.monotonic() - started,
             totals["nodes"], totals["edges"], totals["input"], totals["output"])


# ---------------------------------------------------------------- thư mục inbox

def scan_inbox(min_age_s: float = 5.0) -> bool:
    """Nhận các file audio đã nằm yên trong inbox. File xong chuyển sang inbox/processed."""
    settings = get_settings()
    inbox = settings.inbox_dir
    if not inbox.is_dir():
        return False
    did = False
    for path in sorted(inbox.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in AUDIO_EXTS:
            continue
        if time.time() - path.stat().st_mtime < min_age_s:
            continue  # có thể còn đang được chép vào
        try:
            result = ingest_file(path, source="inbox", close_episode=True)
            target = inbox / "processed"
            log.info("inbox: %s %s", path.name, "đã có từ trước" if result.duplicate else "đã nhận")
        except AudioError as exc:
            target = inbox / "failed"
            log.warning("inbox: bỏ qua %s: %s", path.name, exc)
        target.mkdir(exist_ok=True)
        dest = target / path.name
        if dest.exists():
            dest = target / f"{path.stem}_{int(time.time())}{path.suffix}"
        shutil.move(str(path), dest)
        did = True
    return did


# ---------------------------------------------------------------- phiên Claude Code

CODE_SCAN_EVERY_S = 20.0
_last_code_scan = 0.0


def scan_code() -> bool:
    """Đọc phần mới của các file phiên Claude Code. Chỉ nhìn lại sau mỗi vài chục giây."""
    global _last_code_scan
    if time.monotonic() - _last_code_scan < CODE_SCAN_EVERY_S:
        return False
    _last_code_scan = time.monotonic()
    return claude_code.scan() > 0


GIT_SCAN_EVERY_S = 300.0
_last_git_scan = 0.0


def scan_git() -> bool:
    """Đọc commit mới của các dự án đã chọn, mỗi vài phút một lần."""
    global _last_git_scan
    if time.monotonic() - _last_git_scan < GIT_SCAN_EVERY_S:
        return False
    _last_git_scan = time.monotonic()
    try:
        return git_log.scan() > 0
    except Exception as exc:  # git hỏng ở một repo không được chặn cả vòng lặp
        log.warning("chưa đọc được commit: %s", exc)
        return False


FORGET_EVERY_S = 3600.0
_last_forget = 0.0


def forget_strangers() -> bool:
    """Drop unnamed voices not heard for a long time, once an hour."""
    global _last_forget
    if time.monotonic() - _last_forget < FORGET_EVERY_S:
        return False
    _last_forget = time.monotonic()
    try:
        return voices.forget_strangers() > 0
    except Exception as exc:
        log.warning("could not forget old voices: %s", exc)
        return False


def step_journal() -> bool:
    """Viết nhật ký cho một ngày đã qua mà chưa có nhật ký hoặc nhật ký thiếu phiên.

    When every day is written, write the summary of a finished week that has none or an old one.
    """
    from .journal import days_due, weeks_due, write_journal, write_week

    try:
        due = days_due()
        if due:
            path = write_journal(due[0])
            log.info("nhật ký ngày %s -> %s", due[0], path)
            return True
        weeks = weeks_due()
        if not weeks:
            return False
        path = write_week(weeks[0])
    except Exception as exc:
        log.warning("chưa viết được nhật ký: %s", exc)
        return False
    log.info("week summary %s -> %s", weeks[0], path)
    return True


# ---------------------------------------------------------------- vòng lặp

def run_once() -> bool:
    """Làm một việc. Trả về False khi không còn gì làm được ngay."""
    if scan_inbox():
        return True
    if scan_code():
        return True
    if group_code():
        return True
    if scan_git():
        return True
    if mac_log.group_mac():
        return True
    if reading.group_reading():
        return True
    if zalo_episodes.group_zalo():
        return True
    if calendar.group_calendar():
        return True
    if forget_strangers():
        return True
    if _run_step("chunks", "received", step_vad):
        return True
    if step_group():
        return True
    if _run_step("episodes", "grouped", step_diarize):
        return True
    if _run_step("episodes", "diarized", step_transcribe):
        return True
    if _run_step("episodes", "transcribed", step_index):
        return True
    graph_on = get_settings().graph != "none"
    if graph_on and purge_graph():
        return True
    if step_journal():
        return True
    if graph_on and run_ingest():
        return True
    return False


def run_voice_once() -> bool:
    """Làm một việc thuộc phần audio: từ file vừa nhận tới lúc có chữ."""
    return (
        _run_step("chunks", "received", step_vad)
        or step_group()
        or _run_step("episodes", "grouped", step_diarize)
        or _run_step("episodes", "diarized", step_transcribe)
    )


def finish_memory(episode_ids: list) -> None:
    """Đưa riêng các episode này qua hai bước bộ nhớ, không đụng tới việc tồn đọng của episode khác."""
    while _run_step("episodes", "transcribed", step_index, only=episode_ids):
        pass
    if get_settings().graph != "none" and not purge_graph():
        while run_ingest(only=episode_ids):
            pass


def drain(max_steps: int = 100_000, run_s: float | None = None) -> int:
    """Làm cho tới khi hết việc làm được ngay. Trả về số việc đã làm.

    With `run_s`, take no new work after that many seconds and leave the rest to the next run.
    """
    deadline = None if run_s is None else time.monotonic() + run_s
    steps = 0
    while steps < max_steps and run_once():
        steps += 1
        if deadline is not None and time.monotonic() >= deadline:
            log.info("worker has run for %.0f s, leaving the rest to the next run", run_s)
            break
    return steps


def start_watchdog(kill_s: float, exit: Callable[[int], None] | None = None) -> threading.Timer:
    """Kill this process if it is still alive after `kill_s` seconds.

    launchd starts no new `worker --once` while the old one lives, so one call that never returns (a stalled
    network request) would otherwise stop all processing until someone notices. The stacks of all threads go
    to stderr (the worker log) first, to show where it was stuck. Rows it held are unlocked when its database
    connections drop, and their transactions roll back.
    """
    def fire() -> None:
        log.error("worker still running after %.0f s, a step is stuck; exiting so the next run can start", kill_s)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        for handler in logging.getLogger().handlers + log.handlers:
            handler.flush()
        (exit or os._exit)(3)

    timer = threading.Timer(kill_s, fire)
    timer.daemon = True
    timer.start()
    return timer


def run_forever(poll_s: float = 3.0) -> None:
    log.info("thợ bắt đầu chạy, %.0f giây nhìn sổ một lần", poll_s)
    while True:
        try:
            if not run_once():
                time.sleep(poll_s)
        except KeyboardInterrupt:
            log.info("thợ dừng")
            return
        except Exception:
            log.exception("lỗi ngoài dự kiến, nghỉ rồi thử lại")
            time.sleep(poll_s * 3)
