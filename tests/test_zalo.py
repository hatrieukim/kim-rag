"""Product 10, round 1: Zalo events from the sensor are received, counted and kept out of the way."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from lifelog import zalo
from lifelog.db import connect
from lifelog.journal import days_due
from lifelog.pipeline import drain

ZALO_DIR = Path(__file__).resolve().parent.parent / "zalo"
# 09:00 Vietnam time, 05/10/2026.
T0 = datetime(2026, 10, 5, 2, 0, tzinfo=timezone.utc)
VN = timezone(timedelta(hours=7))

# What zca-js hands the sensor, in the shapes of its UserMessage, GroupMessage and Undo classes.
SCRIPT = """
const N = require("./normalize");
const cfg = { users: true, groups: ["Kế toán"], blocked: ["666"] };
const ctx = { config: cfg, ownId: "100", names: { "900": "Kế toán", "901": "Hội cựu sinh viên" } };
const t = (min) => String(Date.parse("2026-10-05T02:00:00Z") + min * 60000);
const user = (id, from, name, min, content, self = false) => ({ type: 0, threadId: "200", isSelf: self,
  data: { msgId: id, cliMsgId: "c" + id, msgType: "webchat", uidFrom: from, dName: name, ts: t(min), content } });
const group = (gid, id, min, extra = {}) => ({ type: 1, threadId: gid, isSelf: false,
  data: { msgId: id, cliMsgId: "c" + id, msgType: "webchat", uidFrom: "300", dName: "Chị Lan", ts: t(min),
          content: "Họp lúc 3 giờ", ...extra } });
const events = [
  N.fromMessage(user("1", "200", "Anh Nam", 0, "Thứ Sáu anh gửi báo giá nhé"), ctx),
  N.fromMessage(user("2", "0", "Kim", 1, "Dạ em nhận, cảm ơn anh", true), ctx),
  N.fromMessage(user("3", "200", "Anh Nam", 2, "nhầm"), ctx),
  N.fromUndo({ threadId: "200", isSelf: false, isGroup: false,
               data: { uidFrom: "200", ts: t(3), content: { globalMsgId: 3, deleteMsg: 1 } } }, ctx),
  N.fromMessage(group("900", "10", 5), ctx),
  N.fromMessage(group("901", "11", 6, { mentions: [{ uid: "100", pos: 0, len: 4, type: 0 }] }), ctx),
  N.fromMessage(group("901", "12", 7), ctx),
  N.fromMessage({ ...user("4", "666", "Người bị chặn", 8, "bí mật"), threadId: "666" }, ctx),
  N.fromContact("user", "200", "Nam"),
  N.fromContact("user", "200", "Anh Nam"),
  N.fromContact("group", "900", "Kế toán", { members: 12 }),
  N.fromContact("group", "901", "Hội cựu sinh viên", { members: 300 }),
].filter(Boolean);
events[events.length - 4].ts = "2026-10-01T00:00:00.000Z";   // the older name
process.stdout.write(JSON.stringify(events));
"""


def sensor_events() -> list[dict]:
    """Events built by the sensor's own code, so the test covers the contract between Node and the server."""
    if not shutil.which("node") or not (ZALO_DIR / "node_modules").exists():
        pytest.skip("needs node and `npm install` in zalo/")
    out = subprocess.run(["node", "-e", SCRIPT], cwd=ZALO_DIR, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def status(state: str, minutes_ago: float, **extra) -> dict:
    return {"uid": f"zalo:status:{state}:{minutes_ago}", "source": "zalo", "type": "status",
            "ts": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(),
            "payload": {"state": state, **extra}}


def test_sensor_events_are_received_and_counted(env):
    from lifelog.api import app

    events = sensor_events()
    assert not any(e["payload"].get("thread_id") == "666" for e in events)   # blocked: no trace at all
    with TestClient(app) as client:
        assert client.post("/events", json=events).json()["new"] == len(events)
        assert client.post("/events", json=events).json()["new"] == 0          # sent twice, stored once

    drain()
    with connect() as conn:
        # Round 2: a day that is over is grouped into chat episodes (one per chat); nothing is left waiting.
        assert {r["stage"] for r in conn.execute("SELECT stage FROM events WHERE source = 'zalo'")} == {"grouped", "ignored"}
        assert sorted(r["t"] for r in conn.execute("SELECT meta->>'thread_id' AS t FROM episodes WHERE kind = 'chat'")) \
            == ["200", "900"]                         # the counted-only group 901 never becomes an episode
        counted = conn.execute("SELECT payload FROM events WHERE payload->>'thread_id' = '901' "
                               "AND type = 'message'").fetchall()
        names = zalo.names(conn)
        [day] = zalo.daily(conn, T0 - timedelta(days=1), "Asia/Ho_Chi_Minh")
        chats = {t.id: t for t in zalo.threads(conn, T0 - timedelta(days=1))}

    # A group Kim did not allow: only that messages happened, never who wrote what.
    assert all("text" not in r["payload"] and "from_name" not in r["payload"] for r in counted)

    assert names["200"]["name"] == "Anh Nam"            # the latest name wins
    assert names["901"] == {"kind": "group", "name": "Hội cựu sinh viên"}
    assert (day.direct, day.groups, day.counted, day.mine, day.undone) == (3, 1, 2, 1, 1)
    assert chats["200"].name == "Anh Nam" and chats["200"].messages == 3 and not chats["200"].skipped
    assert chats["901"].skipped and chats["901"].mentions_me == 1 and chats["901"].messages == 2
    assert chats["900"].name == "Kế toán" and not chats["900"].skipped


def test_zalo_events_do_not_hold_back_the_journal(env):
    from lifelog.api import app

    read = {"uid": "chrome:read:1", "source": "chrome", "type": "read", "ts": T0.isoformat(),
            "payload": {"url": "https://example.com/a", "title": "A", "domain": "example.com", "site": "Example",
                        "text": "Một bài viết đủ dài để thành episode. " * 20, "chars": 800}}
    with TestClient(app) as client:
        client.post("/events", json=[read] + sensor_events())
    drain()   # also writes the journals that are due
    with connect() as conn:
        written = {r["day"] for r in conn.execute("SELECT day FROM journals")}
    assert T0.astimezone(VN).date() in written
    assert days_due() == []


@pytest.mark.parametrize("events, good, words", [
    ([], None, "no events yet"),
    ([status("needs_login", 1)], False, "npm run login"),
    ([status("closed", 1, code=3000, kicked=True)], False, "browser"),
    ([status("tick", 1, connected=False)], False, "not connected"),
    ([status("tick", 30, connected=True)], False, "no word"),
    ([status("connected", 2), status("tick", 1, connected=True)], True, "listening"),
])
def test_doctor_says_what_the_sensor_is_doing(env, events, good, words):
    from lifelog.events import store_events

    with connect() as conn:
        store_events(conn, events)
        result = zalo.verdict(zalo.health(conn), datetime.now(timezone.utc), VN)
    assert result[0] is good
    assert words in result[1]


def test_zalo_command_lists_chats(env):
    from lifelog.api import app
    from lifelog.cli import app as cli

    events = sensor_events()
    now = datetime.now(timezone.utc)
    for e in events:   # move them into the last week, keeping the older name older
        older = e["type"] == "contact" and e["payload"]["name"] == "Nam"
        e["ts"] = (now - timedelta(hours=2 if older else 1)).isoformat()
    with TestClient(app) as client:
        client.post("/events", json=events + [status("tick", 1, connected=True)])
    out = CliRunner().invoke(cli, ["zalo", "--threads"])
    assert out.exit_code == 0, out.output
    assert "listening" in out.output
    assert "Anh Nam" in out.output and "id 200" in out.output
    assert "Hội cựu sinh viên (counted only)" in out.output
