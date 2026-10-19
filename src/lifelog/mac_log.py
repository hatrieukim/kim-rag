"""Hoạt động trên Mac (Sản phẩm 4): gom sự kiện thô thành các khoảng thời gian, rồi viết mục nhật ký.

Sự kiện thô trong `events` (source 'mac') không bao giờ bị sửa. Bảng `mac_spans` chỉ là kết quả tính ra từ
đó, theo từng ngày: đổi luật thì xóa và tính lại (`lifelog mac --rebuild`). Mọi bước ở đây là code thuần,
không gọi LLM, để giờ giấc và con số luôn đúng và giống nhau mỗi lần tính.

Các loại sự kiện chương trình ghi trên Mac gửi về:
- focus: app đang ở phía trước, kèm tiêu đề cửa sổ (`app`, `bundle`, `title`; `ignored` hoặc `hidden` khi bị lọc)
- away / back: rời máy và quay lại (`reason`: idle, lock, sleep, off, pause)
- start / stop / tick: chương trình bật, tắt, và nhịp mỗi phút cho biết nó vẫn chạy
- clipboard: chữ vừa copy (đã che bí mật trên Mac)
- shell: một lệnh terminal (`cmd`, `cwd`, `status`, `seconds`)
- sound: what the Mac plays out loud (`on`, `apps`, `output`), see `mac_sound`
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from psycopg.types.json import Jsonb

from .config import get_settings
from .db import connect

log = logging.getLogger("lifelog")

SOURCE = "mac"
SOURCES = ["mac", "git"]   # nguồn được gom ở bước này; commit chỉ cần đánh dấu đã xem
MAC_LOCK = 7_316_004
STATE_TYPES = ["focus", "away", "back", "start", "stop", "tick"]

SETTLE_S = 600       # chỉ gom tới cách hiện tại ngần này, vì khoảng cuối có thể còn đang diễn ra
TICK_S = 60          # chương trình trên Mac gửi nhịp mỗi phút
SILENCE_S = 150      # quá ngần này không nghe gì thì coi như máy tắt hoặc chương trình chết
BLIP_S = 30          # liếc sang app khác dưới ngần này rồi quay lại thì không cắt khoảng
MIN_SPAN_S = 10      # khoảng ngắn hơn thế bị bỏ
LOOKBACK = timedelta(hours=12)  # nhìn lùi để biết lúc nửa đêm đang ở app nào

IGNORED_APP = "(app bỏ qua)"

# ---------------------------------------------------------------- luật nhận diện app

BROWSERS = ("com.google.Chrome", "com.microsoft.edgemac", "com.coccoc.Coccoc", "org.mozilla.firefox",
            "com.apple.Safari", "company.thebrowser.Browser")
EDITORS = ("com.microsoft.VSCode", "com.todesktop.230313mzl4w4u92", "com.google.antigravity",
           "com.exafunction.windsurf")
TERMINALS = ("com.mitchellh.ghostty", "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable")

# Đuôi trình duyệt tự gắn vào tiêu đề tab: cờ trạng thái, tên trình duyệt, tên hồ sơ.
_BROWSER_TAIL = re.compile(
    r"(?: - (?:Audio playing|Camera recording|Microphone recording|Pinned|Muted|Network error|"
    r"Bluetooth device connected|High memory usage - [\d.,]+ [KMG]B))*"
    r" [-—] (?:Google Chrome|Microsoft Edge|Cốc Cốc|Mozilla Firefox)(?: - .*)?$"
)
_SITE_SEP = re.compile(r" (?:-|\||·|—|–|/) ")
# Số thông báo chưa đọc trang web gắn vào đầu tiêu đề tab: "(3) Facebook". Bỏ đi để cùng trang không bị tách khoảng.
_UNREAD = re.compile(r"^\(\d+\+?\) ")


@dataclass(frozen=True)
class Rule:
    """Một dòng luật: app nào, tiêu đề dạng gì thì lấy ra ngữ cảnh gì.

    `title` là regex trên tiêu đề đã bỏ đuôi trình duyệt; nhóm tên `ctx` là ngữ cảnh. Không khớp thì xét
    dòng sau. `site` lấy tên trang web ở cuối tiêu đề tab làm ngữ cảnh.
    """
    bundles: tuple[str, ...]
    app: str | None = None       # tên hiện trong nhật ký; để trống thì dùng tên app
    kind: str | None = None      # project | site | chat | host | db
    title: str | None = None
    site: bool = False


# Luật đầu tiên khớp được dùng. Thêm app mới thì thêm một dòng.
RULES: list[Rule] = [
    Rule(BROWSERS, app="Zalo", kind="chat", title=r"^(?:\(\d+\) )?Zalo\b"),   # Zalo bản web
    Rule(BROWSERS, kind="site", site=True),
    Rule(EDITORS, kind="project", title=r"^(?:● )?(?:.+? — )?(?P<ctx>[^—]+?)(?: — .+)?$"),
    Rule(("com.vng.zalo",), app="Zalo", kind="chat", title=r"^(?!Zalo$)(?P<ctx>.+)$"),
    Rule(("com.vng.zalo",), app="Zalo"),
    Rule(("com.lemonmojo.RoyalTSX.App",), app="Royal TSX", kind="host",
         title=r"^(?P<ctx>.+?)(?: [-—] Royal TSX.*)?$"),
    Rule(("org.jkiss.dbeaver.core.product",), app="DBeaver", kind="db", title=r"<(?P<ctx>[^>]+)>"),
    Rule(TERMINALS),                         # chi tiết của terminal lấy từ lệnh, không từ tiêu đề
    Rule(("com.anthropic.claudefordesktop",), app="Claude"),
]
_COMPILED = {id(r): re.compile(r.title) for r in RULES if r.title}


def clean_title(bundle: str, title: str) -> str:
    if bundle not in BROWSERS:
        return title.strip()
    return _UNREAD.sub("", _BROWSER_TAIL.sub("", title).strip())


def site_of(title: str) -> str | None:
    """Tên trang ở cuối tiêu đề tab: "lofi beats - YouTube" thì là YouTube."""
    if not title:
        return None
    return _SITE_SEP.split(title)[-1].strip() or None


def classify(payload: dict) -> tuple[str, str | None, str | None, str]:
    """(tên app, loại ngữ cảnh, ngữ cảnh, tiêu đề đã làm sạch) của một sự kiện focus."""
    if payload.get("ignored"):
        return IGNORED_APP, None, None, ""
    bundle = payload.get("bundle") or ""
    name = payload.get("app") or bundle or "?"
    title = clean_title(bundle, payload.get("title") or "")
    for rule in RULES:
        if bundle not in rule.bundles:
            continue
        ctx = None
        if rule.title:
            m = _COMPILED[id(rule)].search(title)
            if not m:
                continue
            ctx = m.groupdict().get("ctx")
        elif rule.site:
            ctx = site_of(title)
        ctx = (ctx or "").strip() or None
        return rule.app or name, rule.kind if ctx else None, ctx, title
    return name, None, None, title


# ---------------------------------------------------------------- dòng thời gian

@dataclass
class Segment:
    start: datetime
    end: datetime
    payload: dict


@dataclass
class Span:
    start: datetime
    end: datetime
    app: str
    bundle: str | None
    kind: str | None
    context: str | None
    seconds: float = 0.0
    titles: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        return self.app, self.context

    def add_title(self, title: str) -> None:
        if title and title not in self.titles and len(self.titles) < 5:
            self.titles.append(title)


def segments(events: list[dict], lo: datetime, hi: datetime) -> list[Segment]:
    """Dựng lại các quãng liền mạch ở một app (một tiêu đề), cắt theo [lo, hi).

    Rời máy, khóa máy, máy ngủ, tạm dừng ghi, hay mất nhịp quá lâu đều cắt quãng.
    """
    out: list[Segment] = []
    current: dict | None = None
    away = False
    opened: datetime | None = None
    last: datetime | None = None

    def close(at: datetime) -> None:
        nonlocal opened
        if opened is not None and current is not None:
            s, e = max(opened, lo), min(at, hi)
            if e > s:
                out.append(Segment(s, e, current))
        opened = None

    for ev in events:
        ts, kind = ev["ts"], ev["type"]
        if opened is not None and last is not None and (ts - last).total_seconds() > SILENCE_S:
            close(last + timedelta(seconds=TICK_S))
        if kind == "focus":
            close(ts)
            current = ev["payload"] or {}
            if not away:
                opened = ts
        elif kind == "away":
            close(ts)
            away = True
        elif kind == "back":
            away = False
            if current is not None and opened is None:
                opened = ts
        elif kind in ("start", "stop"):
            close(ts)
            current, away = None, False
        elif kind == "tick":
            if opened is None and not away and current is not None:
                opened = ts   # nhịp quay lại sau khi mất nhịp: máy vẫn đang được dùng
        last = ts
    if last is not None:
        close(min(hi, last + timedelta(seconds=TICK_S)))
    return out


def build_spans(segs: list[Segment]) -> list[Span]:
    """Gộp các quãng liền nhau cùng app, cùng ngữ cảnh thành một khoảng. Lần liếc ngắn sang app khác thì bỏ."""
    merged: list[Span] = []
    for seg in segs:
        app, kind, ctx, title = classify(seg.payload)
        secs = (seg.end - seg.start).total_seconds()
        last = merged[-1] if merged else None
        if last and last.key == (app, ctx) and (seg.start - last.end).total_seconds() <= BLIP_S:
            last.end = seg.end
            last.seconds += secs
            last.add_title(title)
            continue
        span = Span(seg.start, seg.end, app, seg.payload.get("bundle"), kind, ctx, secs)
        span.add_title(title)
        merged.append(span)

    out: list[Span] = []
    for span in merged:
        if (len(out) >= 2 and out[-1].seconds < BLIP_S and out[-2].key == span.key
                and (span.start - out[-2].end).total_seconds() <= 2 * BLIP_S):
            out.pop()
            host = out[-1]
            host.end = span.end
            host.seconds += span.seconds
            for t in span.titles:
                host.add_title(t)
            continue
        out.append(span)
    return [s for s in out if s.seconds >= MIN_SPAN_S]


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, get_settings().tz)
    return start, start + timedelta(days=1)


def rebuild_day(conn, day: date, until: datetime) -> int:
    """Tính lại các khoảng của một ngày từ sự kiện thô, tới mốc `until`. Trả về số khoảng."""
    lo, hi = _bounds(day)
    hi = min(hi, until)
    events = conn.execute(
        "SELECT type, ts, payload FROM events WHERE source = %s AND type = ANY(%s) AND ts >= %s AND ts < %s "
        "ORDER BY ts, id",
        (SOURCE, STATE_TYPES, lo - LOOKBACK, hi),
    ).fetchall()
    spans = build_spans(segments(events, lo, hi)) if hi > lo else []
    conn.execute("DELETE FROM mac_spans WHERE day = %s", (day,))
    for s in spans:
        conn.execute(
            "INSERT INTO mac_spans (day, started_at, ended_at, seconds, app, bundle, kind, context, titles) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (day, s.start, s.end, round(s.seconds), s.app, s.bundle, s.kind, s.context, Jsonb(s.titles)),
        )
    return len(spans)


def group_mac() -> bool:
    """Gom các sự kiện Mac đã đủ cũ. Ngày nào có sự kiện mới thì tính lại cả ngày đó."""
    settings = get_settings()
    horizon = datetime.now(timezone.utc) - timedelta(seconds=SETTLE_S)
    with connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (MAC_LOCK,)).fetchone()["ok"]:
            return False
        days = [r["day"] for r in conn.execute(
            "SELECT DISTINCT (ts AT TIME ZONE %s)::date AS day FROM events "
            "WHERE source = ANY(%s) AND stage = 'received' AND ts < %s ORDER BY 1",
            (settings.timezone, SOURCES, horizon))]
        if not days:
            return False
        total = sum(rebuild_day(conn, day, horizon) for day in days)
        conn.execute("UPDATE events SET stage = 'grouped' WHERE source = ANY(%s) AND stage = 'received' AND ts < %s",
                     (SOURCES, horizon))
    log.info("Mac: tính lại %d ngày, %d khoảng", len(days), total)
    return True


def rebuild(day: date | None = None) -> int:
    """Tính lại từ sự kiện thô, cho một ngày hoặc mọi ngày, sau khi đổi luật."""
    settings = get_settings()
    horizon = datetime.now(timezone.utc) - timedelta(seconds=SETTLE_S)
    with connect() as conn:
        days = [day] if day else [r["day"] for r in conn.execute(
            "SELECT DISTINCT (ts AT TIME ZONE %s)::date AS day FROM events WHERE source = %s ORDER BY 1",
            (settings.timezone, SOURCE))]
        return sum(rebuild_day(conn, d, horizon) for d in days)


# ---------------------------------------------------------------- mục nhật ký

MIN_APP_S = 600       # app dùng ít hơn thế gộp vào dòng "các app khác"
MIN_CONTEXT_S = 60
MAX_COMMANDS = 15
MAX_COMMITS = 20
# Lệnh đáng ghi vào nhật ký: vào máy khác, triển khai, đụng tới dịch vụ và cơ sở dữ liệu.
NOTABLE = re.compile(
    r"^(?:sudo\s+)?(?:ssh|scp|sftp|rsync|mosh|docker|docker-compose|kubectl|helm|terraform|ansible(?:-playbook)?|"
    r"psql|mysql|sqlcmd|systemctl|pm2|git\s+push|gh\s+(?:pr|release)|npm\s+publish|deploy|make\s+deploy|"
    r"\./deploy\S*|vercel|fly)\b"
)
# Mã thoát của Ctrl-C và các lệnh tìm kiếm không thấy gì: không phải lỗi đáng ghi.
_QUIET_STATUS = {0, 130, 141}
_QUIET_FAIL = re.compile(r"^(?:grep|rg|ag|test|\[|which|diff|cmp|git\s+diff|ls|cd)\b")


def duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "dưới 1 phút"
    if minutes < 60:
        return f"{minutes} phút"
    h, m = divmod(minutes, 60)
    return f"{h} giờ" + (f" {m} phút" if m else "")


def _clip(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def render_day(conn, day: date) -> list[str]:
    """Mục "Hoạt động trên Mac" của nhật ký ngày, dạng markdown. Ngày không có dữ liệu thì trả về rỗng."""
    tz = get_settings().tz
    lo, hi = _bounds(day)
    spans = conn.execute(
        "SELECT started_at, ended_at, seconds, app, kind, context FROM mac_spans WHERE day = %s ORDER BY started_at",
        (day,),
    ).fetchall()
    commands = conn.execute(
        "SELECT ts, payload FROM events WHERE source = %s AND type = 'shell' AND ts >= %s AND ts < %s ORDER BY ts",
        (SOURCE, lo, hi),
    ).fetchall()
    commits = conn.execute(
        "SELECT ts, payload FROM events WHERE source = 'git' AND ts >= %s AND ts < %s ORDER BY ts", (lo, hi),
    ).fetchall()
    if not spans and not commits:
        return []

    out = ["## Hoạt động trên Mac", ""]
    if spans:
        total = sum(s["seconds"] for s in spans)
        first, last = spans[0]["started_at"].astimezone(tz), spans[-1]["ended_at"].astimezone(tz)
        out += [f"Dùng máy {duration(total)}, từ {first:%H:%M} tới {last:%H:%M}.", ""]

        by_app: dict[str, float] = {}
        by_ctx: dict[str, dict[str, float]] = {}
        for s in spans:
            by_app[s["app"]] = by_app.get(s["app"], 0) + s["seconds"]
            if s["context"]:
                ctx = by_ctx.setdefault(s["app"], {})
                ctx[s["context"]] = ctx.get(s["context"], 0) + s["seconds"]
        out += ["Thời gian theo việc:"]
        rest, rest_names = 0.0, []
        for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1]):
            if secs < MIN_APP_S:
                rest += secs
                rest_names.append(app)
                continue
            parts = [f"{c} {duration(t)}" for c, t in sorted(by_ctx.get(app, {}).items(), key=lambda kv: -kv[1])
                     if t >= MIN_CONTEXT_S][:4]
            out.append(f"- {app}: {duration(secs)}" + (f" ({', '.join(parts)})" if parts else ""))
        if rest:
            names = ", ".join(rest_names[:4]) + (", …" if len(rest_names) > 4 else "")
            out.append(f"- Các app khác: {duration(rest)} ({names})")
        out.append("")

    if commits:
        out.append("Commit:")
        for c in commits[:MAX_COMMITS]:
            p = c["payload"]
            out.append(f"- {c['ts'].astimezone(tz):%H:%M} {p.get('project') or p.get('repo')} "
                       f"`{str(p.get('sha', ''))[:7]}` {_clip(p.get('subject') or '', 100)}")
        if len(commits) > MAX_COMMITS:
            out.append(f"- và {len(commits) - MAX_COMMITS} commit khác")
        out.append("")

    picked = []
    for c in commands:
        p = c["payload"]
        cmd = (p.get("cmd") or "").strip()
        status = int(p.get("status") or 0)
        failed = status not in _QUIET_STATUS and not _QUIET_FAIL.match(cmd)
        if NOTABLE.match(cmd) or failed:
            note = []
            if (p.get("seconds") or 0) >= 60:
                note.append(duration(p["seconds"]))
            if failed:
                note.append(f"lỗi, mã {status}")
            picked.append(f"- {c['ts'].astimezone(tz):%H:%M} `{_clip(cmd)}`" + (f" ({', '.join(note)})" if note else ""))
    if picked:
        out.append("Lệnh đáng chú ý:")
        out += picked[:MAX_COMMANDS]
        if len(picked) > MAX_COMMANDS:
            out.append(f"- và {len(picked) - MAX_COMMANDS} lệnh khác")
        out.append("")
    return out


# ---------------------------------------------------------------- số liệu hôm nay cho app trên Mac

def today_summary(now: datetime | None = None, top: int = 5) -> dict:
    """Thời gian hôm nay theo app, tính ngay từ sự kiện thô tới hiện tại, cùng luật với nhật ký."""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(settings.tz).date()
    lo, hi = _bounds(day)
    with connect() as conn:
        events = conn.execute(
            "SELECT type, ts, payload FROM events WHERE source = %s AND type = ANY(%s) AND ts >= %s AND ts < %s "
            "ORDER BY ts, id",
            (SOURCE, STATE_TYPES, lo - LOOKBACK, now),
        ).fetchall()
        commits = conn.execute("SELECT count(*) AS n FROM events WHERE source = 'git' AND ts >= %s AND ts < %s",
                               (lo, hi)).fetchone()["n"]
    spans = build_spans(segments(events, lo, min(hi, now)))
    by_app: dict[str, float] = {}
    by_ctx: dict[str, dict[str, float]] = {}
    for s in spans:
        by_app[s.app] = by_app.get(s.app, 0) + s.seconds
        if s.context:
            ctx = by_ctx.setdefault(s.app, {})
            ctx[s.context] = ctx.get(s.context, 0) + s.seconds
    apps = [{"app": app, "seconds": round(secs),
             "context": max(by_ctx[app].items(), key=lambda kv: kv[1])[0] if app in by_ctx else None}
            for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1])[:top]]
    return {
        "day": day.isoformat(),
        "total_seconds": round(sum(s.seconds for s in spans)),
        "first": spans[0].start.astimezone(settings.tz).strftime("%H:%M") if spans else None,
        "last": spans[-1].end.astimezone(settings.tz).strftime("%H:%M") if spans else None,
        "apps": apps,
        "commits": commits,
    }
