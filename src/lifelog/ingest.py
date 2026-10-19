"""Cửa nhận file: cất audio vào raw/YYYY/MM/DD và ghi một dòng `received` vào sổ.

Gửi cùng một file hai lần không tạo bản ghi đôi. Trùng được nhận ra theo nội dung file
(sha256) hoặc theo `client_id` mà thiết bị tự đặt.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from .audio import probe
from .config import get_settings
from .db import connect


@dataclass
class IngestResult:
    id: str
    duplicate: bool
    stage: str
    path: str


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_settings().tz)
    return dt


def ingest_file(
    src: Path,
    *,
    source: str = "cli",
    started_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    client_id: str | None = None,
    close_episode: bool = False,
    original_name: str | None = None,
    move: bool = False,
) -> IngestResult:
    """Nhận một file audio. `move=True` thì chuyển file đi thay vì chép."""
    settings = get_settings()
    src = Path(src)
    metadata = dict(metadata or {})
    digest = sha256_of(src)

    with connect() as conn:
        existing = conn.execute(
            "SELECT id, stage, path FROM chunks WHERE sha256 = %s OR (%s::text IS NOT NULL AND client_id = %s)",
            (digest, client_id, client_id),
        ).fetchone()
        if existing:
            if move:
                src.unlink(missing_ok=True)
            return IngestResult(str(existing["id"]), True, existing["stage"], existing["path"])

        duration, created = probe(src)

        if started_at is not None:
            time_source = "metadata"
        elif created is not None:
            started_at, time_source = created, "file_tag"
        else:
            # Không có gì tốt hơn: coi lúc file được ghi xong là lúc kết thúc.
            mtime = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc)
            started_at, time_source = mtime - timedelta(seconds=duration), "file_mtime"
        ended_at = started_at + timedelta(seconds=duration)
        metadata.setdefault("time_source", time_source)

        local = started_at.astimezone(settings.tz)
        ext = (Path(original_name or src.name).suffix or ".bin").lower()
        rel = Path("raw") / f"{local:%Y}" / f"{local:%m}" / f"{local:%d}" / f"{local:%H%M%S}_{digest[:12]}{ext}"
        dest = settings.data_dir / rel

        row = conn.execute(
            """
            INSERT INTO chunks (sha256, client_id, source, original_name, path, started_at, ended_at,
                                duration_s, metadata, close_episode)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id, stage
            """,
            (digest, client_id, source, original_name or src.name, str(rel), started_at, ended_at,
             duration, Jsonb(metadata), close_episode),
        ).fetchone()
        if row is None:
            # Một yêu cầu khác vừa ghi cùng file này trong lúc ta đang kiểm tra.
            other = conn.execute(
                "SELECT id, stage, path FROM chunks WHERE sha256 = %s OR client_id = %s", (digest, client_id)
            ).fetchone()
            if move:
                src.unlink(missing_ok=True)
            return IngestResult(str(other["id"]), True, other["stage"], other["path"])

        # Cất file trước khi commit: nếu cất hỏng thì dòng trong sổ cũng bị hủy theo.
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        if move:
            shutil.move(str(src), tmp)
        else:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        return IngestResult(str(row["id"]), False, row["stage"], str(rel))


def parse_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("metadata phải là một object JSON")
    return data
