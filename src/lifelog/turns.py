"""Biến kết quả tách người nói thành các lượt nói đủ gọn để gửi cho API chuyển chữ."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    speaker: str

    @property
    def length(self) -> float:
        return self.end - self.start


def build_turns(
    raw: list[tuple[float, float, str]],
    *,
    merge_gap_s: float = 1.0,
    max_s: float = 50.0,
    min_s: float = 0.4,
    split_slack_s: float = 4.0,
) -> list[Turn]:
    """Bỏ chồng lấn, gộp lượt liền nhau của cùng một người, cắt lượt quá dài, bỏ lượt quá ngắn."""
    ordered = sorted((Turn(float(s), float(e), spk) for s, e, spk in raw if e > s), key=lambda t: (t.start, t.end))

    # 1. Bỏ chồng lấn: lượt sau chỉ được tính từ chỗ lượt trước kết thúc.
    flat: list[Turn] = []
    cursor = -math.inf
    for t in ordered:
        start = max(t.start, cursor)
        if t.end - start <= 0:
            continue
        flat.append(Turn(start, t.end, t.speaker))
        cursor = t.end

    # 2. Gộp các lượt liền nhau của cùng một người, miễn là không vượt độ dài tối đa.
    merged: list[Turn] = []
    for t in flat:
        prev = merged[-1] if merged else None
        if prev and prev.speaker == t.speaker and t.start - prev.end <= merge_gap_s and t.end - prev.start <= max_s:
            merged[-1] = Turn(prev.start, t.end, prev.speaker)
        else:
            merged.append(t)

    # 3. Cắt đều các lượt còn dài hơn mức tối đa.
    pieces: list[Turn] = []
    for t in merged:
        if t.length <= max_s:
            pieces.append(t)
            continue
        # Chừa một khoảng để sau đó dời điểm cắt tới chỗ ngừng nói mà vẫn không vượt mức tối đa.
        n = math.ceil(t.length / max(1.0, max_s - split_slack_s))
        step = t.length / n
        for i in range(n):
            pieces.append(Turn(t.start + i * step, t.start + (i + 1) * step, t.speaker))

    # 4. Lượt quá ngắn gần như không chuyển thành chữ được.
    return [t for t in pieces if t.length >= min_s]


def relabel(turns: list[Turn]) -> tuple[list[Turn], dict[str, str]]:
    """Đổi nhãn thành SPEAKER_01, SPEAKER_02 theo thứ tự xuất hiện."""
    mapping: dict[str, str] = {}
    out = []
    for t in turns:
        if t.speaker not in mapping:
            mapping[t.speaker] = f"SPEAKER_{len(mapping) + 1:02d}"
        out.append(Turn(t.start, t.end, mapping[t.speaker]))
    return out, mapping


def snap_cuts(turns: list[Turn], samples, sample_rate: int, *, window_s: float = 4.0, frame_s: float = 0.1) -> list[Turn]:
    """Dời điểm cắt giữa hai mảnh liền nhau của cùng một người tới chỗ lặng nhất gần đó.

    Cắt đúng giữa một từ làm dịch vụ chuyển chữ nghe lặp hoặc mất chữ ở hai đầu.
    """
    import numpy as np

    out = list(turns)
    frame = max(1, int(frame_s * sample_rate))
    for i in range(len(out) - 1):
        a, b = out[i], out[i + 1]
        if a.speaker != b.speaker or abs(a.end - b.start) > 1e-6:
            continue
        lo = max(a.start + 1.0, a.end - window_s)
        hi = min(b.end - 1.0, a.end + window_s)
        if hi - lo < frame_s * 2:
            continue
        seg = np.asarray(samples[int(lo * sample_rate): int(hi * sample_rate)], dtype=np.float32)
        n = len(seg) // frame
        if n < 2:
            continue
        energy = (seg[: n * frame].reshape(n, frame) ** 2).mean(axis=1)
        cut = lo + (int(energy.argmin()) + 0.5) * frame_s
        out[i] = Turn(a.start, cut, a.speaker)
        out[i + 1] = Turn(cut, b.end, b.speaker)
    return out
