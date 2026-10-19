"""Cắt transcript của một episode thành các cửa sổ vài lượt nói liền nhau.

Dùng cho cả hai kho: cửa sổ nhỏ để embed nguyên văn vào Postgres, cửa sổ lớn làm từng phần nạp
vào Graphiti.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Window:
    first_idx: int
    last_idx: int
    started_at: datetime
    ended_at: datetime
    speakers: list[str]
    text: str  # mỗi dòng: "Người nói: câu nói"
    kind: str = "turns"  # 'summary' khi cửa sổ là bản tóm tắt của cả episode


def transcript_rows(conn, ep: dict, first_idx: int | None = None, last_idx: int | None = None) -> list[dict]:
    """Các lượt nói đã có chữ của episode, theo thứ tự. Người đã được gán tên thì dùng tên.

    A voice without a name shows its LLM role label when it has one (`known` is false then).

    Đặt `first_idx` và `last_idx` để chỉ lấy một khúc.
    """
    return conn.execute(
        """
        SELECT s.idx, COALESCE(p.name, es.role, s.speaker) AS speaker, p.name IS NOT NULL AS known,
               s.started_at, s.end_s - s.start_s AS length_s, btrim(t.text) AS text
        FROM segments s
        JOIN segment_texts t ON t.segment_id = s.id AND t.backend = %s
        LEFT JOIN episode_speakers es ON es.episode_id = s.episode_id AND es.label = s.speaker
        LEFT JOIN speakers p ON p.id = es.speaker_id
        WHERE s.episode_id = %s AND btrim(t.text) <> ''
          AND (%s::int IS NULL OR s.idx >= %s) AND (%s::int IS NULL OR s.idx <= %s)
        ORDER BY s.idx
        """,
        (ep["asr_backend"], ep["id"], first_idx, first_idx, last_idx, last_idx),
    ).fetchall()


def make_window(rows: list[dict]) -> Window:
    """Một cửa sổ từ các lượt nói liền nhau."""
    lines: list[list[str]] = []
    for r in rows:
        # Các lượt liền nhau của cùng một người gộp thành một dòng.
        if lines and lines[-1][0] == r["speaker"]:
            lines[-1][1] += " " + r["text"]
        else:
            lines.append([r["speaker"], r["text"]])
    return Window(
        first_idx=rows[0]["idx"],
        last_idx=rows[-1]["idx"],
        started_at=rows[0]["started_at"],
        ended_at=rows[-1]["started_at"] + timedelta(seconds=rows[-1]["length_s"]),
        speakers=sorted({r["speaker"] for r in rows}),
        text="\n".join(f"{speaker}: {text}" for speaker, text in lines),
    )


def build_windows(rows: list[dict], *, max_chars: int, overlap: int = 0) -> list[Window]:
    """Gom các lượt nói liền nhau cho tới khi đủ `max_chars`. Một lượt dài hơn thế vẫn đứng riêng một cửa sổ.

    `overlap` là số lượt nói cuối của cửa sổ trước được lặp lại ở đầu cửa sổ sau, để một ý nằm vắt
    qua ranh giới vẫn nằm trọn trong ít nhất một cửa sổ.
    """
    out: list[Window] = []
    i = 0
    while i < len(rows):
        j, size = i, 0
        while j < len(rows) and (j == i or size + len(rows[j]["text"]) <= max_chars):
            size += len(rows[j]["text"])
            j += 1
        out.append(make_window(rows[i:j]))
        if j >= len(rows):
            break
        i = max(i + 1, j - overlap)
    return out
