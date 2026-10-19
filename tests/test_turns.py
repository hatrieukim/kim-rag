from lifelog.turns import build_turns, relabel


def test_gop_luot_lien_nhau_cua_cung_mot_nguoi():
    turns = build_turns([(0, 2, "A"), (2.5, 4, "A"), (4.2, 6, "B")], merge_gap_s=1.0)
    assert [(t.start, t.end, t.speaker) for t in turns] == [(0, 4, "A"), (4.2, 6, "B")]


def test_khong_gop_khi_khoang_lang_qua_dai():
    turns = build_turns([(0, 2, "A"), (5, 7, "A")], merge_gap_s=1.0)
    assert len(turns) == 2


def test_bo_chong_lan():
    turns = build_turns([(0, 5, "A"), (4, 8, "B")])
    assert [(t.start, t.end, t.speaker) for t in turns] == [(0, 5, "A"), (5, 8, "B")]


def test_cat_luot_qua_dai_va_khong_gop_vuot_muc():
    turns = build_turns([(0, 120, "A")], max_s=50)
    assert len(turns) == 3
    assert all(t.length <= 46 for t in turns)   # chừa 4 giây để dời điểm cắt
    assert turns[0].start == 0 and turns[-1].end == 120

    turns = build_turns([(0, 40, "A"), (40.5, 80, "A")], max_s=50, merge_gap_s=1.0)
    assert len(turns) == 2


def test_bo_luot_qua_ngan():
    turns = build_turns([(0, 0.2, "A"), (1, 3, "B")], min_s=0.4)
    assert [t.speaker for t in turns] == ["B"]


def test_doi_nhan_theo_thu_tu_xuat_hien():
    turns, mapping = relabel(build_turns([(0, 2, "SPK_7"), (3, 5, "SPK_2"), (6, 8, "SPK_7")]))
    assert [t.speaker for t in turns] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_01"]
    assert mapping == {"SPK_7": "SPEAKER_01", "SPK_2": "SPEAKER_02"}


def test_doi_diem_cat_toi_cho_lang():
    import numpy as np

    from lifelog.turns import Turn, snap_cuts

    rate = 1000
    samples = np.ones(60 * rate, dtype=np.float32)
    samples[int(31.2 * rate): int(31.6 * rate)] = 0.0      # một chỗ ngừng nói ở giây 31.2 tới 31.6
    turns = [Turn(0, 30, "A"), Turn(30, 60, "A"), Turn(60, 70, "B")]
    out = snap_cuts(turns, samples, rate)
    assert 31.2 <= out[0].end <= 31.6 and out[1].start == out[0].end
    assert out[2] == turns[2]                               # ranh giới giữa hai người khác nhau không bị đụng
    assert all(t.length <= 50 for t in out)
