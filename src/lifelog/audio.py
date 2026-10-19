"""Đọc và ghi audio. Mọi thứ bên trong pipeline là mono 16 kHz float32."""
from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16_000
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".opus", ".flac", ".mp4", ".caf", ".webm", ".aiff", ".aif"}


class AudioError(ValueError):
    """File không phải audio đọc được."""


def probe(path: Path) -> tuple[float, datetime | None]:
    """Trả về (thời lượng tính bằng giây, thời điểm ghi nếu file có ghi lại)."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name:format=duration,format_name:format_tags=creation_time",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffprobe không đọc được file: {proc.stderr.strip()[:200]}")
    info = json.loads(proc.stdout or "{}")
    if not info.get("streams"):
        raise AudioError("file không có luồng audio")
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError) as exc:
        raise AudioError("không xác định được thời lượng") from exc
    if info["format"].get("format_name") == "aac":
        # Raw ADTS AAC (the iPhone recorder) has no index: ffprobe only estimates the length from the bitrate.
        duration = len(decode(path)) / SAMPLE_RATE
    if duration <= 0:
        raise AudioError("audio rỗng")
    created = None
    raw = (info["format"].get("tags") or {}).get("creation_time")
    if raw:
        try:
            created = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            created = None
    return duration, created


def decode(path: Path) -> np.ndarray:
    """Giải mã bất kỳ định dạng nào ffmpeg đọc được thành mono 16 kHz float32."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg không giải mã được: {proc.stderr.decode(errors='replace').strip()[:200]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def decode_channels(path: Path, channels: int) -> np.ndarray:
    """Decode to 16 kHz float32 with `channels` columns. A mono file is copied to every column."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-vn", "-ac", str(channels), "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg could not decode: {proc.stderr.decode(errors='replace').strip()[:200]}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    return samples[: len(samples) // channels * channels].reshape(-1, channels).copy()


def wav_bytes(samples: np.ndarray) -> bytes:
    """Đóng gói thành file WAV PCM 16 bit trong bộ nhớ, để gửi cho API."""
    buf = io.BytesIO()
    sf.write(buf, samples, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def cut(samples: np.ndarray, start_s: float, end_s: float, pad_before_s: float = 0.0,
        pad_after_s: float = 0.0) -> np.ndarray:
    a = max(0, int((start_s - pad_before_s) * SAMPLE_RATE))
    b = min(len(samples), int((end_s + pad_after_s) * SAMPLE_RATE))
    return samples[a:b]
