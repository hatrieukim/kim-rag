"""Product 11: the JSON the Mac app window reads."""
from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from lifelog.pipeline import drain

from test_zalo import T0, VN, sensor_events

DAY = T0.astimezone(VN).date().isoformat()
READ = {"uid": "chrome:read:1", "source": "chrome", "type": "read", "ts": T0.isoformat(),
        "payload": {"url": "https://example.com/a", "title": "Bài A", "domain": "example.com", "site": "Example",
                    "text": "Một bài viết đủ dài để thành episode. " * 20, "chars": 800}}


def test_day_lists_episodes_and_zalo_chats(env):
    from lifelog.api import app

    with TestClient(app) as client:
        client.post("/events", json=[READ] + sensor_events())
        drain()
        day = client.get("/app/day", params={"day": DAY}).json()
        [ep] = day["episodes"]
        assert ep["kind"] == "read" and ep["title"] == "Bài A" and ep["subtitle"] == "Example"
        chats = {c["id"]: c for c in day["zalo"]}
        assert chats["200"]["name"] == "Anh Nam" and chats["200"]["messages"] == 3
        assert chats["901"]["skipped"]
        assert day["weekday"] == "Thứ hai" and day["recorder"]["day"] == DAY

        detail = client.get(f"/app/episodes/{ep['id']}").json()
        assert detail["url"] == "https://example.com/a" and detail["turns"] and not detail["has_audio"]
        assert client.get(f"/app/episodes/{ep['id']}/turns/0/audio").status_code == 404

        # The other day has nothing; a day not come yet is refused.
        assert client.get("/app/day", params={"day": (T0 - timedelta(days=3)).date().isoformat()}).json()["episodes"] == []
        assert client.get("/app/day", params={"day": "2999-01-01"}).status_code == 400


def test_zalo_chat_leaves_out_messages_taken_back(env):
    from lifelog.api import app

    with TestClient(app) as client:
        client.post("/events", json=sensor_events())
        chat = client.get("/app/zalo/200", params={"day": DAY}).json()
        counted = client.get("/app/zalo/901", params={"day": DAY}).json()

    assert chat["name"] == "Anh Nam"
    assert [m["text"] for m in chat["messages"]] == ["Thứ Sáu anh gửi báo giá nhé", "Dạ em nhận, cảm ơn anh"]
    assert [m["is_self"] for m in chat["messages"]] == [False, True]
    # A group that is only counted: the messages are there, never who wrote what.
    assert counted["messages"] and all(m["skipped"] and m["text"] is None and m["from_name"] is None
                                       for m in counted["messages"])


def test_status_reports_checks_and_queue(env, monkeypatch):
    from lifelog import doctor
    from lifelog.api import app

    monkeypatch.setattr(doctor, "run", lambda: doctor.Report(
        [doctor.Check("", True, "Postgres", "17"), doctor.Check("Hoạt động trên Mac", False, "Zalo", "down",
                                                               required=False)], {}, "/data"))
    with TestClient(app) as client:
        status = client.get("/app/status").json()
    assert status["ok"] is True                       # Zalo being down does not fail the pipeline
    assert [c["label"] for c in status["checks"]] == ["Postgres", "Zalo"]
    assert status["chunks"] == {} and status["failed"] == []


def test_window_routes_need_the_token(env, monkeypatch):
    from lifelog.api import app
    from lifelog.config import get_settings

    monkeypatch.setenv("API_TOKEN", "secret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        assert client.get("/app/day").status_code == 401
        assert client.get("/app/day", headers={"Authorization": "Bearer secret"}).status_code == 200
