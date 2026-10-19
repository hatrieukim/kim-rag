"""Sự kiện từ các nguồn không phải audio. Mọi nguồn dùng chung hợp đồng {ts, source, type, payload}."""
from __future__ import annotations

from datetime import datetime

from psycopg.types.json import Jsonb


def store_events(conn, events: list[dict]) -> int:
    """Ghi các sự kiện vào sổ, bỏ qua sự kiện đã có (cùng `uid`). Trả về số sự kiện mới.

    Mỗi sự kiện: `uid` (mã chống gửi trùng do nguồn đặt), `source`, `type`, `ts`, `payload`.
    """
    new = 0
    for e in events:
        ts = e["ts"] if isinstance(e["ts"], datetime) else datetime.fromisoformat(str(e["ts"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError(f"sự kiện {e['uid']}: `ts` thiếu múi giờ")
        done = conn.execute(
            "INSERT INTO events (uid, source, type, ts, payload) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (uid) DO NOTHING",
            (str(e["uid"]), str(e["source"]), str(e["type"]), ts, Jsonb(e.get("payload") or {})),
        )
        new += done.rowcount
    return new
