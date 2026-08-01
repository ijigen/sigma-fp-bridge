#!/usr/bin/env python3
"""DataGroupMovie（CINE 模式設定）。不需要相機。

執行：python3 tests/test_movie.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import movie_settings as M
except ImportError as e:
    print(f"- 跳過：沒裝 sigma-ptpy（{e}）")
    sys.exit(0)

import fake_camera


def test_shutter_angle_encoding_matches_camera():
    """相機回報的合法角度清單第一個是 (112, 3600) = 11.2°。

    分母固定 3600 = 360.0° × 10，所以分子就是角度 × 10。整份清單換算出來
    是標準電影快門角度序列，這是 tag 7 身分的證據。
    """
    s = M.MOVIE_BY_NAME["shutter_angle"]
    assert M.decode(s, [(112, 3600)]) == 11.2
    assert M.decode(s, [(1728, 3600)]) == 172.8
    assert M.decode(s, [(3600, 3600)]) == 360.0
    assert M.encode(s, 180) == (1800, 3600)
    assert M.encode(s, 172.8) == (1728, 3600)
    print("✓ 快門角度編解碼（分子 = 角度 × 10）")


def test_frame_rate_encoding():
    s = M.MOVIE_BY_NAME["frame_rate"]
    assert M.decode(s, [(2398, 100)]) == 23.98
    assert M.decode(s, [(25, 1)]) == 25.0
    assert M.encode(s, 23.98) == (2398, 100)
    print("✓ 幀率編解碼")


def test_read_settings():
    cam = fake_camera.FakeCamera()
    got = M.read_settings(cam)
    assert got["shutter_angle"] == 172.8, got
    assert got["frame_rate"] == 23.98, got
    assert got["cinema_dng_quality"] == 12, got
    print("✓ 讀回錄影設定")


def test_apply_writes_only_requested_tags():
    """IFD 是稀疏的，沒帶到的 tag 不該被動到。"""
    cam = fake_camera.FakeCamera()
    M.apply_settings(cam, {"shutter_angle": 180})
    assert cam.movie[7] == (1800, 3600), cam.movie
    assert cam.movie[61] == (2398, 100), "幀率不該被動到"
    assert cam.movie[51] == 12, "位元深度不該被動到"
    print("✓ 只寫指定的 tag，其餘不動")


def test_rejects_values_camera_did_not_offer():
    """只寫相機自己宣告合法的值 —— 這是往未文件化的 DataGroup 寫入時
    唯一站得住腳的安全網。"""
    cam = fake_camera.FakeCamera()
    caps = {"shutter_angle": [11.2, 172.8, 180.0, 360.0]}
    try:
        M.apply_settings(cam, {"shutter_angle": 200}, caps)
    except M.MovieSettingError as e:
        assert "200" in str(e)
        assert cam.movie_write_log == [], "被擋下的值不該送到相機"
    else:
        raise AssertionError("200° 不在清單裡，應該被擋下")
    M.apply_settings(cam, {"shutter_angle": 180}, caps)
    assert cam.movie[7] == (1800, 3600)
    print("✓ 不在相機合法清單裡的值被擋下")


def test_unknown_name_rejected():
    cam = fake_camera.FakeCamera()
    try:
        M.apply_settings(cam, {"iso": 800})
    except M.MovieSettingError as e:
        assert "iso" in str(e)
        assert cam.movie_write_log == []
        print("✓ 不認得的錄影設定被擋下")
    else:
        raise AssertionError("應該要丟 MovieSettingError")


def test_capabilities_from_info5():
    """合法值清單要從 CanSetInfo5 解出來。"""
    from sigma_ptpy.schema import DirectoryType as DT
    from sigma_ptpy.schema import _DirectoryEntrySchema
    enc = type("E", (_DirectoryEntrySchema,), {})()
    info5 = enc._encode([
        (214, DT.URational, [(112, 3600), (1728, 3600), (1800, 3600)]),
        (161, DT.URational, [(2398, 100), (25, 1)]),
        (151, DT.UInt8, [12, 10, 8]),
    ])
    caps = M.read_capabilities(None, info5)
    assert caps["shutter_angle"] == [11.2, 172.8, 180.0], caps
    assert caps["frame_rate"] == [23.98, 25.0], caps
    assert caps["cinema_dng_quality"] == [12, 10, 8], caps
    print("✓ 從 CanSetInfo5 取得合法值清單")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_movie 全部通過")
