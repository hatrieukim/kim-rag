"""Lệnh `lifelog` để dùng hằng ngày."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import typer

from .config import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Lifelog: audio vào, hội thoại có người nói ra, rồi thành bộ nhớ hỏi được.")


def _setup(verbose: bool = False) -> None:
    import warnings

    # Các thư viện model in cảnh báo nội bộ không liên quan tới người dùng.
    for category in (FutureWarning, DeprecationWarning, UserWarning):
        warnings.filterwarnings("ignore", category=category)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "urllib3", "google", "google_genai", "pyannote", "torio", "speechbrain", "lightning",
                  "graphiti_core", "neo4j", "falkordb", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # SDK của Gemini cảnh báo mỗi lần trả về phần transcript có cấu trúc; chữ vẫn đầy đủ.
    logging.getLogger("google_genai.types").setLevel(logging.ERROR)


def _date(value: Optional[str]):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise typer.BadParameter(f"ngày phải có dạng YYYY-MM-DD, nhận được: {value}")


def _db_or_exit() -> None:
    import psycopg

    from .db import init_db

    try:
        init_db()
    except psycopg.OperationalError as exc:
        typer.secho(f"Không kết nối được Postgres: {str(exc).strip().splitlines()[0]}", fg="red", err=True)
        typer.echo("Chạy `docker compose up -d` ở thư mục dự án rồi thử lại.", err=True)
        raise typer.Exit(2)


def _episode_line(ep: dict) -> str:
    tz = get_settings().tz
    start, end = ep["started_at"].astimezone(tz), ep["ended_at"].astimezone(tz)
    meta = ep["meta"] or {}
    voice = f"meeting {meta['meeting'].get('app') or '?'}" if meta.get("meeting") else "ghi âm"
    what = {"code": meta.get("project") or "?", "read": f"đọc {meta.get('domain') or '?'}",
            "chat": f"Zalo {meta.get('title') or '?'}"}.get(ep["kind"], voice)
    return f"{str(ep['id'])[:8]}  {start:%Y-%m-%d %H:%M}–{end:%H:%M}  {ep['stage']:<11} {what}"


# ------------------------------------------------------------------ lệnh chính

@app.command("init-db")
def init_db_cmd() -> None:
    """Tạo các bảng trong Postgres. Chạy lại nhiều lần không sao."""
    _db_or_exit()
    typer.echo("Sổ đã sẵn sàng.")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Chạy server nhận file: POST /chunks."""
    import uvicorn

    _db_or_exit()
    uvicorn.run("lifelog.api:app", host=host, port=port, log_level="info")


@app.command()
def worker(
    once: bool = typer.Option(False, "--once", help="Làm hết việc đang có rồi thoát."),
    poll: float = typer.Option(3.0, help="Số giây giữa hai lần nhìn sổ."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Chạy người thợ: xử lý audio, đọc phiên Claude Code, nạp vào bộ nhớ, viết nhật ký ngày."""
    from .pipeline import drain, run_forever, start_watchdog

    _setup(verbose)
    settings = get_settings()
    if once:
        # A run that hangs would keep launchd from starting the next one, so it must always end.
        start_watchdog(settings.worker_kill_s)
    _db_or_exit()
    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    if once:
        typer.echo(f"Đã làm {drain(run_s=settings.worker_run_s)} việc.")
    else:
        run_forever(poll)


@app.command()
def transcribe(
    files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True, help="File audio."),
    start: Optional[str] = typer.Option(None, help="Giờ bắt đầu ghi, ISO 8601. Chỉ dùng khi gửi một file."),
    no_wait: bool = typer.Option(False, "--no-wait", help="Chỉ nhận file, để thợ chạy nền xử lý."),
    timeout: float = typer.Option(3600.0, help="Số giây tối đa chờ kết quả."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Nhận file audio và xử lý ngay, in ra đường dẫn file markdown kết quả.

    Chỉ làm việc của các file này. Việc tồn đọng khác (ví dụ lịch sử phiên code đang chờ nạp) để cho `lifelog worker`.
    """
    from .audio import AudioError
    from .db import connect
    from .ingest import ingest_file, parse_time
    from .pipeline import finish_memory, run_voice_once
    from .reprocess import HAS_TEXT

    _setup(verbose)
    if start and len(files) > 1:
        raise typer.BadParameter("--start chỉ dùng khi gửi một file")
    _db_or_exit()
    settings = get_settings()

    chunk_ids: dict[str, Path] = {}
    for f in files:
        try:
            res = ingest_file(f, source="cli", started_at=parse_time(start), close_episode=True)
        except AudioError as exc:
            typer.secho(f"{f.name}: không phải audio đọc được ({exc})", fg="red", err=True)
            continue
        chunk_ids[res.id] = f
        typer.echo(f"{f.name}: {'đã có từ trước, không nhận lại' if res.duplicate else 'đã nhận'}")
    if not chunk_ids:
        raise typer.Exit(1)
    if no_wait:
        return

    deadline = time.monotonic() + timeout
    failed = False
    episode_ids: set = set()
    while True:
        while run_voice_once():
            pass
        waiting = []
        report = []
        with connect() as conn:
            for cid, f in chunk_ids.items():
                c = conn.execute(
                    "SELECT c.stage, c.error, c.attempts, e.id AS ep_id, e.stage AS ep_stage, e.error AS ep_error, "
                    "e.export_path FROM chunks c LEFT JOIN episodes e ON e.id = c.episode_id WHERE c.id = %s",
                    (cid,),
                ).fetchone()
                if c["stage"] == "no_speech":
                    report.append((f, "không có tiếng nói", False))
                elif c["ep_stage"] in HAS_TEXT:
                    episode_ids.add(c["ep_id"])
                    report.append((f, str(settings.data_dir / c["export_path"]), False))
                elif c["ep_error"] or (c["error"] and c["attempts"] >= settings.max_attempts):
                    where = f"episode {str(c['ep_id'])[:8]} dừng ở bước '{c['ep_stage']}'" if c["ep_id"] else "dừng ở bước cắt im lặng"
                    report.append((f, f"{where}: {c['ep_error'] or c['error']}", True))
                else:
                    waiting.append(f)
        if not waiting or time.monotonic() > deadline:
            break
        time.sleep(2.0)
    if episode_ids:
        finish_memory(list(episode_ids))

    for f, message, is_error in report:
        failed = failed or is_error
        typer.secho(f"{f.name}: {message}", fg="red" if is_error else None)
    for f in waiting:
        failed = True
        typer.secho(f"{f.name}: chưa xong sau {timeout:.0f} giây, xem `lifelog status`", fg="yellow")
    if failed:
        raise typer.Exit(1)


@app.command()
def status(limit: int = typer.Option(10, help="Số episode gần nhất để liệt kê.")) -> None:
    """Xem sổ: bao nhiêu việc ở mỗi bước, việc nào đang kẹt."""
    from .db import connect

    _db_or_exit()
    with connect() as conn:
        chunk_counts = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, count(*) AS n FROM chunks GROUP BY stage")}
        ep_counts = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, count(*) AS n FROM episodes GROUP BY stage")}
        ev_counts = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, count(*) AS n FROM events GROUP BY stage")}
        kinds = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, count(*) AS n FROM episodes GROUP BY kind")}
        journals = conn.execute("SELECT count(*) AS n, max(day) AS last FROM journals").fetchone()
        stuck_chunks = conn.execute(
            "SELECT id, original_name, stage, attempts, error FROM chunks WHERE error IS NOT NULL ORDER BY started_at"
        ).fetchall()
        stuck_eps = conn.execute("SELECT * FROM episodes WHERE error IS NOT NULL ORDER BY started_at").fetchall()
        recent = conn.execute("SELECT * FROM episodes ORDER BY started_at DESC LIMIT %s", (limit,)).fetchall()

    typer.echo("File:    " + "  ".join(f"{s} {chunk_counts.get(s, 0)}" for s in ("received", "vad", "grouped", "no_speech")))
    typer.echo("Sự kiện: " + "  ".join(f"{s} {ev_counts.get(s, 0)}" for s in ("received", "grouped", "ignored")))
    typer.echo("Episode: " + "  ".join(f"{s} {ep_counts.get(s, 0)}" for s in ("open", "grouped", "diarized", "transcribed", "indexed", "ingested"))
               + f"   (ghi âm {kinds.get('voice', 0)}, phiên code {kinds.get('code', 0)})")
    typer.echo(f"Nhật ký: {journals['n']} ngày" + (f", mới nhất {journals['last']:%d/%m/%Y}" if journals["last"] else ""))
    with connect() as conn:
        mac = conn.execute("SELECT count(*) AS n, max(ts) AS last FROM events WHERE source = 'mac'").fetchone()
    if mac["n"]:
        typer.echo(f"Mac:     {mac['n']} sự kiện, gần nhất lúc {mac['last'].astimezone(get_settings().tz):%d/%m %H:%M}")
    with connect() as conn:
        zl = conn.execute("SELECT count(*) AS n, max(ts) AS last FROM events WHERE source = 'zalo' "
                          "AND type = 'message'").fetchone()
    if zl["n"]:
        typer.echo(f"Zalo:    {zl['n']} messages, last at {zl['last'].astimezone(get_settings().tz):%d/%m %H:%M}")
    from . import voices

    cov = voices.coverage()
    if cov["total"]:
        waiting = len(voices.queue())
        typer.echo(f"Speakers: {cov['named'] / cov['total']:.0%} of speech in 14 days named "
                   f"({cov['auto'] / cov['total']:.0%} automatically), {waiting} voice(s) waiting for a name")
    since = get_settings().graph_since_at
    if since:
        with connect() as conn:
            old = conn.execute("SELECT count(*) AS n FROM episodes WHERE stage = 'indexed' AND started_at < %s", (since,)).fetchone()["n"]
        typer.echo(f"Graphiti chỉ nhận episode từ {since:%d/%m/%Y}: {old} episode cũ hơn dừng ở indexed là đúng ý, không phải kẹt.")
    if stuck_chunks or stuck_eps:
        typer.secho("\nĐang kẹt:", fg="red")
        for c in stuck_chunks:
            typer.echo(f"  file {c['original_name']} ở bước '{c['stage']}' (đã thử {c['attempts']} lần): {c['error']}")
        for e in stuck_eps:
            typer.echo(f"  episode {_episode_line(e)} (đã thử {e['attempts']} lần): {e['error']}")
    if recent:
        typer.echo("\nGần đây:")
        for e in recent:
            typer.echo(f"  {_episode_line(e)}  {e['export_path'] or ''}")


@app.command("reprocess")
def reprocess_cmd(
    from_step: str = typer.Option(..., "--from", help="Bước chạy lại: vad, group, diarize, transcribe, index, ingest."),
    since: Optional[str] = typer.Option(None, help="Từ ngày YYYY-MM-DD."),
    until: Optional[str] = typer.Option(None, help="Đến hết ngày YYYY-MM-DD."),
    episode: Optional[str] = typer.Option(None, help="Mã episode hoặc vài ký tự đầu."),
    backend: Optional[str] = typer.Option(None, help="Đổi dịch vụ chuyển chữ cho các episode này."),
    all_: bool = typer.Option(False, "--all", help="Cho phép chạy lại toàn bộ khi không đặt --since hay --episode."),
    no_run: bool = typer.Option(False, "--no-run", help="Chỉ lùi bước, để thợ chạy nền làm lại."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Chạy lại từ một bước, ví dụ sau khi đổi dịch vụ chuyển chữ hoặc đổi model của bộ nhớ."""
    from .asr import backend_names
    from .pipeline import drain
    from .reprocess import reprocess

    _setup(verbose)
    if not (since or until or episode or all_):
        raise typer.BadParameter("đặt --since, --episode, hoặc --all để chạy lại toàn bộ")
    if backend and backend not in backend_names():
        raise typer.BadParameter(f"không có dịch vụ '{backend}'. Có: {', '.join(backend_names())}")
    _db_or_exit()
    try:
        counts = reprocess(from_step, since=_date(since), until=_date(until), episode=episode, backend=backend)
    except ValueError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"Đã lùi {counts['episodes']} episode và {counts['chunks']} file về trước bước '{from_step}'.")
    if not no_run:
        typer.echo(f"Đã làm lại {drain()} việc. Xem `lifelog status`.")


@app.command()
def retry(
    no_run: bool = typer.Option(False, "--no-run"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Thử lại những việc đang kẹt, ví dụ sau khi thêm khóa API vào .env."""
    from .pipeline import drain
    from .reprocess import retry_failed

    _setup(verbose)
    _db_or_exit()
    counts = retry_failed()
    typer.echo(f"Cho thử lại {counts['chunks']} file và {counts['episodes']} episode.")
    if not no_run:
        typer.echo(f"Đã làm {drain()} việc. Xem `lifelog status`.")


@app.command()
def compare(
    episodes: list[str] = typer.Argument(None, help="Mã episode hoặc vài ký tự đầu."),
    since: Optional[str] = typer.Option(None, help="So sánh mọi episode từ ngày YYYY-MM-DD."),
    backends: Optional[str] = typer.Option(None, help="Danh sách dịch vụ, cách nhau bằng dấu phẩy. Mặc định là tất cả."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Chạy cùng các lượt nói qua nhiều dịch vụ chuyển chữ và xuất file so sánh."""
    from .asr import AsrUnavailable, backend_names, get_backend
    from .db import connect
    from .export import render_compare
    from .pipeline import transcribe_missing
    from .reprocess import HAS_TEXT, find_episodes

    _setup(verbose)
    _db_or_exit()
    settings = get_settings()
    names = [b.strip() for b in backends.split(",")] if backends else backend_names()
    if not episodes and not since:
        raise typer.BadParameter("đưa mã episode hoặc đặt --since")

    with connect() as conn:
        found: dict = {}
        for key in episodes or [None]:
            for e in find_episodes(conn, since=_date(since), episode=key):
                if e["stage"] in ("diarized", *HAS_TEXT):
                    found[e["id"]] = e
    if not found:
        typer.secho("Không có episode nào đã tách người nói xong trong phạm vi này.", fg="yellow")
        raise typer.Exit(1)

    usable = []
    for name in names:
        try:
            b = get_backend(name)
            b.check()
            usable.append(b)
        except AsrUnavailable as exc:
            typer.secho(f"Bỏ qua {name}: {exc}", fg="yellow")

    failed = False
    for ep_id, ep in found.items():
        for b in usable:
            try:
                n = transcribe_missing(ep_id, b)
                if n:
                    typer.echo(f"{str(ep_id)[:8]}: {b.name} chuyển chữ {n} lượt nói")
            except Exception as exc:
                failed = True
                typer.secho(f"{str(ep_id)[:8]}: {b.name} lỗi: {exc}", fg="red")
        local = ep["started_at"].astimezone(settings.tz)
        dest = settings.episodes_dir / f"{local:%Y-%m-%d}" / f"{local:%H%M}_{str(ep_id)[:8]}.compare.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        with connect() as conn:
            dest.write_text(render_compare(conn, ep_id, names), encoding="utf-8")
        typer.echo(str(dest))
    if failed:
        raise typer.Exit(1)


@app.command()
def show(episode: str = typer.Argument(..., help="Mã episode hoặc vài ký tự đầu."),
         backend: Optional[str] = typer.Option(None, help="Xem chữ của dịch vụ khác.")) -> None:
    """In transcript của một episode ra màn hình."""
    from .db import connect
    from .export import render_episode
    from .reprocess import find_episodes

    _db_or_exit()
    with connect() as conn:
        found = find_episodes(conn, episode=episode)
        if len(found) != 1:
            typer.secho(f"Tìm thấy {len(found)} episode khớp '{episode}', cần đúng một.", fg="red", err=True)
            raise typer.Exit(1)
        typer.echo(render_episode(conn, found[0]["id"], backend))


@app.command()
def search(
    query: str = typer.Argument(..., help="Câu hỏi hoặc vài từ cần tìm."),
    since: Optional[str] = typer.Option(None, help="Từ mốc này: 2026-10-13 hoặc 2026-10-13T09:30."),
    until: Optional[str] = typer.Option(None, help="Trước mốc này. Một ngày trơn thì tính hết ngày đó."),
    speaker: list[str] = typer.Option([], "--speaker", help="Chỉ lấy đoạn có người này nói. Lặp lại được."),
    limit: int = typer.Option(5, help="Số kết quả mỗi kho."),
) -> None:
    """Tìm trong bộ nhớ. In ra đúng thứ agent nhận được từ tool `search` của MCP server."""
    from .search import render
    from .search import search as run

    _setup()
    _db_or_exit()
    try:
        typer.echo(render(run(query, since=since, until=until, speakers=speaker, limit=limit)))
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def summary(
    day: Optional[str] = typer.Argument(None, help="Day YYYY-MM-DD. Defaults to yesterday."),
    week: bool = typer.Option(False, "--week", help="The week (Monday to Sunday) containing that day."),
    detail: bool = typer.Option(False, "--detail", help="Include the full summary of every Claude Code session."),
) -> None:
    """Print what the agent gets from the MCP `summary` tool."""
    from .journal import day_summary, week_summary

    _setup()
    _db_or_exit()
    target = _date(day) or datetime.now(get_settings().tz).date() - timedelta(days=1)
    try:
        typer.echo(week_summary(target) if week else day_summary(target, detail=detail))
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def people(name: str = typer.Argument(..., help="The person's name.")) -> None:
    """Print what the agent gets from the MCP `people` tool."""
    from .people import profile

    _setup()
    _db_or_exit()
    try:
        typer.echo(profile(name))
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def timeline(
    topic: str = typer.Argument(..., help="Topic, decision or project."),
    since: Optional[str] = typer.Option(None, help="From this point: 2026-10-13 or 2026-10-13T09:30."),
    until: Optional[str] = typer.Option(None, help="Before this point. A plain day counts the whole day."),
    limit: int = typer.Option(20, help="Maximum number of facts."),
) -> None:
    """Print what the agent gets from the MCP `timeline` tool."""
    from .timeline import timeline as run

    _setup()
    _db_or_exit()
    try:
        typer.echo(run(topic, since=since, until=until, limit=limit))
    except ValueError as exc:
        raise typer.BadParameter(str(exc))


@app.command()
def journal(
    day: Optional[str] = typer.Argument(None, help="Ngày YYYY-MM-DD. Mặc định là hôm nay."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Viết (hoặc viết lại) nhật ký làm việc của một ngày từ các phiên Claude Code và hoạt động trên Mac."""
    from .journal import write_journal
    from .pipeline import drain

    _setup(verbose)
    _db_or_exit()
    settings = get_settings()
    target = _date(day) or datetime.now(settings.tz).date()
    drain()  # đọc và tóm tắt những phiên vừa xong trước khi viết
    path = write_journal(target)
    if not path:
        typer.secho(f"Ngày {target:%d/%m/%Y} chưa có phiên Claude Code nào đã tóm tắt, cũng chưa có hoạt động "
                    "trên Mac. Phiên đang mở chỉ được tính sau khi im một lúc.", fg="yellow")
        raise typer.Exit(1)
    typer.echo(str(settings.data_dir / path))


@app.command()
def mac(
    day: Optional[str] = typer.Argument(None, help="Ngày YYYY-MM-DD. Mặc định là hôm nay."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Tính lại các khoảng từ sự kiện thô (sau khi đổi luật). "
                                 "Không ghi ngày thì tính lại mọi ngày."),
) -> None:
    """Xem hoạt động trên Mac của một ngày: các khoảng thời gian và mục nhật ký sẽ được viết từ đó."""
    from . import mac_log
    from .db import connect

    _db_or_exit()
    settings = get_settings()
    target = _date(day)
    if rebuild:
        n = mac_log.rebuild(target)
        typer.echo(f"Đã tính lại {n} khoảng" + (f" của ngày {target:%d/%m/%Y}" if target else " của mọi ngày") + ".")
        if not target:
            return
    else:
        mac_log.group_mac()
    target = target or datetime.now(settings.tz).date()
    with connect() as conn:
        spans = conn.execute(
            "SELECT started_at, ended_at, seconds, app, context FROM mac_spans WHERE day = %s ORDER BY started_at",
            (target,)).fetchall()
        section = mac_log.render_day(conn, target)
    if not spans and not section:
        typer.secho(f"Ngày {target:%d/%m/%Y} chưa có hoạt động nào trên Mac. Mười phút gần nhất chỉ được tính "
                    "sau khi qua hẳn.", fg="yellow")
        raise typer.Exit(1)
    for s in spans:
        typer.echo(f"{s['started_at'].astimezone(settings.tz):%H:%M}–{s['ended_at'].astimezone(settings.tz):%H:%M}  "
                   f"{mac_log.duration(s['seconds']):>14}  {s['app']}" + (f" · {s['context']}" if s["context"] else ""))
    typer.echo("")
    typer.echo("\n".join(section).rstrip())


@app.command()
def mcp() -> None:
    """Chạy MCP server cho agent (Claude Code) kết nối qua stdio."""
    from .mcp_server import main

    main()


@app.command()
def doctor() -> None:
    """Kiểm tra máy và cấu hình đã đủ để chạy chưa."""
    from .doctor import run

    report = run()
    section = ""
    for check in report.checks:
        if check.section != section:
            section = check.section
            typer.echo(f"\n{section}")
            for note in report.notes.get(section, []):
                typer.echo(f"  {note}")
        mark = {True: ("ok  ", "green"), False: ("THIẾU", "red"), None: ("--  ", "yellow")}[check.good]
        typer.secho(f"[{mark[0]}] ", fg=mark[1], nl=False)
        typer.echo(f"{check.label}{': ' + check.detail if check.detail else ''}")

    typer.echo(f"\nDữ liệu nằm ở: {report.data_dir}")
    if not report.ok:
        raise typer.Exit(1)


@app.command("zalo")
def zalo_cmd(
    days: int = typer.Option(7, help="How many days back to count."),
    threads: bool = typer.Option(False, "--threads", help="List every chat with its id, to fill zalo.json."),
    limit: int = typer.Option(20, help="How many chats to list."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Drop chat episodes and group the messages again "
                                 "(after changing the rules). With --since, only from that day on."),
    since: Optional[str] = typer.Option(None, help="With --rebuild: from this day, YYYY-MM-DD."),
) -> None:
    """Zalo messages received (product 10): per day, then the busiest chats."""
    from . import zalo
    if rebuild:
        from . import zalo_episodes

        _db_or_exit()
        n = zalo_episodes.rebuild(_date(since))
        typer.echo(f"Dropped {n} chat episodes; the worker groups the messages again on its next run.")
        return
    from .db import connect

    _db_or_exit()
    s = get_settings()
    since = datetime.now(s.tz) - timedelta(days=days)
    with connect() as conn:
        good, detail = zalo.verdict(zalo.health(conn), datetime.now(s.tz), s.tz)
        per_day = zalo.daily(conn, since, s.timezone)
        chats = zalo.threads(conn, since)
    typer.secho(f"Sensor: {detail}", fg={True: "green", False: "red", None: "yellow"}[good])
    if not per_day:
        typer.echo(f"No Zalo messages in the last {days} days.")
        return
    typer.echo("")
    typer.echo(f"{'day':<10} {'direct':>7} {'groups':>7} {'counted':>8} {'chars':>8} {'by Kim':>7} {'undone':>7}")
    for d in per_day:
        typer.echo(f"{d.day:%d/%m/%Y} {d.direct:>7} {d.groups:>7} {d.counted:>8} {d.chars:>8} {d.mine:>7} {d.undone:>7}")
    typer.echo("")
    typer.echo("direct: direct chats kept in full. groups: allowed groups kept in full. "
               "counted: other groups, counted only.")
    typer.echo("")
    shown = chats if threads else chats[:limit]
    typer.echo(f"{'kind':<6} {'messages':>8} {'chars':>8} {'by Kim':>7} {'@Kim':>5}  {'last':<11} chat")
    for t in shown:
        label = (t.name or "?") + (" (counted only)" if t.skipped else "") + (f"  id {t.id}" if threads else "")
        typer.echo(f"{t.kind:<6} {t.messages:>8} {t.chars:>8} {t.mine:>7} {t.mentions_me:>5}  "
                   f"{t.last.astimezone(s.tz):%d/%m %H:%M} {label}")
    if len(chats) > len(shown):
        typer.echo(f"... and {len(chats) - len(shown)} more; --threads lists all with their ids.")


@app.command("zalo-link")
def zalo_link_cmd(
    person: Optional[str] = typer.Argument(None, help="Zalo name or id of the person."),
    voice: Optional[str] = typer.Argument(None, help="Name of the named voice they are."),
    remove: bool = typer.Option(False, "--remove", help="Undo the link of this person."),
    limit: int = typer.Option(40, help="How many people to list."),
) -> None:
    """Which named voice each Zalo person is (product 10, round 3). With no arguments, lists the people Kim
    chats with, busiest first, with their link or the voices whose names look like theirs."""
    from . import zalo_people
    from .db import connect

    _db_or_exit()
    with connect() as conn:
        try:
            if person and remove:
                p = zalo_people.unlink(conn, person)
                typer.echo(f'Unlinked "{p.name}".')
                return
            if person and voice:
                p, name = zalo_people.link(conn, person, voice)
                typer.echo(f'Linked Zalo "{p.name}" to the voice "{name}". Chats sent to the graph from now on '
                           f'call them "{name}".')
                return
            if person:
                raise ValueError("give the voice name too, or --remove")
        except ValueError as exc:
            typer.secho(str(exc), fg="red")
            raise typer.Exit(1) from exc
        people = zalo_people.suggest(conn, zalo_people.contacts(conn))
    tz = get_settings().tz
    typer.echo(f"{'messages':>8} {'days':>5}  {'last':<11} {'Zalo name':<28} voice")
    for p in people[:limit]:
        link = p.voice or (("maybe: " + ", ".join(p.suggested)) if p.suggested else "")
        typer.echo(f"{p.messages:>8} {p.days:>5}  {p.last.astimezone(tz):%d/%m %H:%M} {p.name:<28} {link}")
    if len(people) > limit:
        typer.echo(f"... and {len(people) - limit} more.")
    typer.echo('\nLink one: lifelog zalo-link "<Zalo name>" "<voice name>"')


@app.command("recorder")
def recorder_cmd(day: Optional[str] = typer.Argument(None, help="Day YYYY-MM-DD, default today."),
                 mac: bool = typer.Option(False, "--mac", help="The Mac's room recording instead of the iPhone.")) -> None:
    """What the iPhone recorder (or the Mac's room recording) delivered on a day: recorded time, gaps and why."""
    from .recorder import EXPECTED, MAC_SOURCE, SOURCE, day_report

    _db_or_exit()
    s = get_settings()
    when = _date(day) or datetime.now(s.tz).date()
    r = day_report(when, MAC_SOURCE if mac else SOURCE)
    if not r.chunks:
        typer.echo(f"No {'Mac room' if mac else 'iPhone'} recordings on {when:%d/%m/%Y}.")
        return
    typer.echo(f"{when:%d/%m/%Y}: {r.recorded_s / 3600:.1f} h recorded in {r.chunks} chunks, "
               f"{r.first.astimezone(s.tz):%H:%M}–{r.last.astimezone(s.tz):%H:%M}; "
               f"lost {r.lost_s / 60:.1f} min in gaps, {r.calls} chunk(s) with a call")
    for g in r.gaps:
        note = "" if g.reason in EXPECTED else "  <- lost"
        typer.echo(f"  gap {g.start.astimezone(s.tz):%H:%M:%S}–{g.end.astimezone(s.tz):%H:%M:%S} "
                   f"{g.seconds / 60:5.1f} min  {g.reason}{note}")


# ------------------------------------------------------------------ speakers (product 6)

speakers_app = typer.Typer(no_args_is_help=False, help="Who is speaking: enrol voices, name them, see coverage.")
app.add_typer(speakers_app, name="speakers")


def _speaker_id(prefix: str):
    """Full speaker id from its first characters."""
    from .db import connect

    with connect() as conn:
        rows = conn.execute("SELECT id FROM speakers WHERE id::text LIKE %s || '%%'", (prefix,)).fetchall()
    if len(rows) != 1:
        raise typer.BadParameter(f"{'no' if not rows else 'more than one'} speaker starts with '{prefix}'")
    return rows[0]["id"]


@speakers_app.callback(invoke_without_command=True)
def speakers_list(ctx: typer.Context) -> None:
    """Named people, voices waiting for a name, and how much speech carries a name."""
    if ctx.invoked_subcommand:
        return
    from . import voices

    _db_or_exit()
    cov = voices.coverage()
    if cov["total"]:
        typer.echo(f"Last 14 days: {cov['named'] / cov['total']:.0%} of recorded speech named "
                   f"({cov['auto'] / cov['total']:.0%} automatically), {cov['ignored'] / cov['total']:.0%} marked not important.")
    typer.echo("\nPeople:")
    for p in voices.people() or []:
        typer.echo(f"  {str(p['id'])[:8]}  {p['name']}{' (me)' if p['is_me'] else ''}  "
                   f"{p['episodes']} conversations, {p['speech_s'] / 60:.0f} min, {p['prints']} voiceprints")
    waiting = voices.queue()
    typer.echo(f"\nWaiting for a name: {len(waiting)}" + (f"  (open http://127.0.0.1:8000/speakers)" if waiting else ""))
    for v in waiting:
        hint = ", ".join(dict.fromkeys([*v["names"], *v["invited"], *v["roles"]]))
        typer.echo(f"  {str(v['id'])[:8]}  {v['days']} days, {v['episodes']} conversations, {v['speech_s'] / 60:.0f} min"
                   + (f"  [{hint}]" if hint else ""))


@speakers_app.command("enroll")
def speakers_enroll(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="Audio file where this person does most of the talking."),
    name: Optional[str] = typer.Option(None, help="Person's name. With --me it defaults to OWNER_NAME."),
    me: bool = typer.Option(False, "--me", help="This is your own voice."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Learn a voice from an audio file, then relabel older recordings that match it."""
    from . import voices

    _setup(verbose)
    _db_or_exit()
    try:
        got = voices.enroll(path, name, me=me)
    except voices.VoiceError as exc:
        typer.secho(f"Not enrolled: {exc}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Enrolled {got.name} from {got.speech_s:.0f} s of speech; {got.episodes} older recording(s) relabelled.")
    if got.other_s > 3:
        typer.secho(f"Note: the file also has {got.other_s:.0f} s of other voices, which were ignored.", fg="yellow")


@speakers_app.command("name")
def speakers_name(speaker: str = typer.Argument(..., help="Speaker id or its first characters."),
                  name: str = typer.Argument(...)) -> None:
    """Name a voice. An existing name merges the two."""
    from . import voices

    _db_or_exit()
    try:
        res = voices.name_speaker(_speaker_id(speaker), name)
    except voices.VoiceError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo(f"{'Merged into' if res['merged'] else 'Named'} {res['name']}; {res['episodes']} recording(s) will be rebuilt.")


@speakers_app.command("ignore")
def speakers_ignore(speaker: str = typer.Argument(..., help="Speaker id or its first characters.")) -> None:
    """Stop asking about a voice until it keeps turning up again. It is forgotten after STRANGER_KEEP_DAYS
    without being heard."""
    from . import voices

    _db_or_exit()
    try:
        voices.ignore_speaker(_speaker_id(speaker))
    except voices.VoiceError as exc:
        raise typer.BadParameter(str(exc))
    typer.echo("OK. It is asked about again only if it keeps turning up.")


@speakers_app.command("rescan")
def speakers_rescan(
    since: Optional[str] = typer.Option(None, help="Only recordings from this day (YYYY-MM-DD)."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Match voices only; no role labels from the LLM."),
    no_run: bool = typer.Option(False, "--no-run", help="Leave the rebuild to the background worker."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Match the voices of recordings processed before speaker matching existed."""
    from datetime import time as dtime

    from . import voices
    from .pipeline import drain

    _setup(verbose)
    _db_or_exit()
    day = _date(since)
    res = voices.rescan(datetime.combine(day, dtime.min, get_settings().tz) if day else None, llm=not no_llm)
    typer.echo(f"Checked {res['episodes']} recording(s); {res['rebuilt']} got new labels and will be rebuilt.")
    if res["rebuilt"] and not no_run:
        typer.echo(f"Did {drain()} step(s). See `lifelog status`.")


if __name__ == "__main__":
    app()
