"""Gemini 3.5 Transcribe.

Mặc định gọi qua project Google Cloud để tính vào credit Cloud. Nếu không đặt project mà có
GEMINI_API_KEY thì gọi qua khóa AI Studio.
"""
from __future__ import annotations

import threading

from ..config import get_settings
from ..gcloud import genai_client, http_options, retryable
from .base import AsrBackend, AsrText, AsrUnavailable, with_retry

GOOGLE_LANG = {"vi": "vi-VN", "en": "en-US"}


def google_language(code: str) -> str:
    return code if "-" in code else GOOGLE_LANG.get(code, code)


class GeminiBackend(AsrBackend):
    def __init__(self, name: str = "gemini-3.5-transcribe") -> None:
        self.name = name
        self.model = get_settings().gemini_model
        self._client = None
        self._lock = threading.Lock()

    def check(self) -> None:
        s = get_settings()
        if not s.google_cloud_project and not s.gemini_api_key:
            raise AsrUnavailable("thiếu GOOGLE_CLOUD_PROJECT (hoặc GEMINI_API_KEY) trong .env")

    def _genai(self):
        with self._lock:
            if self._client is None:
                s = get_settings()
                if s.google_cloud_project:
                    self._client = genai_client(s.gemini_location)
                else:
                    from google import genai

                    self._client = genai.Client(api_key=s.gemini_api_key, http_options=http_options())
            return self._client

    def transcribe(self, wav: bytes) -> AsrText:
        from google.genai import types

        s = get_settings()
        client = self._genai()
        transcription = {"language_codes": [google_language(s.asr_language)]}
        if s.keywords:
            transcription["custom_vocabulary"] = s.keywords
        config = types.GenerateContentConfig(
            audio_transcription_config=types.AudioTranscriptionConfig(**transcription)
        )

        def call() -> AsrText:
            resp = client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav")],
                config=config,
            )
            return AsrText(text=(resp.text or "").strip())

        return with_retry(call, retryable=retryable)
