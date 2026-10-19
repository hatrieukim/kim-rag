"""Product 10, round 2: Zalo messages of past days become chat episodes, searchable and in the journal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from lifelog import zalo_episodes
from lifelog.db import connect
from lifelog.events import store_events
from lifelog.pipeline import drain
from lifelog.search import search

VN = timezone(timedelta(hours=7))
DAY = datetime(2026, 10, 5, tzinfo=VN)          # a day that is over
KIM = "100"


def at(hour: float, day: datetime = DAY) -> datetime:
    return day + timedelta(hours=hour)


def msg(msg_id: str, when: datetime, text: str, *, thread="200", kind="user", self_=False, sender="Anh Nam",
        sender_id="200", **extra) -> dict:
    payload = {"thread_id": thread, "thread_kind": kind, "msg_id": msg_id, "from_id": KIM if self_ else sender_id,
               "from_name": "Kim" if self_ else sender, "is_self": self_, "msg_type": "text", "text": text, **extra}
    return {"uid": f"zalo:msg:{kind}:{thread}:{msg_id}", "source": "zalo", "type": "message", "ts": when,
            "payload": payload}


def counted(msg_id: str, when: datetime, thread="901") -> dict:
    return {"uid": f"zalo:msg:group:{thread}:{msg_id}", "source": "zalo", "type": "message", "ts": when,
            "payload": {"thread_id": thread, "thread_kind": "group", "msg_id": msg_id, "skipped": True,
                        "is_self": False, "msg_type": "text", "chars": 20, "mentions_me": False}}


def undo(msg_id: str, when: datetime, thread="200") -> dict:
    return {"uid": f"zalo:undo:user:{thread}:{msg_id}", "source": "zalo", "type": "undo", "ts": when,
            "payload": {"thread_id": thread, "thread_kind": "user", "msg_id": msg_id, "from_id": "200", "is_self": False}}


def contact(thread: str, name: str, kind="user") -> dict:
    return {"uid": f"zalo:{kind}:{thread}:{name}", "source": "zalo", "type": "contact", "ts": DAY - timedelta(days=1),
            "payload": {"thread_id": thread, "thread_kind": kind, "name": name}}


def put(events: list[dict]) -> None:
    with connect() as conn:
        store_events(conn, events)


def chat_episodes() -> list[dict]:
    with connect() as conn:
        return conn.execute("SELECT id, stage, meta, started_at FROM episodes WHERE kind = 'chat' "
                            "ORDER BY started_at").fetchall()


def turns(ep_id) -> list[tuple[str, str]]:
    with connect() as conn:
        return [(r["speaker"], r["text"]) for r in conn.execute(
            "SELECT s.speaker, t.text FROM segments s JOIN segment_texts t ON t.segment_id = s.id "
            "WHERE s.episode_id = %s ORDER BY s.idx", (ep_id,))]


def test_one_chat_on_one_day_becomes_one_searchable_episode(env):
    now = datetime.now(VN)
    put([
        contact("200", "Anh Nam"), contact("900", "Kế toán", "group"),
        msg("1", at(9), "Thứ Sáu anh gửi kế hoạch du lịch nhé"),
        msg("2", at(9.1), "Dạ em nhận, cảm ơn anh", self_=True),
        msg("3", at(20), "Anh gửi rồi đó", quote={"from_name": "Kim", "text": "Dạ em nhận, cảm ơn anh"}),
        msg("4", at(10), "Họp lúc 3 giờ", thread="900", kind="group", sender="Chị Lan", sender_id="300"),
        counted("5", at(11)),
        msg("6", now, "tin của hôm nay", thread="200"),
        {"uid": "zalo:status:x", "source": "zalo", "type": "status", "ts": at(9), "payload": {"state": "tick"}},
    ])
    drain()

    eps = chat_episodes()
    assert [(e["meta"]["thread_kind"], e["meta"]["title"], e["meta"]["messages"]) for e in eps] == [
        ("user", "Anh Nam", 3), ("group", "Kế toán", 1)]
    assert all(e["stage"] == "indexed" for e in eps)              # embedded, not sent to Graphiti
    direct = eps[0]
    assert direct["meta"]["day"] == "2026-10-05" and direct["meta"]["from_kim"] == 1
    assert turns(direct["id"]) == [
        ("Anh Nam", "Thứ Sáu anh gửi kế hoạch du lịch nhé"),
        ("Kim", "Dạ em nhận, cảm ơn anh"),
        ("Anh Nam", "[reply to Kim: Dạ em nhận, cảm ơn anh] Anh gửi rồi đó"),
    ]
    with connect() as conn:
        stages = {r["uid"]: r["stage"] for r in conn.execute("SELECT uid, stage FROM events")}
    assert stages["zalo:msg:user:200:6"] == "received"           # today waits until the day is over
    assert stages["zalo:msg:group:901:5"] == "ignored"           # a counted-only group never becomes an episode
    assert stages["zalo:status:x"] == "ignored"

    origins = {p.origin for p in search("kế hoạch du lịch thứ Sáu").passages}
    assert {"Zalo, chat với Anh Nam", "Zalo, nhóm Kế toán"} <= origins


def test_withdrawn_and_late_messages_rebuild_their_day(env):
    put([contact("200", "Anh Nam"), msg("1", at(9), "Mai 9h anh qua nhé"), msg("2", at(9.2), "gửi nhầm số tài khoản"),
         undo("2", at(9.3))])
    drain()
    [ep] = chat_episodes()
    assert [t for _, t in turns(ep["id"])] == ["Mai 9h anh qua nhé"]   # taken back before grouping

    # A message heard late (catch-up after a dropped connection) and an undo after the day was grouped.
    put([msg("3", at(10), "Nhớ mang hồ sơ", self_=True), undo("1", at(23))])
    drain()
    [ep] = chat_episodes()
    assert [t for _, t in turns(ep["id"])] == ["Nhớ mang hồ sơ"]
    with connect() as conn:
        left = {r["uid"]: r["stage"] for r in conn.execute("SELECT uid, stage FROM events WHERE type = 'message'")}
    assert left == {"zalo:msg:user:200:1": "ignored", "zalo:msg:user:200:2": "ignored", "zalo:msg:user:200:3": "grouped"}


def test_only_stickers_is_no_episode_but_a_short_promise_is(env):
    put([msg("1", at(9), "[sticker]"), msg("2", at(9.1), "[photo]", thread="201", sender="Chị Hoa", sender_id="201"),
         msg("3", at(9.2), "9h nhé", thread="202", sender="Bình", sender_id="202")])
    drain()
    assert [e["meta"]["title"] for e in chat_episodes()] == ["Bình"]   # no contact name: the sender's name


def test_journal_has_the_chats_that_matter_and_names_the_other_groups(env):
    put([contact("200", "Anh Nam"), contact("900", "Kế toán", "group"), contact("901", "Hội cựu sinh viên", "group"),
         msg("1", at(9), "Thứ Sáu anh gửi báo giá nhé"),
         msg("2", at(10), "Kim xem giúp hóa đơn", thread="900", kind="group", sender="Chị Lan", sender_id="300",
             mentions=[KIM]),
         msg("3", at(9.5), "Dạ", self_=True),
         msg("4", at(11), "Ảnh họp lớp đây", thread="901", kind="group", sender="Tuấn", sender_id="400")])
    drain()   # groups, indexes and writes the journals that are due
    with connect() as conn:
        row = conn.execute("SELECT path FROM journals WHERE day = %s", (DAY.date(),)).fetchone()
    from lifelog.config import get_settings
    text = (get_settings().data_dir / row["path"]).read_text(encoding="utf-8")
    assert "## Zalo" in text
    assert "chat với Anh Nam" in text and "> Anh Nam: Thứ Sáu anh gửi báo giá nhé" in text
    assert "nhóm Kế toán" in text                                     # Kim was named there
    assert "Nhóm khác: Hội cựu sinh viên (1 tin)." in text           # not a chat Kim took part in


def test_rebuild_drops_and_groups_again(env):
    from lifelog.cli import app as cli

    put([contact("200", "Anh Nam"), msg("1", at(9), "Thứ Sáu anh gửi báo giá nhé")])
    drain()
    [before] = chat_episodes()
    out = CliRunner().invoke(cli, ["zalo", "--rebuild"])
    assert out.exit_code == 0, out.output
    assert "Dropped 1 chat episodes" in out.output
    assert chat_episodes() == []
    drain()
    [after] = chat_episodes()
    assert after["id"] != before["id"] and after["meta"]["messages"] == 1


def test_a_message_sent_again_is_kept_once(env):
    put([contact("200", "Anh Nam"),
         msg("1", at(9), "Anh ơi gửi giúp em hóa đơn", self_=True, cli_msg_id="c1"),
         msg("2", at(9) + timedelta(seconds=10), "Anh ơi gửi giúp em hóa đơn", self_=True, cli_msg_id="c2"),
         msg("2_000", at(9) + timedelta(seconds=11), "Anh ơi gửi giúp em hóa đơn", self_=True, cli_msg_id="c2"),
         msg("3", at(9.5), "ok"), msg("4", at(10), "ok"),                # the same word an hour apart is two answers
         msg("5", at(11), "Anh ơi gửi giúp em hóa đơn", self_=True, cli_msg_id="c5")])
    drain()
    [ep] = chat_episodes()
    assert [t for _, t in turns(ep["id"])] == ["Anh ơi gửi giúp em hóa đơn", "ok", "ok", "Anh ơi gửi giúp em hóa đơn"]


def test_labels_are_not_counted_as_words():
    assert zalo_episodes.real_chars("[photo] [sticker] [call, 5 min] [file: a.pdf]") == 0
    assert zalo_episodes.real_chars("[photo] bản vẽ tầng 2") == len("bản vẽ tầng 2")
