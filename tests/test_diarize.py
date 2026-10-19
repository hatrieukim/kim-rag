"""Kiểm tra phần đọc kết quả của pyannote bằng chính các lớp của pyannote, không cần tải model."""
from __future__ import annotations

import numpy as np


def test_doc_ket_qua_pyannote_4(env):
    from pyannote.audio.pipelines.speaker_diarization import DiarizeOutput
    from pyannote.core import Annotation, Segment

    from lifelog.diarize import PyannoteDiarizer

    full = Annotation()
    full[Segment(0.0, 4.0)] = "SPEAKER_01"
    full[Segment(3.5, 7.0)] = "SPEAKER_00"   # chồng lấn với lượt trước
    exclusive = Annotation()
    exclusive[Segment(0.0, 3.5)] = "SPEAKER_01"
    exclusive[Segment(3.5, 7.0)] = "SPEAKER_00"
    # Vector xếp theo thứ tự full.labels(), tức SPEAKER_00 rồi SPEAKER_01.
    vectors = np.array([[0.0, 1.0], [1.0, 0.0]])

    seen = {}

    def fake_pipeline(audio):
        seen["shape"] = tuple(audio["waveform"].shape)
        seen["rate"] = audio["sample_rate"]
        return DiarizeOutput(speaker_diarization=full, exclusive_speaker_diarization=exclusive,
                             speaker_embeddings=vectors)

    d = object.__new__(PyannoteDiarizer)
    d.pipeline, d.device, d.model = fake_pipeline, "cpu", "thu"
    result = d.run(np.zeros(16_000 * 7, dtype=np.float32), [])

    assert seen == {"shape": (1, 112_000), "rate": 16_000}
    assert result.turns == [(0.0, 3.5, "SPEAKER_01"), (3.5, 7.0, "SPEAKER_00")]   # dùng bản không chồng lấn
    assert result.embeddings == {"SPEAKER_00": [0.0, 1.0], "SPEAKER_01": [1.0, 0.0]}


def test_luu_dau_giong_vao_so(env, audio_dir, monkeypatch):
    """Vector giọng nói được lưu cạnh nhãn đã đổi tên, sẵn cho Sản phẩm 6."""
    from datetime import datetime, timezone

    from lifelog import pipeline
    from lifelog.db import connect
    from lifelog.diarize import DiarizeResult
    from lifelog.ingest import ingest_file

    class TwoSpeakers:
        name = "gia"

        def run(self, samples, speech):
            mid = len(samples) / 16_000 / 2
            return DiarizeResult(turns=[(0.5, mid, "X"), (mid, mid * 2 - 0.5, "Y")],
                                 embeddings={"X": [0.1, 0.2, 0.3], "Y": [0.9, 0.8, 0.7]}, model="gia-v1")

    monkeypatch.setattr(pipeline, "get_diarizer", lambda name: TwoSpeakers())
    ingest_file(audio_dir / "a.m4a", started_at=datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc), close_episode=True)
    pipeline.drain()

    with connect() as conn:
        got = conn.execute("SELECT label, embedding::text AS v, embedding_model FROM episode_speakers ORDER BY label").fetchall()
        ep = conn.execute("SELECT diarizer, export_path FROM episodes").fetchone()
    assert [(g["label"], g["embedding_model"]) for g in got] == [("SPEAKER_01", "gia-v1"), ("SPEAKER_02", "gia-v1")]
    assert got[0]["v"] == "[0.1,0.2,0.3]"
    assert ep["diarizer"] == "gia-v1"
    md = (env.data / ep["export_path"]).read_text(encoding="utf-8")
    assert "SPEAKER_01:" in md and "SPEAKER_02:" in md


def test_model_loads_from_the_local_cache_without_network():
    from huggingface_hub import constants

    from lifelog.diarize import _load_pipeline

    calls = []

    class Cached:
        @staticmethod
        def from_pretrained(model, token):
            calls.append(constants.HF_HUB_OFFLINE)
            return "pipeline"

    before = constants.HF_HUB_OFFLINE
    assert _load_pipeline(Cached, "m", "t") == "pipeline"
    assert calls == [True] and constants.HF_HUB_OFFLINE == before


def test_model_not_in_the_cache_is_downloaded():
    from huggingface_hub import constants

    from lifelog.diarize import _load_pipeline

    calls = []

    class NotCached:
        @staticmethod
        def from_pretrained(model, token):
            calls.append(constants.HF_HUB_OFFLINE)
            if constants.HF_HUB_OFFLINE:
                raise FileNotFoundError("not cached")
            return "pipeline"

    assert _load_pipeline(NotCached, "m", "t") == "pipeline"
    assert calls == [True, False]
