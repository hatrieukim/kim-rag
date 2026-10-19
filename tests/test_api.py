from __future__ import annotations

import json

from fastapi.testclient import TestClient

from lifelog.db import connect


def post(client, path, name="rec.m4a", metadata=None, headers=None):
    with open(path, "rb") as f:
        return client.post("/chunks", files={"audio": (name, f, "audio/mp4")},
                           data={"metadata": json.dumps(metadata or {})}, headers=headers or {})


def test_post_chunks_idempotent(env, audio_dir):
    from lifelog.api import app

    with TestClient(app) as client:
        meta = {"started_at": "2026-10-12T09:00:00+07:00", "client_id": "iphone-000123",
                "battery": 0.8, "wifi": "nha"}
        first = post(client, audio_dir / "a.m4a", metadata=meta)
        again = post(client, audio_dir / "a.m4a", metadata=meta)
        # Cùng client_id nhưng nội dung khác (ví dụ thiết bị gửi lại sau khi nén lại) vẫn là trùng.
        same_client = post(client, audio_dir / "b.m4a", metadata=meta)

        assert first.status_code == 201 and first.json()["duplicate"] is False
        assert again.status_code == 200 and again.json()["duplicate"] is True
        assert same_client.status_code == 200
        assert first.json()["id"] == again.json()["id"] == same_client.json()["id"]

        got = client.get(f"/chunks/{first.json()['id']}").json()
        assert got["stage"] == "received" and got["started_at"].startswith("2026-10-12T02:00:00")

    with connect() as conn:
        row = conn.execute("SELECT count(*) AS n, max(metadata::text) AS m FROM chunks").fetchone()
    assert row["n"] == 1
    assert json.loads(row["m"])["wifi"] == "nha"     # trường lạ được giữ nguyên
    assert not list((env.data / "tmp").glob("upload_*"))  # không để lại file tạm


def test_tu_choi_file_khong_phai_audio_va_metadata_hong(env, audio_dir):
    from lifelog.api import app

    with TestClient(app) as client:
        assert post(client, audio_dir / "not_audio.m4a").status_code == 400
        with open(audio_dir / "a.m4a", "rb") as f:
            bad = client.post("/chunks", files={"audio": ("a.m4a", f, "audio/mp4")}, data={"metadata": "{khong phai json"})
        assert bad.status_code == 400
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM chunks").fetchone()["n"] == 0


def test_token_bat_buoc_khi_da_dat(env, audio_dir, monkeypatch):
    from lifelog.api import app
    from lifelog.config import get_settings

    monkeypatch.setenv("API_TOKEN", "bi-mat-thu")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert post(client, audio_dir / "a.m4a").status_code == 401
        assert post(client, audio_dir / "a.m4a", headers={"Authorization": "Bearer sai"}).status_code == 401
        assert post(client, audio_dir / "a.m4a", headers={"Authorization": "Bearer bi-mat-thu"}).status_code == 201
        assert client.get("/health").status_code == 200
