"""Danh sách dịch vụ chuyển chữ. Đổi dịch vụ là đổi ASR_BACKEND trong .env."""
from __future__ import annotations

from typing import Callable

from .base import AsrBackend, AsrText, AsrUnavailable
from .chirp_backend import ChirpBackend
from .gemini_backend import GeminiBackend
from .openai_backend import OpenAIBackend

_FACTORIES: dict[str, Callable[[], AsrBackend]] = {
    "gpt-transcribe": lambda: OpenAIBackend("gpt-transcribe"),
    "gemini-3.5-transcribe": lambda: GeminiBackend("gemini-3.5-transcribe"),
    "chirp_3": lambda: ChirpBackend("chirp_3"),
}
_instances: dict[str, AsrBackend] = {}


def backend_names() -> list[str]:
    return list(_FACTORIES)


def register(name: str, factory: Callable[[], AsrBackend]) -> None:
    _FACTORIES[name] = factory
    _instances.pop(name, None)


def get_backend(name: str) -> AsrBackend:
    if name not in _FACTORIES:
        raise AsrUnavailable(f"không có dịch vụ chuyển chữ tên '{name}'. Có: {', '.join(_FACTORIES)}")
    if name not in _instances:
        _instances[name] = _FACTORIES[name]()
    return _instances[name]


__all__ = ["AsrBackend", "AsrText", "AsrUnavailable", "backend_names", "get_backend", "register"]
