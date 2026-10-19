"""Khe cắm chuyển giọng thành chữ. Mỗi dịch vụ là một lớp có cùng hình dạng."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass
class AsrText:
    text: str
    confidence: float | None = None


class AsrUnavailable(RuntimeError):
    """Dịch vụ chưa được cấu hình đủ để gọi (thiếu khóa, thiếu project)."""


class AsrBackend:
    name: str = ""
    model: str = ""

    def check(self) -> None:
        """Ném AsrUnavailable kèm lời giải thích nếu chưa gọi được."""

    def transcribe(self, wav: bytes) -> AsrText:  # pragma: no cover - giao diện
        raise NotImplementedError


def with_retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 1.5,
               retryable: Callable[[Exception], bool] = lambda exc: True) -> T:
    """Thử lại khi gặp lỗi tạm thời, chờ lâu dần giữa các lần."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if i == attempts - 1 or not retryable(exc):
                raise
            time.sleep(base_delay * (2 ** i))
    raise AssertionError("unreachable")
