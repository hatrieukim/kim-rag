"""Tìm các khoảng có tiếng nói bằng Silero VAD."""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from .audio import SAMPLE_RATE


@lru_cache(maxsize=1)
def _model():
    from silero_vad import load_silero_vad

    return load_silero_vad()


def speech_intervals(samples: np.ndarray) -> list[list[float]]:
    """Trả về [[bắt đầu, kết thúc], ...] tính bằng giây."""
    import torch
    from silero_vad import get_speech_timestamps

    if len(samples) < SAMPLE_RATE // 4:
        return []
    stamps = get_speech_timestamps(
        torch.from_numpy(samples),
        _model(),
        sampling_rate=SAMPLE_RATE,
        min_speech_duration_ms=250,
        min_silence_duration_ms=500,
        speech_pad_ms=200,
        return_seconds=True,
    )
    return [[round(float(s["start"]), 2), round(float(s["end"]), 2)] for s in stamps]


def subtract_spans(spans: list[list[float]], cuts) -> list[list[float]]:
    """Remove the `cuts` ([start, end] in seconds) from the speech `spans`; pieces under 50 ms are dropped."""
    out = []
    for s, e in spans:
        pieces = [(s, e)]
        for a, b in cuts:
            pieces = [p for (x, y) in pieces for p in ((x, min(y, a)), (max(x, b), y)) if p[1] - p[0] > 0.05]
        out.extend([round(x, 3), round(y, 3)] for x, y in pieces)
    return out
