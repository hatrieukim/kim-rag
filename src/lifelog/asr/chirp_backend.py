"""Chirp 3 qua Google Cloud Speech-to-Text V2, gọi đồng bộ (mỗi lượt nói dưới 60 giây)."""
from __future__ import annotations

import threading

from ..config import get_settings
from .base import AsrBackend, AsrText, AsrUnavailable, with_retry
from .gemini_backend import google_language


def _retryable(exc: Exception) -> bool:
    from google.api_core import exceptions as gexc

    return isinstance(exc, (gexc.ServiceUnavailable, gexc.DeadlineExceeded, gexc.ResourceExhausted,
                            gexc.InternalServerError, gexc.Aborted))


class ChirpBackend(AsrBackend):
    def __init__(self, model: str = "chirp_3") -> None:
        self.name = model
        self.model = model
        self._client = None
        self._lock = threading.Lock()

    def check(self) -> None:
        if not get_settings().google_cloud_project:
            raise AsrUnavailable("thiếu GOOGLE_CLOUD_PROJECT trong .env")

    def _speech(self):
        with self._lock:
            if self._client is None:
                from google.api_core.client_options import ClientOptions
                from google.cloud.speech_v2 import SpeechClient

                s = get_settings()
                # Tính hạn mức và tiền vào đúng project này, không theo project mặc định của gcloud.
                self._client = SpeechClient(
                    client_options=ClientOptions(
                        api_endpoint=f"{s.chirp_location}-speech.googleapis.com",
                        quota_project_id=s.google_cloud_project,
                    )
                )
            return self._client

    def transcribe(self, wav: bytes) -> AsrText:
        from google.cloud.speech_v2.types import cloud_speech

        s = get_settings()
        client = self._speech()
        request = cloud_speech.RecognizeRequest(
            recognizer=f"projects/{s.google_cloud_project}/locations/{s.chirp_location}/recognizers/_",
            config=cloud_speech.RecognitionConfig(
                auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
                language_codes=[google_language(s.asr_language)],
                model=self.model,
            ),
            content=wav,
        )

        def call() -> AsrText:
            resp = client.recognize(request=request, timeout=120)
            parts, scores = [], []
            for result in resp.results:
                if result.alternatives:
                    alt = result.alternatives[0]
                    parts.append(alt.transcript.strip())
                    if alt.confidence:
                        scores.append(float(alt.confidence))
            confidence = sum(scores) / len(scores) if scores else None
            return AsrText(text=" ".join(p for p in parts if p), confidence=confidence)

        return with_retry(call, retryable=_retryable)
