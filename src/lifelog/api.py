"""Cửa nhận dữ liệu qua HTTP, trả lời ngay, không chờ xử lý.

POST /chunks nhận audio kèm metadata. POST /events nhận sự kiện từ các nguồn không phải audio.
GET /speakers is the page to name voices (product 6).
"""
from __future__ import annotations

import json
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import resources

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .audio import AudioError
from .config import get_settings
from .db import connect, init_db
from .events import store_events
from .ingest import ingest_file, parse_metadata, parse_time


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="lifelog", version="0.1.0", lifespan=lifespan)


def require_token(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().api_token
    if not token:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(given, token):
        raise HTTPException(status_code=401, detail="sai hoặc thiếu token")


@app.get("/health")
def health() -> dict:
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.post("/chunks", dependencies=[Depends(require_token)])
def post_chunk(audio: UploadFile = File(...), metadata: str = Form(default="{}")):
    """Nhận một file audio.

    `metadata` là JSON. Các trường server hiểu: `started_at` (ISO 8601), `client_id` (mã chống gửi trùng
    do thiết bị đặt), `source`, `close_episode` (true nếu đây là file cuối của cuộc hội thoại).
    Mọi trường khác được giữ nguyên.
    """
    settings = get_settings()
    try:
        meta = parse_metadata(metadata)
        started_at = parse_time(meta.pop("started_at", None))
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"metadata không hợp lệ: {exc}") from exc
    client_id = meta.pop("client_id", None)
    source = str(meta.pop("source", "upload"))
    close_episode = bool(meta.pop("close_episode", False))

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.tmp_dir / f"upload_{uuid.uuid4().hex}"
    limit = settings.max_upload_mb * 1024 * 1024
    try:
        size = 0
        with tmp.open("wb") as out:
            while block := audio.file.read(1 << 20):
                size += len(block)
                if size > limit:
                    raise HTTPException(status_code=413, detail=f"file lớn hơn {settings.max_upload_mb} MB")
                out.write(block)
        if size == 0:
            raise HTTPException(status_code=400, detail="file rỗng")
        try:
            result = ingest_file(
                tmp, source=source, started_at=started_at, metadata=meta,
                client_id=str(client_id) if client_id is not None else None,
                close_episode=close_episode, original_name=audio.filename, move=True,
            )
        except AudioError as exc:
            raise HTTPException(status_code=400, detail=f"không phải audio đọc được: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)

    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={"id": result.id, "duplicate": result.duplicate, "stage": result.stage},
    )


@app.get("/chunks/{chunk_id}", dependencies=[Depends(require_token)])
def get_chunk(chunk_id: uuid.UUID) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, stage, started_at, ended_at, duration_s, speech_s, episode_id, error FROM chunks WHERE id = %s",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="không có chunk này")
    return row


@app.get("/episodes/{episode_id}", dependencies=[Depends(require_token)])
def get_episode(episode_id: uuid.UUID) -> dict:
    with connect() as conn:
        ep = conn.execute(
            "SELECT id, stage, started_at, ended_at, duration_s, speech_s, diarizer, asr_backend, error "
            "FROM episodes WHERE id = %s",
            (episode_id,),
        ).fetchone()
        if ep is None:
            raise HTTPException(status_code=404, detail="không có episode này")
        ep["segments"] = conn.execute(
            """
            SELECT s.idx, s.speaker, s.start_s, s.end_s, s.started_at, t.text
            FROM segments s LEFT JOIN segment_texts t ON t.segment_id = s.id AND t.backend = %s
            WHERE s.episode_id = %s ORDER BY s.idx
            """,
            (ep["asr_backend"], episode_id),
        ).fetchall()
    return ep


class EventIn(BaseModel):
    uid: str = Field(min_length=1, description="Mã do nguồn đặt, chống gửi trùng.")
    source: str = Field(min_length=1)
    type: str = Field(min_length=1)
    ts: datetime = Field(description="Thời điểm xảy ra, ISO 8601 có múi giờ.")
    payload: dict = Field(default_factory=dict)


@app.get("/mac/today", dependencies=[Depends(require_token)])
def mac_today() -> dict:
    """Thời gian dùng Mac hôm nay theo app, cho bảng nhỏ của app KimRag Mac."""
    from .mac_log import today_summary

    return today_summary()


@app.post("/events", dependencies=[Depends(require_token)])
def post_events(events: list[EventIn]):
    """Nhận một mẻ sự kiện theo hợp đồng {ts, source, type, payload}. Gửi lại sự kiện cũ không tạo bản đôi."""
    if any(e.ts.tzinfo is None for e in events):
        raise HTTPException(status_code=400, detail="`ts` phải có múi giờ")
    with connect() as conn:
        new = store_events(conn, [e.model_dump() for e in events])
    return {"received": len(events), "new": new}


# ---------------------------------------------------------------- speakers (product 6)

class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _voice_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404 if "no such" in str(exc) or "no audio" in str(exc) else 400, detail=str(exc))


@app.get("/speakers", response_class=HTMLResponse)
def speakers_page() -> str:
    """Page to name the voices worth naming. Its data calls below need the token when one is set."""
    return resources.files("lifelog").joinpath("static/speakers.html").read_text(encoding="utf-8")


@app.get("/speakers/data", dependencies=[Depends(require_token)])
def speakers_data() -> dict:
    from . import voices

    return {"queue": voices.queue(), "people": voices.people(), "coverage": voices.coverage()}


@app.get("/speakers/{speaker_id}/voices", dependencies=[Depends(require_token)])
def speaker_voices(speaker_id: uuid.UUID) -> list[dict]:
    from . import voices

    return voices.voices_of(speaker_id)


@app.get("/speakers/{speaker_id}/clip", dependencies=[Depends(require_token)])
def speaker_clip(speaker_id: uuid.UUID) -> FileResponse:
    """A few seconds of this speaker: the saved sample if there is one, else their longest voice."""
    from . import voices

    with connect() as conn:
        sp = conn.execute("SELECT sample_path FROM speakers WHERE id = %s", (speaker_id,)).fetchone()
        voice = voices.best_voice(conn, speaker_id)
    if sp and sp["sample_path"] and (get_settings().data_dir / sp["sample_path"]).exists():
        return FileResponse(get_settings().data_dir / sp["sample_path"], media_type="audio/wav")
    if not voice:
        raise HTTPException(status_code=404, detail="no audio for this speaker")
    return voice_clip(voice["episode_id"], voice["label"])


@app.get("/voices/{episode_id}/{label}/clip", dependencies=[Depends(require_token)])
def voice_clip(episode_id: uuid.UUID, label: str) -> FileResponse:
    from . import voices

    try:
        return FileResponse(voices.clip_path(episode_id, label), media_type="audio/wav")
    except voices.VoiceError as exc:
        raise _voice_error(exc) from exc


@app.post("/speakers/{speaker_id}/name", dependencies=[Depends(require_token)])
def name_speaker(speaker_id: uuid.UUID, body: NameIn) -> dict:
    from . import voices

    try:
        return voices.name_speaker(speaker_id, body.name)
    except voices.VoiceError as exc:
        raise _voice_error(exc) from exc


@app.post("/speakers/{speaker_id}/ignore", dependencies=[Depends(require_token)])
def ignore_speaker(speaker_id: uuid.UUID) -> dict:
    from . import voices

    try:
        voices.ignore_speaker(speaker_id)
    except voices.VoiceError as exc:
        raise _voice_error(exc) from exc
    return {"ok": True}


@app.post("/voices/{episode_id}/{label}/detach", dependencies=[Depends(require_token)])
def detach_voice(episode_id: uuid.UUID, label: str) -> dict:
    from . import voices

    try:
        return {"ok": True, **voices.detach(episode_id, label)}
    except voices.VoiceError as exc:
        raise _voice_error(exc) from exc


# ---------------------------------------------------------------- iPhone recorder (product 5)

@app.get("/recorder/day", dependencies=[Depends(require_token)])
def recorder_day(day: str | None = None, source: str = "iphone") -> dict:
    """What the iPhone recorder (or with `source=mac-room` the Mac's room recording) delivered on a day
    (default today): recorded time, gaps and their reasons."""
    from datetime import date as date_type

    from .recorder import day_report

    try:
        when = date_type.fromisoformat(day) if day else datetime.now(get_settings().tz).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD") from exc
    return day_report(when, source).as_dict()


# ---------------------------------------------------------------- Mac app window (product 11)

from . import views  # noqa: E402

app.include_router(views.router(require_token))
