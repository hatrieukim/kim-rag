"""Product 10, round 3: direct chats go into the graph, Zalo people are linked to voices, `people` shows Zalo."""
from __future__ import annotations

from typer.testing import CliRunner

from lifelog import zalo_episodes
from lifelog.db import connect
from lifelog.people import profile
from lifelog.pipeline import drain

from test_zalo_episodes import DAY, at, contact, msg, put

LONG = ("Chiều nay em bận họp tới năm giờ, anh ghé hiệu sách lấy giúp em hai cuốn đã đặt được không? "
        "Xong anh qua đón em ở công ty luôn nhé, thứ Năm tuần sau mình đi xem triển lãm lúc 9 giờ sáng.")


def voice(name: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO speakers (name, status) VALUES (%s, 'named')", (name,))


def chats() -> None:
    put([contact("200", "Thu Hà"), contact("300", "Anh Nam"), contact("900", "Kế toán", "group"),
         msg("1", at(9), LONG, sender="Thu Hà"),
         msg("2", at(9.1), "Ok để anh lấy sách rồi qua đón em", self_=True),
         msg("3", at(10), "Mai 9h nhé", thread="300", sender="Anh Nam", sender_id="300"),
         msg("4", at(11), LONG, thread="900", kind="group", sender="Thu Hà", sender_id="200")])


def stages() -> dict[str, str]:
    with connect() as conn:
        return {f"{r['meta']['thread_kind']} {r['meta']['title']}": r["stage"] for r in conn.execute(
            "SELECT meta, stage FROM episodes WHERE kind = 'chat'")}


def test_only_direct_chats_with_enough_words_go_into_the_graph_under_the_linked_voice(env):
    voice("Hà")
    chats()
    assert CliRunner().invoke(__import__("lifelog.cli", fromlist=["app"]).app,
                              ["zalo-link", "Thu Hà", "Hà"]).exit_code == 0
    drain()

    assert stages() == {"user Thu Hà": "ingested", "user Anh Nam": "indexed", "group Kế toán": "indexed"}
    [part] = env.graph.added
    assert part["instructions"] == zalo_episodes.GRAPH_INSTRUCTIONS
    assert part["text"].startswith("Hà: Chiều nay em bận họp") and "\nKim: Ok để anh lấy sách" in part["text"]
    assert 'giữa Kim và Hà ngày 05/10/2026 (tên trên Zalo: "Thu Hà")' in part["description"]


def test_people_finds_a_person_by_voice_or_zalo_name_and_shows_their_chats(env):
    voice("Hà")
    chats()
    drain()
    # Not linked yet: the Zalo name finds the chats, the voice name only the voice.
    page = profile("Thu Hà")
    assert page.startswith("# Thu Hà\n") and "## Zalo" in page
    assert 'Chat riêng với "Thu Hà": 2 tin trong 1 ngày (1 tin Kim gửi)' in page
    assert "Viết trong các nhóm: Kế toán (1 tin)." in page
    assert "## Zalo" not in profile("Hà")

    from lifelog import zalo_people
    with connect() as conn:
        zalo_people.link(conn, "thu hà", "hà")
    for asked in ("Hà", "Thu Hà"):
        page = profile(asked)
        assert page.startswith("# Hà (trên Zalo: Thu Hà)\n"), asked
        assert "## Zalo" in page and "## Các ngày chat gần nhất" in page

    assert "## Zalo" in profile("Anh Nam")                       # on Zalo only, no voice


def test_zalo_link_lists_people_with_suggestions_and_links_by_hand(env):
    from lifelog.cli import app as cli

    voice("Hà")
    voice("Nam acme")
    chats()
    out = CliRunner().invoke(cli, ["zalo-link"])
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert any("Thu Hà" in line and "maybe: Hà" in line for line in lines)
    assert any("Anh Nam" in line and "maybe: Nam acme" in line for line in lines)

    out = CliRunner().invoke(cli, ["zalo-link", "Nam", "Hà"])
    assert out.exit_code == 0 and 'Linked Zalo "Anh Nam" to the voice "Hà"' in out.output
    out = CliRunner().invoke(cli, ["zalo-link", "Anh Nam", "--remove"])
    assert out.exit_code == 0 and 'Unlinked "Anh Nam"' in out.output
    out = CliRunner().invoke(cli, ["zalo-link", "Không Ai", "Hà"])
    assert out.exit_code == 1 and 'no Zalo person called "Không Ai"' in out.output
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM zalo_links").fetchone()["n"] == 0


def test_older_chats_go_in_from_their_own_day_after_everything_newer(env, monkeypatch):
    from lifelog.config import get_settings

    put([contact("200", "Thu Hà"), msg("1", at(9), LONG, sender="Thu Hà"),
         msg("2", at(9.1), "Ok để anh lấy sách rồi qua đón em", self_=True)])
    monkeypatch.setenv("GRAPH_SINCE", "2026-10-06")                  # the chat day is older than this
    get_settings.cache_clear()
    drain()
    assert stages() == {"user Thu Hà": "indexed"}

    monkeypatch.setenv("GRAPH_CHAT_SINCE", "2026-07-15")
    get_settings.cache_clear()
    drain()
    assert stages() == {"user Thu Hà": "ingested"}
