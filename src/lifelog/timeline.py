"""How a topic changed over time, for the `timeline` tool (product 9).

`search` answers "what is true about X" with the closest matches first. A timeline answers "how did X come
about": the facts about a topic in the order they became true, including the ones later replaced, next to the
passages where it was discussed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import graph, summarize
from .config import get_settings
from .search import Fact, Passage, _span, _turns, find_passages, parse_when, render_fact, with_sources

# Graphiti always returns as many facts as asked for. Facts much further in meaning from the topic than the
# closest one are dropped before the LLM picks the ones about the topic. Closeness alone does not separate them
# well: for "Redis", facts about MongoDB scored within 0.07 of the best match. Without the LLM, the narrower gap
# is used; on real data, facts about a topic of several words sat within about 0.1 of the closest.
CANDIDATE_GAP = 0.15
FACT_GAP = 0.1
CANDIDATES = 60
MAX_LIMIT = 40
EXCERPT = 400


@dataclass
class Entry:
    at: datetime
    fact: Fact | None = None
    passage: Passage | None = None


def _when(f: Fact) -> datetime:
    return f.valid_at or min(s.started_at for s in f.sources)


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EXCERPT else text[:EXCERPT].rsplit(" ", 1)[0] + " …"


def timeline(topic: str, *, since: str | None = None, until: str | None = None, limit: int = 20) -> str:
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("chủ đề đang trống")
    limit = max(1, min(int(limit), MAX_LIMIT))
    lo, hi = parse_when(since), parse_when(until, end=True)
    if lo and hi and lo >= hi:
        raise ValueError("mốc bắt đầu phải trước mốc kết thúc")
    tz = get_settings().tz
    notes: list[str] = []

    ranked: list[dict] = []
    if get_settings().graph == "none":
        notes.append("Kho fact đang tắt (GRAPH=none), chỉ có nguyên văn.")
    else:
        try:
            graph.check()
            ranked = graph.rank_facts(topic, CANDIDATES)
            ranked = [f for f in ranked if f["score"] >= ranked[0]["score"] - CANDIDATE_GAP] if ranked else []
        except Exception as exc:
            notes.append(f"Kho fact không hỏi được: {exc}.")
    facts: list[Fact] = []
    try:
        facts = with_sources(ranked, lo, hi)
    except Exception as exc:
        notes.append(f"Kho fact không hỏi được: {exc}.")
    scores = {f["fact"]: f["score"] for f in ranked}

    passages: list[Passage] = []
    try:
        passages, _ = find_passages(topic, lo, hi, [], max(5, limit // 2))
    except Exception as exc:
        notes.append(f"Kho nguyên văn không hỏi được: {exc}.")

    try:
        summarize.check()
        keep = set(summarize.pick_relevant(topic, [f.text for f in facts] + [p.text for p in passages]))
        passages = [p for i, p in enumerate(passages, len(facts)) if i in keep]
        facts = [f for i, f in enumerate(facts) if i in keep]
    except Exception as exc:
        best = max(scores.values(), default=0.0)
        facts = [f for f in facts if scores.get(f.text, 0.0) >= best - FACT_GAP]
        notes.append(f"Chưa lọc được bằng LLM ({exc}); fact chỉ được lọc theo độ gần nghĩa nên có thể lẫn mục lạc đề.")
    facts = facts[:limit]

    entries = sorted([Entry(_when(f), fact=f) for f in facts] + [Entry(p.started_at, passage=p) for p in passages],
                     key=lambda e: e.at)
    scope = []
    if lo:
        scope.append(f"từ {lo.astimezone(tz):%d/%m/%Y %H:%M}")
    if hi:
        scope.append(f"trước {hi.astimezone(tz):%d/%m/%Y %H:%M}")
    lines = [f'Dòng thời gian "{topic}"' + (f" ({'; '.join(scope)})" if scope else "")
             + f": {len(facts)} fact, {len(passages)} đoạn nguyên văn, cũ nhất trước", ""]
    lines += [f"Lưu ý: {n}" for n in notes] + ([""] if notes else [])
    if not entries:
        return "\n".join(lines + ["Không tìm thấy gì về chủ đề này."])

    day = None
    for e in entries:
        local = e.at.astimezone(tz)
        if local.date() != day:
            day = local.date()
            lines += [f"## {day:%d/%m/%Y}", ""]
        if e.fact:
            lines += render_fact(e.fact)
        else:
            p = e.passage
            where = "tóm tắt cả phiên" if p.summary else _turns(p.first_idx, p.last_idx)
            lines += [f"- Nguyên văn {_span(p.started_at, p.ended_at)} · {p.origin} · episode {p.episode_id[:8]} · "
                      f"{where} · độ gần {p.score:.2f}", f"  > {_excerpt(p.text)}"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
