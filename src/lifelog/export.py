"""Xuất một episode thành file markdown đọc được."""
from __future__ import annotations

from pathlib import Path

from .config import get_settings


def _fmt_dur(seconds: float | None) -> str:
    seconds = int(round(seconds or 0))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h} giờ {m:02d} phút"
    if m:
        return f"{m} phút {s:02d} giây"
    return f"{s} giây"


def _header(conn, ep: dict, backend: str) -> list[str]:
    tz = get_settings().tz
    start, end = ep["started_at"].astimezone(tz), ep["ended_at"].astimezone(tz)
    if ep["kind"] == "read":
        meta = ep["meta"] or {}
        return [
            f"# Đã đọc {start:%d/%m/%Y} {start:%H:%M}: {meta.get('title') or '?'}",
            "",
            f"- Mã episode: `{ep['id']}`",
            f"- Địa chỉ: {meta.get('url') or '?'}",
            f"- Trang: {meta.get('site') or meta.get('domain') or '?'}" + (f", tác giả {meta['byline']}" if meta.get("byline") else ""),
            f"- {meta.get('chars', 0)} ký tự, lấy bằng extension Chrome",
            "",
        ]
    if ep["kind"] == "chat":
        meta = ep["meta"] or {}
        where = ("nhóm " if meta.get("thread_kind") == "group" else "chat với ") + (meta.get("title") or "?")
        return [
            f"# Zalo {start:%d/%m/%Y} {start:%H:%M}–{end:%H:%M}: {where}",
            "",
            f"- Mã episode: `{ep['id']}`",
            f"- Hội thoại Zalo: {meta.get('thread_id')}",
            f"- {meta.get('messages', 0)} tin, {meta.get('from_kim', 0)} tin của Kim; người nhắn: "
            + ", ".join(meta.get("participants") or []),
            "",
        ]
    if ep["kind"] == "code":
        meta = ep["meta"] or {}
        files = meta.get("files") or []
        return [
            f"# Phiên Claude Code {start:%d/%m/%Y} {start:%H:%M}–{end:%H:%M}",
            "",
            f"- Tên phiên: {meta.get('title') or 'chưa có'}",
            f"- Mã episode: `{ep['id']}`",
            f"- Dự án: {meta.get('project') or '?'}" + (f" (nhánh {meta['branch']})" if meta.get("branch") else ""),
            f"- Thư mục: {meta.get('cwd') or '?'}",
            f"- {meta.get('prompts', 0)} lần yêu cầu, {meta.get('actions', 0)} việc Claude làm",
            f"- File đã sửa: {', '.join(files) if files else 'không có'}",
            "",
        ]
    sources = conn.execute(
        "SELECT original_name FROM chunks WHERE episode_id = %s ORDER BY started_at", (ep["id"],)
    ).fetchall()
    speakers = conn.execute(
        "SELECT DISTINCT COALESCE(p.name, es.role, s.speaker) AS speaker FROM segments s "
        "LEFT JOIN episode_speakers es ON es.episode_id = s.episode_id AND es.label = s.speaker "
        "LEFT JOIN speakers p ON p.id = es.speaker_id WHERE s.episode_id = %s ORDER BY 1", (ep["id"],)
    ).fetchall()
    names = [r["original_name"] or "?" for r in sources]
    shown = ", ".join(names[:5]) + (f" và {len(names) - 5} file khác" if len(names) > 5 else "")
    mt = (ep["meta"] or {}).get("meeting")
    if mt:
        shown = f"app họp {mt.get('app') or '?'} trên Mac, mic {mt.get('mic') or '?'} ({len(names)} file)"
    title = f"Họp online qua {mt.get('app') or '?'}" if mt else "Hội thoại"
    return [
        f"# {title} {start:%d/%m/%Y} {start:%H:%M}–{end:%H:%M}",
        "",
        f"- Mã episode: `{ep['id']}`",
        f"- Thời lượng audio: {_fmt_dur(ep['duration_s'])}, trong đó có tiếng nói: {_fmt_dur(ep['speech_s'])}",
        f"- Người nói: {', '.join(r['speaker'] for r in speakers) or 'không có'}",
        f"- Tách người nói: {ep['diarizer'] or 'chưa'}",
        f"- Chuyển chữ: {backend}",
        f"- Nguồn: {shown}",
        "",
    ]


def render_episode(conn, episode_id, backend: str | None = None) -> str:
    tz = get_settings().tz
    ep = conn.execute("SELECT * FROM episodes WHERE id = %s", (episode_id,)).fetchone()
    backend = backend or ep["asr_backend"] or get_settings().asr_backend
    rows = conn.execute(
        """
        SELECT s.idx, COALESCE(p.name, es.role, s.speaker) AS speaker, s.started_at, t.text
        FROM segments s LEFT JOIN segment_texts t ON t.segment_id = s.id AND t.backend = %s
        LEFT JOIN episode_speakers es ON es.episode_id = s.episode_id AND es.label = s.speaker
        LEFT JOIN speakers p ON p.id = es.speaker_id
        WHERE s.episode_id = %s ORDER BY s.idx
        """,
        (backend, episode_id),
    ).fetchall()

    lines = _header(conn, ep, backend)
    # Gộp các lượt liền nhau của cùng một người thành một đoạn.
    blocks: list[dict] = []
    for r in rows:
        text = (r["text"] or "").strip()
        if not text:
            continue
        if blocks and blocks[-1]["speaker"] == r["speaker"]:
            blocks[-1]["text"] += " " + text
        else:
            blocks.append({"speaker": r["speaker"], "at": r["started_at"], "text": text})
    if not blocks:
        lines.append("_Không có nội dung chuyển thành chữ được._")
    for b in blocks:
        lines.append(f"**[{b['at'].astimezone(tz):%H:%M:%S}] {b['speaker']}:** {b['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_episode(conn, episode_id) -> str:
    """Ghi file markdown và trả về đường dẫn tương đối so với data_dir."""
    settings = get_settings()
    ep = conn.execute("SELECT id, started_at, export_path FROM episodes WHERE id = %s", (episode_id,)).fetchone()
    local = ep["started_at"].astimezone(settings.tz)
    rel = Path("episodes") / f"{local:%Y-%m-%d}" / f"{local:%H%M}_{str(ep['id'])[:8]}.md"
    if ep["export_path"] and ep["export_path"] != str(rel):
        (settings.data_dir / ep["export_path"]).unlink(missing_ok=True)
    dest = settings.data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_episode(conn, episode_id), encoding="utf-8")
    conn.execute("UPDATE episodes SET export_path = %s WHERE id = %s", (str(rel), episode_id))
    return str(rel)


def render_compare(conn, episode_id, backends: list[str]) -> str:
    """Đặt chữ của nhiều dịch vụ cạnh nhau theo từng lượt nói."""
    tz = get_settings().tz
    ep = conn.execute("SELECT * FROM episodes WHERE id = %s", (episode_id,)).fetchone()
    segs = conn.execute(
        "SELECT id, idx, speaker, started_at, start_s, end_s FROM segments WHERE episode_id = %s ORDER BY idx",
        (episode_id,),
    ).fetchall()
    texts = conn.execute(
        """
        SELECT t.segment_id, t.backend, t.text FROM segment_texts t
        JOIN segments s ON s.id = t.segment_id WHERE s.episode_id = %s
        """,
        (episode_id,),
    ).fetchall()
    by_seg: dict[int, dict[str, str]] = {}
    for t in texts:
        by_seg.setdefault(t["segment_id"], {})[t["backend"]] = t["text"]

    lines = _header(conn, ep, " so với ".join(backends))
    lines[0] = lines[0].replace("# Hội thoại", "# So sánh chuyển chữ:")
    for s in segs:
        lines.append(f"### [{s['started_at'].astimezone(tz):%H:%M:%S}] {s['speaker']} ({s['end_s'] - s['start_s']:.1f} giây)")
        lines.append("")
        for b in backends:
            text = by_seg.get(s["id"], {}).get(b)
            lines.append(f"- **{b}:** {text if text else '_(trống)_' if text == '' else '_(chưa chạy)_'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
