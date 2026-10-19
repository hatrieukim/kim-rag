"""Kho graph: Graphiti trên FalkorDB, rút người, việc và fact có thời gian hiệu lực.

Graphiti là tầng tính lại được. Mọi thứ ở đây đều dựng lại được từ Postgres, nên module này không
ghi gì riêng vào schema của Graphiti: chỉ gọi add_episode, remove_episode và search, and reads entities and
their facts for `people`.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import get_settings
from .embed import EMBED_DIM
from .gcloud import genai_client
from .windows import Window

# Graphiti mặc định gửi số liệu sử dụng ẩn danh về cho Zep. Tắt trước khi nạp thư viện.
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

# Thinking của Gemini tính chung vào giới hạn token đầu ra, nên để rộng cho JSON không bị cụt.
MAX_OUTPUT_TOKENS = 32768

INSTRUCTIONS = """\
Đây là bản ghi âm hội thoại đời thường bằng tiếng Việt, đã được máy chuyển thành chữ.
- Chữ có thể sai chính tả, nhất là tên riêng. Đừng tạo thực thể từ một từ rõ ràng là nghe nhầm.
- Nhãn dạng "Người nói 1 (bản ghi ab12cd34)" là một người chưa biết tên trong đúng bản ghi đó. Cùng số
  nhưng khác mã bản ghi là hai người khác nhau, không được gộp. Nếu trong hội thoại lộ tên thật của người
  đó thì ghi thành một fact, vẫn giữ nguyên nhãn. Nhãn vai trò như "nhân viên tổng đài (bản ghi ab12cd34)"
  cũng là một người chưa biết tên, chỉ trong bản ghi đó.
- Ưu tiên những điều còn dùng được về sau: ai hứa gì, hẹn lúc nào, quyết định gì, kế hoạch, sở thích,
  quan hệ giữa người với người, địa điểm, con số. Bỏ qua câu đưa đẩy.
"""

CODE_INSTRUCTIONS = """\
Đây là bản tóm tắt một phiên làm việc giữa người dùng và trợ lý lập trình Claude Code, viết bằng tiếng Việt.
- Thực thể đáng giữ: dự án, hệ thống, dịch vụ, công nghệ, file quan trọng, người và tổ chức được nhắc tới.
- Ưu tiên những điều còn dùng được về sau: đã quyết định gì và vì sao, đã làm xong gì, lỗi gì do đâu,
  việc gì còn dở. Gắn mỗi fact với đúng dự án của phiên.
- "Claude" là trợ lý, không phải một người. Đừng tạo fact kiểu "Claude đã trả lời".
"""

_LABEL = re.compile(r"^SPEAKER_(\d+)$")


class GraphUnavailable(RuntimeError):
    """Chưa dùng được kho graph vì thiếu cấu hình hoặc FalkorDB chưa chạy."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0  # gồm cả token suy nghĩ


@dataclass
class PartResult:
    uuid: str
    nodes: int
    edges: int
    seconds: float
    usage: Usage = field(default_factory=Usage)


def speaker_name(speaker: str, episode_id, known: bool = False) -> str:
    """Nhãn SPEAKER_01 chỉ có nghĩa trong một episode. Gắn mã episode để Graphiti không gộp nhầm người.

    Role labels of unnamed voices ("nhân viên tổng đài") get the same suffix; real names stay as they are.
    """
    if known:
        return speaker
    m = _LABEL.match(speaker)
    return f"Người nói {int(m.group(1))} (bản ghi {str(episode_id)[:8]})" if m else f"{speaker} (bản ghi {str(episode_id)[:8]})"


def check() -> None:
    import redis

    s = get_settings()
    if not s.google_cloud_project:
        raise GraphUnavailable("thiếu GOOGLE_CLOUD_PROJECT trong .env")
    try:
        client = redis.Redis(host=s.falkordb_host, port=s.falkordb_port, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        client.close()
    except redis.RedisError as exc:
        raise GraphUnavailable(
            f"không kết nối được FalkorDB ở {s.falkordb_host}:{s.falkordb_port}. Chạy `docker compose up -d`"
        ) from exc


def _metered(client, usage: Usage):
    """Đếm token của mọi lần Graphiti gọi LLM. Graphiti tự đếm thì bỏ sót token suy nghĩ."""
    inner = client.aio.models.generate_content

    async def generate_content(*args, **kwargs):
        resp = await inner(*args, **kwargs)
        meta = getattr(resp, "usage_metadata", None)
        usage.calls += 1
        if meta:
            usage.input_tokens += meta.prompt_token_count or 0
            usage.output_tokens += (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)
        return resp

    client.aio.models.generate_content = generate_content
    return client


def _graphiti(usage: Usage):
    """Dựng Graphiti mới cho mỗi lần chạy: các kết nối của nó gắn với vòng lặp asyncio đang chạy."""
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
    from graphiti_core.driver.falkordb_driver import FalkorDriver
    from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.gemini_client import GeminiClient

    s = get_settings()
    small = s.graph_llm_small_model or s.graph_llm_model
    config = LLMConfig(model=s.graph_llm_model, small_model=small)
    llm = _metered(genai_client(s.graph_llm_location), usage)
    return Graphiti(
        graph_driver=FalkorDriver(host=s.falkordb_host, port=s.falkordb_port, database=s.graph_name),
        llm_client=GeminiClient(config=config, client=llm, max_tokens=MAX_OUTPUT_TOKENS),
        embedder=GeminiEmbedder(
            config=GeminiEmbedderConfig(embedding_model=s.embed_model, embedding_dim=EMBED_DIM),
            client=genai_client(s.embed_location),
            batch_size=1,  # gửi nhiều đoạn một lần thì model gộp thành một vector
        ),
        # Graphiti bắt buộc có bộ xếp hạng lại, thiếu thì nó đòi khóa OpenAI. Chỉ đường tìm kiếm
        # nâng cao `search_` mới dùng tới; `search` thường thì không.
        cross_encoder=GeminiRerankerClient(config=LLMConfig(model=small), client=llm),
    )


async def _add_parts(parts, name, description, on_done, prose, instructions) -> None:
    from graphiti_core.nodes import EpisodeType

    usage = Usage()
    g = _graphiti(usage)
    try:
        await g.build_indices_and_constraints()
        for n, window in parts:
            before = Usage(usage.calls, usage.input_tokens, usage.output_tokens)
            started = time.monotonic()
            res = await g.add_episode(
                name=f"{name} phần {n + 1}",
                episode_body=window.text,
                source_description=description,
                reference_time=window.started_at,
                source=EpisodeType.text if prose else EpisodeType.message,
                group_id=get_settings().graph_name,
                custom_extraction_instructions=instructions or (CODE_INSTRUCTIONS if prose else INSTRUCTIONS),
            )
            on_done(n, window, PartResult(
                uuid=res.episode.uuid, nodes=len(res.nodes), edges=len(res.edges),
                seconds=time.monotonic() - started,
                usage=Usage(usage.calls - before.calls, usage.input_tokens - before.input_tokens,
                            usage.output_tokens - before.output_tokens),
            ))
    finally:
        await g.close()


def add_parts(parts: list[tuple[int, Window]], *, name: str, description: str,
              on_done: Callable[[int, Window, PartResult], None], prose: bool = False,
              instructions: str | None = None) -> None:
    """Nạp từng phần của một episode theo thứ tự. Xong phần nào gọi `on_done` ngay phần đó.

    `prose` là True khi phần đó là văn xuôi (bản tóm tắt phiên code) chứ không phải lời thoại.
    `instructions` replaces the default extraction instructions (Zalo chats have their own).
    """
    if parts:
        asyncio.run(_add_parts(parts, name, description, on_done, prose, instructions))


async def _remove_parts(uuids: list[str]) -> None:
    from graphiti_core.errors import NodeNotFoundError

    g = _graphiti(Usage())
    try:
        for uuid in uuids:
            try:
                await g.remove_episode(uuid)
            except NodeNotFoundError:
                pass  # graph đã bị xóa từ trước, coi như gỡ xong
    finally:
        await g.close()


def remove_parts(uuids: list[str]) -> None:
    """Gỡ các phần khỏi Graphiti, kèm những thực thể và fact chỉ riêng chúng nhắc tới."""
    if uuids:
        asyncio.run(_remove_parts(uuids))


async def _search(query: str, limit: int) -> list[dict]:
    g = _graphiti(Usage())
    try:
        edges = await g.search(query, group_ids=[get_settings().graph_name], num_results=limit)
        return [
            {"fact": e.fact, "valid_at": e.valid_at, "invalid_at": e.invalid_at, "parts": list(e.episodes or [])}
            for e in edges
        ]
    finally:
        await g.close()


def search_facts(query: str, limit: int = 8) -> list[dict]:
    """Các fact gần nghĩa nhất với câu hỏi. `parts` là mã các phần đã sinh ra fact đó."""
    return asyncio.run(_search(query, limit))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


async def _rank(query: str, limit: int) -> list[dict]:
    from .embed import embed_texts

    g = _graphiti(Usage())
    try:
        edges = await g.search(query, group_ids=[get_settings().graph_name], num_results=limit)
        vec = embed_texts([query])[0]
        # Search results come without their vectors; read them all in one query.
        records, _, _ = await g.driver.execute_query(
            "MATCH ()-[e:RELATES_TO]->() WHERE e.uuid IN $uuids RETURN e.uuid AS uuid, e.fact_embedding AS vec",
            uuids=[e.uuid for e in edges])
        vecs = {r["uuid"]: r["vec"] for r in records}
        out = []
        for e in edges:
            out.append({"fact": e.fact, "valid_at": e.valid_at, "invalid_at": e.invalid_at,
                        "parts": list(e.episodes or []), "score": _cosine(vec, vecs.get(e.uuid) or [])})
        return sorted(out, key=lambda f: -f["score"])
    finally:
        await g.close()


def rank_facts(query: str, limit: int = 50) -> list[dict]:
    """Like `search_facts`, with a `score` per fact: how close it is in meaning to the query, 0 to 1.

    Graphiti ranks by combining several searches and gives no score of its own, so a long list always comes
    back full. The score lets the caller drop facts that are not about the query.
    """
    return asyncio.run(_rank(query, limit))


def whole_word(name: str, query: str) -> bool:
    """`query` appears in `name` as whole words: "Tuấn" in "anh Tuấn", but "kim" not in "kim-rag"."""
    return re.search(rf"(?:^|\s){re.escape(query)}(?:$|\s)", name, re.IGNORECASE) is not None


async def _person(name: str, limit: int) -> dict:
    from graphiti_core.edges import EntityEdge

    g = _graphiti(Usage())
    try:
        records, _, _ = await g.driver.execute_query(
            "MATCH (n:Entity {group_id: $group}) WHERE toLower(n.name) CONTAINS $q "
            "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary",
            group=get_settings().graph_name, q=name.lower())
        exact = [r for r in records if r["name"].strip().lower() == name.lower()]
        nodes = exact or [r for r in records if whole_word(r["name"], name)][:5]
        facts: dict[str, dict] = {}
        for node in nodes:
            for e in await EntityEdge.get_by_node_uuid(g.driver, node["uuid"]):
                facts[e.uuid] = {"fact": e.fact, "valid_at": e.valid_at, "invalid_at": e.invalid_at,
                                 "parts": list(e.episodes or []), "created_at": e.created_at}
        newest = sorted(facts.values(), key=lambda f: f["valid_at"] or f["created_at"], reverse=True)[:limit]
        return {"nodes": [{"name": n["name"], "summary": n["summary"] or ""} for n in nodes], "facts": newest}
    finally:
        await g.close()


def person(name: str, limit: int = 15) -> dict:
    """What the graph knows about someone: the matching names with their written summary, and the newest facts
    that involve them. A name matches exactly, or else as whole words ("Tuấn" finds "anh Tuấn")."""
    return asyncio.run(_person(name.strip(), limit))
