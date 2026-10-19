"""OpenAI GPT Transcribe qua POST /v1/audio/transcriptions."""
from __future__ import annotations

import httpx

from ..config import get_settings
from .base import AsrBackend, AsrText, AsrUnavailable, with_retry

URL = "https://api.openai.com/v1/audio/transcriptions"


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 409, 429) or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


class OpenAIBackend(AsrBackend):
    def __init__(self, model: str = "gpt-transcribe") -> None:
        self.name = model
        self.model = model
        self._client: httpx.Client | None = None

    def check(self) -> None:
        if not get_settings().openai_api_key:
            raise AsrUnavailable("thiếu OPENAI_API_KEY trong .env")

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(120.0, connect=15.0),
                headers={"Authorization": f"Bearer {get_settings().openai_api_key}"},
            )
        return self._client

    def transcribe(self, wav: bytes) -> AsrText:
        settings = get_settings()
        data: dict[str, object] = {"model": self.model}
        if self.model == "gpt-transcribe":
            # gpt-transcribe nhận danh sách ngôn ngữ và từ khóa; không gửi kèm `language` số ít.
            data["languages[]"] = [settings.asr_language]
            if settings.keywords:
                data["keywords[]"] = settings.keywords
        else:
            data["language"] = settings.asr_language
            if settings.keywords:
                data["prompt"] = ", ".join(settings.keywords)

        def call() -> AsrText:
            resp = self._http().post(URL, data=data, files={"file": ("turn.wav", wav, "audio/wav")})
            if resp.status_code >= 400:
                # Đưa lời giải thích của OpenAI vào thông báo lỗi để dễ sửa.
                detail = resp.text[:300]
                raise httpx.HTTPStatusError(f"OpenAI {resp.status_code}: {detail}", request=resp.request, response=resp)
            return AsrText(text=(resp.json().get("text") or "").strip())

        return with_retry(call, retryable=_retryable)
