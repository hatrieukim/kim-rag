"""Tìm trong bộ nhớ: nguyên văn từ Postgres và fact từ Graphiti, gộp vào một kết quả.

Hai kho trả lời hai loại câu hỏi. Nguyên văn cho biết ai nói chính xác gì, lúc nào, và bắt được cả
những đoạn không có tên riêng. Fact là điều đã được chắt lọc: ai, việc gì, quan hệ, còn đúng hay không.
Mỗi fact đều lần được về đúng các lượt nói đã sinh ra nó.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from . import embed, graph
from .config import get_settings
from .db import connect
from .windows import make_window, transcript_rows

MAX_LIMIT = 20
# Đoạn kém kết quả đứng đầu quá ngần này thì bỏ, để agent không phải đọc cả nghìn chữ lạc đề.
# Với gemini-embedding-2, đoạn đúng thường được 0,6 trở lên, đoạn lạc đề quanh 0,45.
SCORE_GAP = 0.15


@dataclass
class Passage:
    episode_id: str
    first_idx: int
    last_idx: int
    started_at: datetime
    ended_at: datetime
    speakers: list[str]
    text: str
    score: float  # độ gần nghĩa với câu hỏi, 0 tới 1
    origin: str = "ghi âm"   # ghi âm, hoặc "Claude Code, dự án X"
    summary: bool = False    # True khi đoạn này là bản tóm tắt của cả episode


@dataclass
class Source:
    episode_id: str
    first_idx: int
    last_idx: int
    started_at: datetime
    origin: str = "ghi âm"


@dataclass
class Fact:
    text: str
    valid_at: datetime | None
    invalid_at: datetime | None
    sources: list[Source]


@dataclass
class Result:
    query: str
    since: datetime | None
    until: datetime | None
    speakers: list[str]
    passages: list[Passage] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # kho nào không hỏi được và vì sao


def parse_when(value: str | None, *, end: bool = False) -> datetime | None:
    """Đọc ngày `2026-10-13` hoặc giờ ISO 8601. Thiếu múi giờ thì hiểu là giờ trong TIMEZONE.

    Với mốc kết thúc (`end`), một ngày trơn nghĩa là hết ngày đó.
    """
    if not value or not value.strip():
        return None
    value = value.strip()
    tz = get_settings().tz
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            return datetime.combine(day + timedelta(days=1) if end else day, time.min, tz)
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"không đọc được thời gian '{value}'. Dùng dạng 2026-10-13 hoặc 2026-10-13T09:30") from None
    return when if when.tzinfo else when.replace(tzinfo=tz)


def _origin(kind: str | None, project: str | None, title: str | None = None) -> str:
    if kind == "chat":   # `project` is the thread kind for a chat
        return f"Zalo, {'nhóm' if project == 'group' else 'chat với'} {title or '?'}"
    if kind == "read":
        return f"Chrome, trang {project or '?'}" + (f', bài "{title}"' if title else "")
    if kind != "code":
        return f"họp online qua {project}" if project else "ghi âm"
    return f"Claude Code, dự án {project or '?'}" + (f', phiên "{title}"' if title else "")


def _merge(hits: list[dict]) -> list[dict]:
    """Các cửa sổ chồng hoặc nối đuôi nhau trong cùng một episode gộp thành một đoạn, để không lặp chữ.

    Bản tóm tắt của episode đứng riêng: nó phủ cả episode nên gộp vào sẽ kéo theo toàn bộ lời thoại.
    """
    merged: list[dict] = [{**h, "merged": False} for h in hits if h.get("kind") == "summary"]
    turns = [h for h in hits if h.get("kind") != "summary"]
    last = None
    for hit in sorted(turns, key=lambda h: (str(h["episode_id"]), h["first_idx"])):
        if last and last["episode_id"] == hit["episode_id"] and hit["first_idx"] <= last["last_idx"] + 1:
            last["last_idx"] = max(last["last_idx"], hit["last_idx"])
            last["score"] = max(last["score"], hit["score"])
            last["merged"] = True
        else:
            last = {**hit, "merged": False}
            merged.append(last)
    return sorted(merged, key=lambda h: -h["score"])


def find_passages(query: str, since: datetime | None, until: datetime | None, speakers: list[str],
                  limit: int) -> tuple[list[Passage], int]:
    """Các đoạn gần nghĩa nhất, và số cửa sổ đã bỏ vì kém hẳn kết quả đứng đầu."""
    settings = get_settings()
    vec = embed.vector_literal(embed.embed_texts([query])[0])
    with connect() as conn:
        # Có bộ lọc thì chỉ mục vector phải dò tiếp cho đủ số kết quả, không dừng ở mẻ đầu.
        conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        hits = conn.execute(
            """
            SELECT w.episode_id, w.kind, w.first_idx, w.last_idx, w.started_at, w.ended_at, w.speakers, w.text,
                   e.kind AS ep_kind, COALESCE(e.meta->>'project', e.meta->>'domain', e.meta->'meeting'->>'app', e.meta->>'thread_kind') AS project, e.meta->>'title' AS title,
                   1 - (w.embedding <=> %(vec)s::vector) AS score
            FROM windows w JOIN episodes e ON e.id = w.episode_id
            WHERE w.embedding_model = %(model)s
              AND (%(lo)s::timestamptz IS NULL OR w.ended_at >= %(lo)s)
              AND (%(hi)s::timestamptz IS NULL OR w.started_at < %(hi)s)
              AND (%(who)s::text[] IS NULL OR EXISTS (SELECT 1 FROM unnest(w.speakers) s WHERE lower(s) = ANY(%(who)s)))
            ORDER BY w.embedding <=> %(vec)s::vector
            LIMIT %(n)s
            """,
            {"vec": vec, "model": settings.embed_model, "lo": since, "hi": until,
             "who": [s.lower() for s in speakers] or None, "n": limit},
        ).fetchall()
        hits.sort(key=lambda h: -h["score"])
        kept = [h for h in hits if h["score"] >= hits[0]["score"] - SCORE_GAP]
        passages: list[Passage] = []
        for h in _merge(kept):
            if h["merged"]:
                ep = conn.execute("SELECT id, asr_backend FROM episodes WHERE id = %s", (h["episode_id"],)).fetchone()
                w = make_window(transcript_rows(conn, ep, h["first_idx"], h["last_idx"]))
                h = {**h, "started_at": w.started_at, "ended_at": w.ended_at, "speakers": w.speakers, "text": w.text}
            passages.append(Passage(
                episode_id=str(h["episode_id"]), first_idx=h["first_idx"], last_idx=h["last_idx"],
                started_at=h["started_at"], ended_at=h["ended_at"], speakers=list(h["speakers"]),
                text=h["text"], score=float(h["score"]),
                origin=_origin(h["ep_kind"], h["project"], h["title"]), summary=h["kind"] == "summary",
            ))
    return passages, len(hits) - len(kept)


def find_facts(query: str, since: datetime | None, until: datetime | None, speakers: list[str],
               limit: int) -> list[Fact]:
    filtered = bool(since or until or speakers)
    # Bộ lọc áp sau khi Graphiti trả kết quả, nên xin dư ra để lọc xong vẫn còn đủ.
    found = graph.search_facts(query, limit * 4 if filtered else limit)
    return with_sources(found, since, until, speakers)[:limit]


def with_sources(found: list[dict], since: datetime | None = None, until: datetime | None = None,
                 speakers: list[str] | None = None) -> list[Fact]:
    """Facts from the graph with the turns that produced them. Facts whose turns are outside the filters, or
    that only come from a replaced version of an episode, are dropped. Order is kept."""
    speakers = speakers or []
    uuids = [u for f in found for u in f["parts"]]
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT g.uuid, g.episode_id, g.first_idx, g.last_idx, g.started_at, e.kind, COALESCE(e.meta->>'project', e.meta->>'domain', e.meta->'meeting'->>'app', e.meta->>'thread_kind') AS project,
                   e.meta->>'title' AS title
            FROM graph_parts g JOIN episodes e ON e.id = g.episode_id
            WHERE g.uuid = ANY(%(uuids)s::uuid[])
              AND (%(hi)s::timestamptz IS NULL OR g.started_at < %(hi)s)
              AND (%(lo)s::timestamptz IS NULL OR EXISTS (
                    SELECT 1 FROM segments s WHERE s.episode_id = g.episode_id
                    AND s.idx BETWEEN g.first_idx AND g.last_idx AND s.started_at >= %(lo)s))
              AND (%(who)s::text[] IS NULL OR EXISTS (
                    SELECT 1 FROM segments s
                    LEFT JOIN episode_speakers es ON es.episode_id = s.episode_id AND es.label = s.speaker
                    LEFT JOIN speakers p ON p.id = es.speaker_id
                    WHERE s.episode_id = g.episode_id AND s.idx BETWEEN g.first_idx AND g.last_idx
                    AND lower(COALESCE(p.name, es.role, s.speaker)) = ANY(%(who)s)))
            """,
            {"uuids": uuids, "lo": since, "hi": until, "who": [s.lower() for s in speakers] or None},
        ).fetchall()
    parts = {str(r["uuid"]): r for r in rows}

    out: list[Fact] = []
    for f in found:
        sources = [
            Source(str(parts[u]["episode_id"]), parts[u]["first_idx"], parts[u]["last_idx"], parts[u]["started_at"],
                   _origin(parts[u]["kind"], parts[u]["project"], parts[u]["title"]))
            for u in f["parts"] if u in parts
        ]
        if sources:  # fact không còn nguồn trong Postgres là fact của bản đã bị thay
            out.append(Fact(f["fact"], f["valid_at"], f["invalid_at"], sources))
    return out


def search(query: str, *, since: str | None = None, until: str | None = None,
           speakers: list[str] | None = None, limit: int = 5) -> Result:
    """Hỏi cả hai kho cùng lúc. Kho nào không hỏi được thì ghi vào `notes`, kho còn lại vẫn trả kết quả."""
    query = (query or "").strip()
    if not query:
        raise ValueError("câu hỏi đang trống")
    limit = max(1, min(int(limit), MAX_LIMIT))
    result = Result(query, parse_when(since), parse_when(until, end=True),
                    [s.strip() for s in speakers or [] if s and s.strip()])
    if result.since and result.until and result.since >= result.until:
        raise ValueError("mốc bắt đầu phải trước mốc kết thúc")
    args = (query, result.since, result.until, result.speakers, limit)

    with ThreadPoolExecutor(max_workers=2) as pool:
        passages = pool.submit(find_passages, *args)
        facts = None
        if get_settings().graph == "none":
            result.notes.append("Kho fact đang tắt (GRAPH=none), chỉ tìm trong nguyên văn.")
        else:
            try:
                graph.check()
                facts = pool.submit(find_facts, *args)
            except graph.GraphUnavailable as exc:
                result.notes.append(f"Kho fact không hỏi được: {exc}.")
        try:
            result.passages, dropped = passages.result()
            if dropped:
                result.notes.append(f"Đã bỏ {dropped} đoạn nguyên văn kém liên quan hơn hẳn đoạn đứng đầu.")
        except Exception as exc:
            result.notes.append(f"Kho nguyên văn không hỏi được: {exc}.")
        if facts is not None:
            try:
                result.facts = facts.result()
            except Exception as exc:
                result.notes.append(f"Kho fact không hỏi được: {exc}.")
    return result


def _span(start: datetime, end: datetime | None = None) -> str:
    tz = get_settings().tz
    start = start.astimezone(tz)
    text = f"{start:%d/%m/%Y %H:%M}"
    return f"{text}–{end.astimezone(tz):%H:%M}" if end else text


def _turns(first_idx: int, last_idx: int) -> str:
    return f"lượt {first_idx}" if first_idx == last_idx else f"lượt {first_idx}–{last_idx}"


def render(result: Result) -> str:
    """Kết quả dạng chữ cho agent đọc. Mã episode và số lượt nói dùng để hỏi tiếp."""
    tz = get_settings().tz
    scope = []
    if result.since:
        scope.append(f"từ {result.since.astimezone(tz):%d/%m/%Y %H:%M}")
    if result.until:
        scope.append(f"trước {result.until.astimezone(tz):%d/%m/%Y %H:%M}")
    if result.speakers:
        scope.append("người nói: " + ", ".join(result.speakers))
    lines = [f'Tìm "{result.query}"' + (f" ({'; '.join(scope)})" if scope else ""), ""]
    lines += [f"Lưu ý: {note}" for note in result.notes] + ([""] if result.notes else [])

    lines.append(f"## Nguyên văn: {len(result.passages)} đoạn")
    if not result.passages:
        lines.append("Không có đoạn nào trong phạm vi này.")
    for i, p in enumerate(result.passages, 1):
        where = "tóm tắt cả phiên" if p.summary else _turns(p.first_idx, p.last_idx)
        lines += ["", f"[{i}] {_span(p.started_at, p.ended_at)} · {p.origin} · episode {p.episode_id[:8]} · "
                      f"{where} · độ gần {p.score:.2f}", p.text]

    lines += ["", f"## Fact: {len(result.facts)}"]
    if not result.facts:
        lines.append("Không có fact nào trong phạm vi này.")
    for f in result.facts:
        lines += [""] + render_fact(f)
    return "\n".join(lines)


def render_fact(f: Fact) -> list[str]:
    """A fact and, on the next line, since when it holds and the turns it comes from."""
    tz = get_settings().tz
    said = "; ".join(f"{_span(s.started_at)}, {s.origin}, episode {s.episode_id[:8]} "
                     f"{_turns(s.first_idx, s.last_idx)}" for s in f.sources)
    valid = f"đúng từ {f.valid_at.astimezone(tz):%d/%m/%Y}" if f.valid_at else "không rõ đúng từ khi nào"
    if f.invalid_at:
        valid += f", ĐÃ HẾT ĐÚNG từ {f.invalid_at.astimezone(tz):%d/%m/%Y}"
    return [f"- {f.text}", f"  ({valid}; nguồn: {said})"]
