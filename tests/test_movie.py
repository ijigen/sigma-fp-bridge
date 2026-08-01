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


def test_record_clip_reports_new_files():
    """錄一小段，回報多出來哪些檔案。

    這是判斷錄影格式的探針：協定裡 record_format 只是個數字，機身主畫面
    也不顯示，但產出的檔案騙不了人 —— MOV 是單一檔案，CinemaDNG 是 .dng。
    """
    import recording
    cam = fake_camera.FakeCamera()
    cam.movie[50] = 1
    result = recording.record_clip(cam, seconds=0.2)
    assert len(result["new"]) == 1, result
    assert result["new"][0].filename.endswith(".MOV"), result["new"][0]

    cam.movie[50] = 2
    result = recording.record_clip(cam, seconds=0.2)
    assert result["new"][0].filename.endswith(".DNG"), result["new"][0]
    print("✓ 錄影探針回報新檔案（副檔名隨 record_format 改變）")


def test_recording_does_not_autofocus_by_default():
    """我們是靠 PTP 手動控焦的，錄影開始時不該讓相機跑 AF 搶走焦點。"""
    import recording
    from sigma_ptpy.enum import CaptureMode
    cam = fake_camera.FakeCamera()
    recording.start(cam)
    assert cam.count("snap:" + CaptureMode.StartRecMovie.name) == 1
    assert cam.count("snap:" + CaptureMode.StartRecMovieAF.name) == 0
    recording.stop(cam)
    print("✓ 錄影預設不觸發 AF")


def test_listing_accepts_plain_arrays():
    """ptpy 的陣列型回傳直接就是序列。

    先前這裡假設會包成帶 .StorageIDs / .ObjectHandles 的物件，假相機也照著
    包，於是測試全過但實機回 502。兩種形狀都要接。
    """
    import types

    import recording
    cam = fake_camera.FakeCamera()
    cam.files = [(101, "A001_C001.DNG", 0x3000, 4096)]

    assert recording.list_objects(cam)[0].filename == "A001_C001.DNG"

    # 換成包一層的形狀也要能用
    plain_ids, plain_handles = cam.get_storage_ids, cam.get_object_handles
    cam.get_storage_ids = lambda: types.SimpleNamespace(StorageIDs=plain_ids())
    cam.get_object_handles = lambda sid, **kw: types.SimpleNamespace(
        ObjectHandles=plain_handles(sid))
    assert recording.list_objects(cam)[0].filename == "A001_C001.DNG"
    print("✓ 檔案列舉同時接受裸陣列與包一層的回傳")


def test_only_confirmed_labels_are_published():
    """只公布實機確認過的標籤。

    先前我推測 record_format=2 是 CinemaDNG（因為只有它的畫質可設），
    實際錄一段才發現是 MOV。猜錯的標籤會被後面的人當事實。
    """
    assert M.VALUE_LABELS["record_format"] == {1: "CinemaDNG", 2: "MOV"}
    schema = {d["name"]: d for d in M.describe()}
    assert schema["record_format"]["labels"] == {1: "CinemaDNG", 2: "MOV"}
    assert schema["movie_resolution"]["labels"] == {1: "FHD", 2: "UHD"}
    assert schema["mov_image_quality"]["labels"] is None, "沒確認的不該有標籤"
    # 推測標籤的機制要留著，將來還有未命名的數值要走這條路
    assert isinstance(M.INFERRED_LABELS, dict)
    print("✓ 只發布實機確認過的數值標籤")


def test_clip_reports_whether_anything_was_produced():
    """SigmaGetMovieFileInfo 只描述 MOV，對 CinemaDNG 是瞎的。

    所以「到底錄成了沒」要看相機自己的拍攝狀態 —— ImageDBTail 前進就是
    產生了新項目，不分格式。先前只靠 movie_info 判斷，結果 CinemaDNG 那段
    看起來像沒錄成。
    """
    import recording
    cam = fake_camera.FakeCamera()
    cam.movie[50] = 2
    result = recording.record_clip(cam, seconds=0.2)
    assert result["produced_something"], result
    assert result["status_after"]["status"] == "MovieGenCompleted", result
    assert result["status_after"]["db_tail"] > result["status_before"]["db_tail"]
    print("✓ 用拍攝狀態判斷是否真的產生檔案（不分格式）")


def test_capture_status_errors_are_reported_not_raised():
    import recording

    class Dead:
        def get_cam_capt_status(self, image_id=0):
            raise RuntimeError("OperationNotSupported")

    got = recording.capture_status(Dead())
    assert "error" in got and "OperationNotSupported" in got["error"]
    print("✓ 拍攝狀態讀不到時回報錯誤而非丟例外")


def test_writes_use_the_cameras_own_encoding():
    """能對上合法清單時，原封送回相機宣告的原始分數。

    自行從浮點推導會引進「我算的分數跟相機講的不同」這個變因 —— 實測相機
    收到 2500/100 存成 25/1、收到 5000/100 存成 50/1，可見它會重新詮釋。
    排查寫入沒生效時，少一個變因要排除。
    """
    from sigma_ptpy.schema import DirectoryType as DT
    from sigma_ptpy.schema import _DirectoryEntrySchema
    enc = type("E", (_DirectoryEntrySchema,), {})()
    info5 = enc._encode([(161, DT.URational, [(2398, 100), (25, 1), (2997, 100)])])
    caps = M.read_capabilities(None, info5)
    assert caps["frame_rate"] == [23.98, 25.0, 29.97]
    assert M.RAW_CAPABILITIES["frame_rate"] == [(2398, 100), (25, 1), (2997, 100)]

    cam = fake_camera.FakeCamera()
    M.apply_settings(cam, {"frame_rate": 25}, caps)
    # 相機宣告的是 (25, 1)，就送 (25, 1)，不是自行推導的 (2500, 100)
    assert cam.movie[61] == (25, 1), cam.movie[61]

    # 對不上清單的值仍走一般推導（並被合法值檢查擋下）
    setting = M.MOVIE_BY_NAME["frame_rate"]
    assert M.encode(setting, 23.98) == (2398, 100)
    print("✓ 寫入時原封送回相機宣告的原始分數")


def test_probe_write_is_type_restricted_and_sparse():
    """探測寫入刻意不做合法值檢查（那正是要探測的），但型別要限制，
    而且只能動到指定的 tag。"""
    cam = fake_camera.FakeCamera()
    before = dict(cam.movie)
    M.write_tag(cam, 6, "UInt8", 1)
    assert cam.movie[6] == 1
    for k, v in before.items():
        if k != 6:
            assert cam.movie[k] == v, f"tag {k} 不該被動到"
    try:
        M.write_tag(cam, 6, "Float64", 1)
    except M.MovieSettingError as e:
        assert "Float64" in str(e)
        print("✓ 探測寫入限制型別，且只影響指定的 tag")
    else:
        raise AssertionError("不允許的型別應該被擋下")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_movie 全部通過")
