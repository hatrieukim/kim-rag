"""MCP server mỏng của lifelog: cửa duy nhất để agent hỏi bộ nhớ.

Agent chỉ nói chuyện với server này, không gọi thẳng Graphiti hay Postgres, nên đổi kho bên dưới
không ảnh hưởng tới agent. Tool khai báo bằng `@server.tool()` ngay trong file này; phần việc thật
nằm ở các module khác để lệnh `lifelog` dùng chung.
"""
from __future__ import annotations

from importlib.metadata import version
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

server = MCPServer(
    "lifelog",
    title="Lifelog",
    description="Bộ nhớ cá nhân dựng từ ghi âm hội thoại hằng ngày.",
    instructions=(
        "The user's personal memory, built from: everyday conversations recorded on the iPhone (or by the Mac's "
        "microphone while the iPhone is off) and online "
        "meetings recorded on the Mac, both turned into text by machine; Claude Code sessions in selected projects; "
        "pages read in Chrome; time spent in apps on the Mac; and the calendar. "
        "Use `search` to find what the user or someone else said, promised or decided, or what was done, fixed or "
        "settled in a project on some day. Use `summary` for what happened on a day or in a week, `people` for "
        "everything known about one person, and `timeline` for how a topic or decision changed over time. "
        "Most content is in Vietnamese. Text from recordings may be misspelled, especially names."
    ),
    version=version("lifelog"),
)


@server.tool()
def search(
    query: Annotated[str, Field(description=(
        "Điều cần tìm, viết bằng tiếng Việt như một câu nói bình thường. Tìm theo nghĩa, không cần trùng chữ. "
        "Đừng nhét ngày tháng hay tên người nói vào đây, dùng các tham số bên dưới."
    ))],
    since: Annotated[str | None, Field(description=(
        "Chỉ lấy những gì được nói từ mốc này. Dạng 2026-10-13 hoặc 2026-10-13T09:30, giờ Việt Nam."
    ))] = None,
    until: Annotated[str | None, Field(description=(
        "Chỉ lấy những gì được nói trước mốc này. Ghi một ngày trơn như 2026-10-13 thì tính hết ngày đó."
    ))] = None,
    speakers: Annotated[list[str] | None, Field(description=(
        "Only passages where these people speak, by name (e.g. \"Kim\"). Voices are recognised across recordings "
        "once the user has named them. Unnamed voices carry a role label (e.g. \"nhân viên tổng đài\") or "
        "SPEAKER_01, which only mean something inside one episode."
    ))] = None,
    limit: Annotated[int, Field(description="Số kết quả tối đa cho mỗi phần.", ge=1, le=20)] = 5,
) -> str:
    """Tìm trong bộ nhớ của người dùng: hội thoại đã ghi âm và các phiên làm việc với Claude Code. Trả về hai phần.

    Nguyên văn: các đoạn gần nghĩa nhất với câu hỏi, có giờ, người nói, nguồn (ghi âm hay dự án nào), và độ gần
    từ 0 tới 1. Độ gần dưới khoảng 0,5 thường là không liên quan. Với phiên Claude Code, đoạn ghi "tóm tắt cả phiên"
    là bản tóm tắt do máy viết; các đoạn khác là lời thật của người dùng và của Claude trong phiên đó.
    Hỏi việc đã làm ở một dự án thì nêu tên dự án trong `query` và dùng `since`, `until` để khoanh ngày.

    Fact: những điều đã được chắt lọc từ hội thoại (ai, việc gì, ở đâu, quan hệ), kèm thời gian fact đó đúng.
    Fact ghi "ĐÃ HẾT ĐÚNG" là điều từng đúng nhưng về sau đã đổi. Hội thoại không nhắc tên riêng nào thì
    thường không có fact, chỉ có nguyên văn.

    Mỗi kết quả ghi mã episode và số lượt nói để dẫn nguồn. Không thấy kết quả phù hợp thì nói là không
    tìm thấy, đừng đoán.
    """
    from .search import render
    from .search import search as run

    try:
        return render(run(query, since=since, until=until, speakers=speakers, limit=limit))
    except ValueError as exc:
        # Tham số sai: nói rõ cho agent để nó tự sửa và gọi lại.
        raise ToolError(str(exc)) from exc


@server.tool()
def summary(
    date: Annotated[str | None, Field(description=(
        "A day as 2026-10-13. Defaults to yesterday. With period \"week\", any day of the week (Monday to Sunday)."
    ))] = None,
    period: Annotated[Literal["day", "week"], Field(description="\"day\" or \"week\".")] = "day",
    detail: Annotated[bool, Field(description=(
        "Day only. False lists each Claude Code session by time, title and episode; true adds the full summary "
        "of every session, which can be long."
    ))] = False,
) -> str:
    """What the user did on one day or in one week, written from all sources: Claude Code sessions by project,
    recorded conversations and online meetings, the calendar, time spent in apps on the Mac, and pages read.

    A day starts with an overview paragraph, then one section per source. A week has a summary by theme (main
    work, results, people met, open items) and the overview of each day. Today and the current week are written
    from the data so far. Every conversation and session carries its episode id, to look it up with `search`.
    """
    from datetime import datetime, timedelta

    from .config import get_settings
    from .journal import day_summary, week_summary
    from .search import parse_when

    try:
        day = parse_when(date).date() if date else datetime.now(get_settings().tz).date() - timedelta(days=1)
        return week_summary(day) if period == "week" else day_summary(day, detail=detail)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@server.tool()
def people(
    name: Annotated[str, Field(description=(
        "The person's name as the user calls them, e.g. \"Tuấn\" or \"anh Tuấn\". A part of a name works when "
        "it matches whole words."
    ))],
) -> str:
    """Everything known about one person: how often and when their voice was recorded and who else was there,
    the latest conversations with them (with a short summary), calendar meetings they were invited to, chats with
    them on the Mac, their direct chats with the user on Zalo (how much, the groups they write in, the latest
    days with what each was about), what the memory has gathered about them, and the newest facts that involve
    them. Their Zalo name also finds them.

    When several people match, the answer lists their names: call again with one of them.
    """
    from .people import profile

    try:
        return profile(name)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


@server.tool()
def timeline(
    topic: Annotated[str, Field(description=(
        "The topic, decision or project, in plain words, e.g. \"Redis\" or \"notification bell in the admin page\". Do not put dates here; use since and until."
    ))],
    since: Annotated[str | None, Field(description="Only from this point: 2026-10-13 or 2026-10-13T09:30.")] = None,
    until: Annotated[str | None, Field(description="Only before this point. A plain day counts the whole day.")] = None,
    limit: Annotated[int, Field(description="Maximum number of facts.", ge=1, le=40)] = 20,
) -> str:
    """How a topic changed over time, oldest first, grouped by day: the facts about it in the order they became
    true, including those marked ĐÃ HẾT ĐÚNG (true once, replaced later), next to the passages where it was
    discussed. Use it for "how did we end up deciding X" or "what happened with Y over the last weeks".

    Facts are picked by closeness in meaning, so a few may be only loosely related: ignore those. Each entry
    carries its episode id and turn numbers for citing.
    """
    from .timeline import timeline as run

    try:
        return run(topic, since=since, until=until, limit=limit)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    """Chạy qua stdio. Không in gì ra stdout ngoài giao thức MCP."""
    server.run("stdio")
