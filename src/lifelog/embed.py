"""Đổi chữ thành vector bằng model embedding của Google, gọi qua project Google Cloud."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .asr.base import with_retry
from .config import get_settings
from .gcloud import genai_client, retryable

# Bảng `windows` khai báo cứng số chiều này. Model trả mặc định 3.072 chiều, vượt giới hạn
# 2.000 chiều của chỉ mục pgvector, nên xin bản rút gọn.
EMBED_DIM = 1536

_client = None
_lock = threading.Lock()


class EmbedUnavailable(RuntimeError):
    """Chưa gọi được dịch vụ embedding vì thiếu cấu hình."""


def check() -> None:
    if not get_settings().google_cloud_project:
        raise EmbedUnavailable("thiếu GOOGLE_CLOUD_PROJECT trong .env")


def _genai():
    global _client
    with _lock:
        if _client is None:
            _client = genai_client(get_settings().embed_location)
        return _client


def _embed_one(text: str) -> list[float]:
    from google.genai import types

    def call() -> list[float]:
        resp = _genai().models.embed_content(
            model=get_settings().embed_model,
            contents=text,
            config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
        )
        values = resp.embeddings[0].values
        if len(values) != EMBED_DIM:
            raise RuntimeError(f"model trả vector {len(values)} chiều, cần {EMBED_DIM}")
        return list(values)

    return with_retry(call, retryable=retryable)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Mỗi đoạn chữ một vector, giữ đúng thứ tự.

    Gọi từng đoạn một: gemini-embedding-2 gộp mọi đoạn gửi trong cùng một lần gọi thành MỘT vector.
    """
    check()
    if not texts:
        return []
    with ThreadPoolExecutor(max_workers=max(1, get_settings().embed_concurrency)) as pool:
        return list(pool.map(_embed_one, texts))


def vector_literal(vec: list[float]) -> str:
    """Dạng chữ mà pgvector nhận, dùng với `%s::vector`."""
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"
