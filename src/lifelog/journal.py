"""Nhật ký làm việc theo ngày, viết từ các bản tóm tắt phiên Claude Code, hoạt động trên Mac và trang đã đọc.

Day journals also list the recorded conversations and online meetings of the day (product 9), and each
finished week gets a week summary written from its day journals.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import calendar, mac_log, meeting, reading, summarize, zalo_episodes
from .config import get_settings
from .db import connect
from .windows import make_window, transcript_rows

# Bước xử lý mà episode code đã có bản tóm tắt.
SUMMARIZED = ("indexed", "ingested")
# Sections of a day journal that are not a project. Everything else under "## " is a project.
NOT_PROJECTS = {"Lịch", "Hội thoại", "Zalo", "Hoạt động trên Mac", "Đã đọc"}
WEEKDAYS = ("Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật")


def _bounds(day: date) -> tuple[datetime, datetime]:
    tz = get_settings().tz
    start = datetime.combine(day, time.min, tz)
    return start, start + timedelta(days=1)


def signature(episodes: int, spans: int, commits: int, pages: int = 0, meetings: int = 0, talks: int = 0,
              chats: int = 0) -> str:
    """Dấu để biết nhật ký đã cũ. Ngày chưa có dữ liệu Mac hay Chrome giữ dấu cũ là số phiên, để nhật ký cũ
    không bị viết lại."""
    if not (spans or commits or pages or meetings or talks or chats):
        return str(episodes)
    return (f"{episodes}|m{spans}|g{commits}" + (f"|r{pages}" if pages else "")
            + (f"|c{meetings}" if meetings else "") + (f"|v{talks}" if talks else "") + (f"|z{chats}" if chats else ""))


def _counts(conn, tz: str) -> dict[str, dict]:
    """Per day: how many episodes of each kind are ready or still being processed, and the other sources."""
    settings = get_settings()
    out: dict[str, dict] = {"code": {}, "code_waiting": {}, "voice": {}, "voice_waiting": {}, "chat": {}, "chat_waiting": {}}
    for r in conn.execute(
        """
        SELECT kind, (started_at AT TIME ZONE %(tz)s)::date AS day,
               count(*) FILTER (WHERE stage = ANY(%(done)s)) AS ready,
               count(*) FILTER (WHERE stage <> ALL(%(done)s) AND attempts < %(max)s) AS waiting
        FROM episodes WHERE kind IN ('code', 'voice', 'chat')
        GROUP BY 1, 2
        """,
        {"tz": tz, "done": list(SUMMARIZED), "max": settings.max_attempts},
    ):
        out[r["kind"]][r["day"]] = r["ready"]
        out[f"{r['kind']}_waiting"][r["day"]] = r["waiting"]
    out["spans"] = {r["day"]: r["n"] for r in conn.execute("SELECT day, count(*) AS n FROM mac_spans GROUP BY day")}
    out["commits"] = {r["day"]: r["n"] for r in conn.execute(
        "SELECT (ts AT TIME ZONE %s)::date AS day, count(*) AS n FROM events WHERE source = 'git' GROUP BY 1", (tz,))}
    out["pages"] = {r["day"]: r["n"] for r in conn.execute(
        "SELECT (ts AT TIME ZONE %s)::date AS day, count(*) AS n FROM events WHERE source = 'chrome' "
        "AND type IN ('visit', 'read') GROUP BY 1", (tz,))}
    out["meetings"] = calendar.count_by_day(conn, tz)
    return out


def _signature_of(counts: dict, day: date) -> str:
    return signature(counts["code"].get(day, 0), counts["spans"].get(day, 0), counts["commits"].get(day, 0),
                     counts["pages"].get(day, 0), counts["meetings"].get(day, 0), counts["voice"].get(day, 0),
                     counts["chat"].get(day, 0))


def _has_content(counts: dict, day: date) -> bool:
    # Calendar meetings alone do not make a journal; they are added to days that have other activity.
    return any(counts[k].get(day) for k in ("code", "voice", "chat", "spans", "commits", "pages"))


def days_due() -> list[date]:
    """Những ngày đã qua, dữ liệu trong ngày đã xử lý xong, mà nhật ký chưa có hoặc đã cũ."""
    settings = get_settings()
    today = datetime.now(settings.tz).date()
    with connect() as conn:
        counts = _counts(conn, settings.timezone)
        have = {r["day"]: r["sig"] for r in conn.execute(
            "SELECT day, COALESCE(signature, episodes::text) AS sig FROM journals")}
        # Sự kiện chưa gom nghĩa là ngày đó còn dữ liệu chưa tính.
        pending = {r["day"] for r in conn.execute(
            "SELECT DISTINCT (ts AT TIME ZONE %s)::date AS day FROM events WHERE stage = 'received'",
            (settings.timezone,))}
    due = []
    days = set().union(*(counts[k] for k in ("code", "voice", "chat", "spans", "commits", "pages")))
    for day in sorted(days):
        waiting = counts["code_waiting"].get(day) or counts["voice_waiting"].get(day) or counts["chat_waiting"].get(day)
        if day >= today or waiting or day in pending or not _has_content(counts, day):
            continue
        if have.get(day) != _signature_of(counts, day):
            due.append(day)
    return due


def _origin(ep: dict) -> str:
    return f"họp online qua {meeting.describe(ep['meta'])}" if (ep["meta"] or {}).get("meeting") else "ghi âm"


def _conversations(day: date) -> list[str]:
    """The "Hội thoại" section: one heading and a few sentences per recorded conversation or meeting.

    Summaries are written once and kept on the episode; naming a speaker sends the episode back through
    indexing, which clears the summary so it is written again with the new name.
    """
    settings = get_settings()
    lo, hi = _bounds(day)
    with connect() as conn:
        eps = conn.execute(
            "SELECT id, started_at, ended_at, meta, summary, asr_backend FROM episodes WHERE kind = 'voice' "
            "AND stage = ANY(%s) AND started_at >= %s AND started_at < %s ORDER BY started_at",
            (list(SUMMARIZED), lo, hi),
        ).fetchall()
        talks = [(e, transcript_rows(conn, e)) for e in eps]
    out: list[str] = []
    for ep, rows in talks:
        if not rows:
            continue
        window = make_window(rows)
        what = _origin(ep)
        if len(window.text) <= summarize.SHORT_CONVERSATION:
            body = "\n".join(f"> {line}" for line in window.text.splitlines())
        else:
            if not ep["summary"]:
                summarize.check()
                ep["summary"] = summarize.summarize_conversation(window.text, what)
                with connect() as conn:
                    conn.execute("UPDATE episodes SET summary = %s, summary_model = %s WHERE id = %s",
                                 (ep["summary"], summarize.model_name(), ep["id"]))
            body = ep["summary"].strip()
        span = f"{ep['started_at'].astimezone(settings.tz):%H:%M}–{ep['ended_at'].astimezone(settings.tz):%H:%M}"
        who = ", ".join(window.speakers)
        out += [f"### {span} · {what} · {who} · episode `{str(ep['id'])[:8]}`", "", body, ""]
    return ["## Hội thoại", ""] + out if out else []


def render_journal(day: date) -> tuple[str, int]:
    """Nội dung nhật ký của một ngày và số phiên Claude Code trong đó. Ngày không có gì thì nội dung rỗng."""
    settings = get_settings()
    lo, hi = _bounds(day)
    with connect() as conn:
        eps = conn.execute(
            "SELECT id, started_at, ended_at, meta, summary FROM episodes WHERE kind = 'code' AND stage = ANY(%s) "
            "AND summary IS NOT NULL AND started_at >= %s AND started_at < %s ORDER BY started_at",
            (list(SUMMARIZED), lo, hi),
        ).fetchall()
        mac = mac_log.render_day(conn, day)
        read = reading.render_day(conn, day)
        cal = calendar.render_day(conn, day)
        chats = zalo_episodes.render_day(conn, day)
    talks = _conversations(day)
    if not eps and not mac and not read and not talks and not chats:
        return "", 0

    by_project: dict[str, list[dict]] = {}
    for e in eps:
        by_project.setdefault((e["meta"] or {}).get("project") or "?", []).append(e)

    def span(e: dict) -> str:
        return f"{e['started_at'].astimezone(settings.tz):%H:%M}–{e['ended_at'].astimezone(settings.tz):%H:%M}"

    body = []
    for project, sessions in by_project.items():
        body += [f"## {project}", ""]
        for e in sessions:
            title = (e["meta"] or {}).get("title")
            body += [f"### {span(e)}" + (f" · {title}" if title else "") + f" · episode `{str(e['id'])[:8]}`",
                     "", e["summary"].strip(), ""]
            files = (e["meta"] or {}).get("files") or []
            if files:
                body += ["File đã sửa: " + ", ".join(f"`{f}`" for f in files[:20])
                         + (f" và {len(files) - 20} file khác" if len(files) > 20 else ""), ""]

    overview = summarize.summarize_day(f"{day:%d/%m/%Y}", "\n".join(body), "\n".join(mac[2:] + read + cal),
                                       "\n".join(talks[2:] + chats[2:]))
    head = [f"# Nhật ký làm việc {day:%d/%m/%Y}", ""]
    if eps:
        head += [f"{len(eps)} phiên Claude Code ở {len(by_project)} dự án: {', '.join(by_project)}.", ""]
    if talks:
        head += [f"{sum(1 for line in talks if line.startswith('### '))} cuộc hội thoại hoặc cuộc họp đã ghi âm.", ""]
    if chats:
        head += [f"{sum(1 for line in chats if line.startswith('### '))} cuộc chat Zalo đáng chú ý.", ""]
    head += [overview, ""]
    return "\n".join(head + cal + talks + chats + mac + read + body).rstrip() + "\n", len(eps)


def write_journal(day: date) -> str | None:
    """Viết lại nhật ký của một ngày. Trả về đường dẫn tương đối so với data_dir, hoặc None nếu ngày đó trống."""
    settings = get_settings()
    text, count = render_journal(day)
    if not text:
        return None
    rel = f"journal/{day:%Y-%m-%d}.md"
    dest = settings.data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    with connect() as conn:
        sig = _signature_of(_counts(conn, settings.timezone), day)
        conn.execute(
            "INSERT INTO journals (day, path, episodes, model, signature) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (day) DO UPDATE SET path = EXCLUDED.path, episodes = EXCLUDED.episodes, "
            "model = EXCLUDED.model, signature = EXCLUDED.signature, generated_at = now()",
            (day, rel, count, summarize.model_name(), sig),
        )
    return rel


def compact(text: str) -> str:
    """A day journal without the long summary under each Claude Code session: headings stay, so every session
    is still listed with its time, title and episode id."""
    out: list[str] = []
    in_project = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_project = line[3:].strip() not in NOT_PROJECTS
            out.append(line)
        elif not in_project or line.startswith("### "):
            out.append(line)
    # Drop the blank lines left between headings whose body was removed.
    lines: list[str] = []
    for line in out:
        if line == "" and lines and lines[-1] == "":
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def overview(text: str) -> str:
    """The overview paragraph of a day journal: the last paragraph before the first section."""
    head = text.split("\n## ", 1)[0]
    paragraphs = [p.strip() for p in head.split("\n\n") if p.strip() and not p.startswith("# ")]
    return paragraphs[-1] if paragraphs else ""


# ---------------------------------------------------------------- tuần

def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _week_label(monday: date) -> str:
    return f"{monday:%d/%m}–{monday + timedelta(days=6):%d/%m/%Y}"


def _day_journals(conn, monday: date) -> list[dict]:
    return conn.execute(
        "SELECT day, path, generated_at FROM journals WHERE day >= %s AND day < %s ORDER BY day",
        (monday, monday + timedelta(days=7))).fetchall()


def _week_signature(days: list[dict]) -> str:
    return ",".join(f"{d['day']:%m%d}@{d['generated_at'].timestamp():.0f}" for d in days)


def weeks_due() -> list[date]:
    """Mondays of finished weeks whose day journals are all written, without a week summary or with an old one."""
    today = datetime.now(get_settings().tz).date()
    waiting = {week_start(d) for d in days_due()}
    with connect() as conn:
        mondays = sorted({week_start(r["day"]) for r in conn.execute("SELECT day FROM journals")})
        have = {r["week_start"]: r["signature"] for r in conn.execute("SELECT week_start, signature FROM week_journals")}
        sigs = {m: _week_signature(_day_journals(conn, m)) for m in mondays}
    return [m for m in mondays if m + timedelta(days=7) <= today and m not in waiting and have.get(m) != sigs[m]]


def render_week(monday: date) -> str:
    """A week summary written from the day journals of that week. Empty when the week has none."""
    settings = get_settings()
    with connect() as conn:
        days = _day_journals(conn, monday)
    texts = []
    for d in days:
        path = settings.data_dir / d["path"]
        if path.exists():
            texts.append((d["day"], path.read_text(encoding="utf-8")))
    if not texts:
        return ""
    label = _week_label(monday)
    body = summarize.summarize_week(label, "\n\n".join(compact(t) for _, t in texts))
    lines = [f"# Tuần {label}", "", f"{len(texts)} ngày có nhật ký.", "", body.strip(), "", "## Từng ngày", ""]
    for day, text in texts:
        lines += [f"### {WEEKDAYS[day.weekday()]} {day:%d/%m}", "", overview(text), ""]
    return "\n".join(lines).rstrip() + "\n"


def write_week(monday: date) -> str | None:
    """Viết lại bản tóm tắt của tuần bắt đầu từ `monday`. Trả về đường dẫn tương đối so với data_dir."""
    settings = get_settings()
    text = render_week(monday)
    if not text:
        return None
    rel = f"journal/week-{monday:%Y-%m-%d}.md"
    dest = settings.data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    with connect() as conn:
        sig = _week_signature(_day_journals(conn, monday))
        conn.execute(
            "INSERT INTO week_journals (week_start, path, signature, model) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (week_start) DO UPDATE SET path = EXCLUDED.path, signature = EXCLUDED.signature, "
            "model = EXCLUDED.model, generated_at = now()",
            (monday, rel, sig, summarize.model_name()),
        )
    return rel


# ---------------------------------------------------------------- tool `summary`

def day_summary(day: date, *, detail: bool = False) -> str:
    """The journal of one day for an agent: the saved one when it is up to date, otherwise written now.

    Today is written from the data so far and not saved, since the day is not over.
    """
    settings = get_settings()
    now = datetime.now(settings.tz)
    if day > now.date():
        raise ValueError(f"ngày {day:%d/%m/%Y} chưa tới")
    note = ""
    text = ""
    with connect() as conn:
        row = conn.execute("SELECT path, COALESCE(signature, episodes::text) AS sig FROM journals WHERE day = %s",
                           (day,)).fetchone()
        current = _signature_of(_counts(conn, settings.timezone), day)
    if day == now.date():
        text, _ = render_journal(day)
        note = f"Ngày chưa hết: bản này viết từ dữ liệu tới {now:%H:%M} và chưa được lưu."
    elif row and row["sig"] == current and (settings.data_dir / row["path"]).exists():
        text = (settings.data_dir / row["path"]).read_text(encoding="utf-8")
    else:
        rel = write_journal(day)
        text = (settings.data_dir / rel).read_text(encoding="utf-8") if rel else ""
    if not text:
        return f"Ngày {day:%d/%m/%Y} không có dữ liệu nào: không có phiên Claude Code, hội thoại, hoạt động Mac hay trang đã đọc."
    text = text if detail else compact(text)
    return f"Lưu ý: {note}\n\n{text}" if note else text


def week_summary(day: date) -> str:
    """The summary of the week (Monday to Sunday) that contains `day`. A week not over yet is written from the
    day journals so far and not saved."""
    settings = get_settings()
    today = datetime.now(settings.tz).date()
    monday = week_start(day)
    if monday > today:
        raise ValueError(f"tuần bắt đầu {monday:%d/%m/%Y} chưa tới")
    # Day journals first, so the week is written from up-to-date days.
    for d in days_due():
        if monday <= d < monday + timedelta(days=7):
            write_journal(d)
    label = _week_label(monday)
    if monday + timedelta(days=7) > today:
        text = render_week(monday)
        if text:
            text = f"Lưu ý: tuần chưa hết; bản này viết từ nhật ký các ngày đã qua (chưa gồm hôm nay) và chưa được lưu.\n\n{text}"
    else:
        with connect() as conn:
            row = conn.execute("SELECT path, signature FROM week_journals WHERE week_start = %s", (monday,)).fetchone()
            current = _week_signature(_day_journals(conn, monday))
        if row and row["signature"] == current and (settings.data_dir / row["path"]).exists():
            text = (settings.data_dir / row["path"]).read_text(encoding="utf-8")
        else:
            rel = write_week(monday)
            text = (settings.data_dir / rel).read_text(encoding="utf-8") if rel else ""
    return text or f"Tuần {label} chưa có nhật ký ngày nào."
