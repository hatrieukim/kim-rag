"""Tách người nói. `pyannote` là đường chính; `none` coi cả episode là một người (để chạy thử)."""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from .audio import SAMPLE_RATE
from .config import get_settings

log = logging.getLogger("lifelog")


class DiarizerUnavailable(RuntimeError):
    """Chưa cấu hình đủ để tách người nói (thiếu token Hugging Face, chưa đồng ý điều khoản)."""


@dataclass
class DiarizeResult:
    turns: list[tuple[float, float, str]]
    embeddings: dict[str, list[float]] = field(default_factory=dict)
    model: str = ""


class NoneDiarizer:
    name = "none"

    def run(self, samples: np.ndarray, speech: list[list[float]]) -> DiarizeResult:
        return DiarizeResult(turns=[(s, e, "S0") for s, e in speech], model="none")


@contextlib.contextmanager
def _hf_offline():
    """Make the Hugging Face hub answer from the local cache only, with no network request at all.

    huggingface_hub reads `constants.HF_HUB_OFFLINE` on every request, so flipping it here is enough.
    """
    from huggingface_hub import constants

    before = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = True
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = before


def _from_pretrained(Pipeline, model: str, token: str):
    try:
        return Pipeline.from_pretrained(model, token=token)
    except TypeError:  # pyannote.audio 3.x
        return Pipeline.from_pretrained(model, use_auth_token=token)


def _load_pipeline(Pipeline, model: str, token: str):
    """Load the model from the local cache; go to the network only when it was never downloaded.

    Even when the files are cached, an online load asks huggingface.co about every file first, and a stalled
    connection there once froze the worker for hours.
    """
    try:
        with _hf_offline():
            pipeline = _from_pretrained(Pipeline, model, token)
        if pipeline is not None:
            return pipeline
    except Exception as exc:
        log.info("%s is not in the local cache (%s), downloading it", model, exc)
    return _from_pretrained(Pipeline, model, token)


class PyannoteDiarizer:
    name = "pyannote"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.hf_token:
            raise DiarizerUnavailable(
                "thiếu HF_TOKEN trong .env. Tạo token tại huggingface.co/settings/tokens và bấm đồng ý "
                f"điều khoản ở trang model {settings.pyannote_model}."
            )
        import torch
        from pyannote.audio import Pipeline

        try:
            pipeline = _load_pipeline(Pipeline, settings.pyannote_model, settings.hf_token)
        except Exception as exc:
            raise DiarizerUnavailable(
                f"không tải được {settings.pyannote_model}: {exc}. Kiểm tra token và việc đồng ý điều khoản."
            ) from exc
        if pipeline is None:
            raise DiarizerUnavailable(
                f"không tải được {settings.pyannote_model}. Kiểm tra token và việc đồng ý điều khoản."
            )

        device = settings.diarize_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        try:
            pipeline.to(torch.device(device))
        except Exception:
            device = "cpu"
            pipeline.to(torch.device("cpu"))
        self.pipeline = pipeline
        self.device = device
        self.model = settings.pyannote_model

    def run(self, samples: np.ndarray, speech: list[list[float]]) -> DiarizeResult:
        import torch

        # Đưa thẳng mảng mẫu vào để pyannote không phải tự giải mã file.
        audio = {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": SAMPLE_RATE}
        try:
            out = self.pipeline(audio)
        except RuntimeError:
            if self.device == "cpu":
                raise
            # Một số phép tính chưa chạy được trên chip Apple: lùi về CPU.
            self.pipeline.to(torch.device("cpu"))
            self.device = "cpu"
            out = self.pipeline(audio)

        embeddings = None
        if hasattr(out, "speaker_diarization"):  # pyannote.audio 4.x
            full = out.speaker_diarization
            annotation = getattr(out, "exclusive_speaker_diarization", None) or full
            embeddings = getattr(out, "speaker_embeddings", None)
        else:  # pyannote.audio 3.x trả thẳng Annotation
            full = annotation = out

        turns = [(float(seg.start), float(seg.end), str(label))
                 for seg, _, label in annotation.itertracks(yield_label=True)]

        vectors: dict[str, list[float]] = {}
        if embeddings is not None:
            for label, vec in zip(full.labels(), np.asarray(embeddings)):
                if np.all(np.isfinite(vec)) and np.linalg.norm(vec) > 1e-6:   # zero vector: voice without speech
                    vectors[str(label)] = [float(x) for x in vec]
        return DiarizeResult(turns=turns, embeddings=vectors, model=self.model)


@lru_cache(maxsize=2)
def get_diarizer(name: str):
    if name == "none":
        return NoneDiarizer()
    if name == "pyannote":
        return PyannoteDiarizer()
    raise DiarizerUnavailable(f"không có bộ tách người nói tên '{name}'. Có: pyannote, none")
