#!/usr/bin/env python3
"""連機拍攝：拍一張並把影像抓回電腦。不需要相機。

執行：python3 tests/test_capture.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import capture
except ImportError as e:
    print(f"- 跳過：沒裝 sigma-ptpy（{e}）")
    sys.exit(0)

import fake_camera


def test_capture_downloads_the_whole_image():
    """分塊下載必須湊齊 FileSize 宣告的長度。"""
    cam = fake_camera.FakeCamera()
    img = capture.capture(cam)
    assert img.data == cam.pict_data, "抓回來的資料跟相機的不一致"
    assert img.size == len(cam.pict_data)
    # 實機的字串欄位是 CString → bytes；str() 會得到 "b'SDIM0001.JPG'"，
    # 檔名就會用那個字面值寫到磁碟上
    assert img.filename == "SDIM0001.JPG", f"檔名沒有正確解碼：{img.filename!r}"
    assert img.format == "JPG" and img.path_name == "/DCIM/100SIGMA"
    assert cam.count("pict_chunk") >= 1
    print(f"✓ 完整下載 {img.size:,} bytes（{cam.count('pict_chunk')} 個分塊）")


def test_capture_writes_the_file():
    import tempfile
    cam = fake_camera.FakeCamera()
    with tempfile.TemporaryDirectory() as d:
        img = capture.capture(cam, Path(d))
        written = Path(d) / img.filename
        assert written.exists() and written.read_bytes() == cam.pict_data
    print("✓ 影像寫入指定目錄")


def test_capture_does_not_autofocus_by_default():
    """這個專案用 PTP 手動控焦，拍攝前跑 AF 會把焦點位置搶走。"""
    from sigma_ptpy.enum import CaptureMode
    cam = fake_camera.FakeCamera()
    capture.capture(cam, fetch=False)
    assert cam.count("snap:" + CaptureMode.NonAFCapt.name) == 1
    assert cam.count("snap:" + CaptureMode.GeneralCapt.name) == 0
    print("✓ 拍攝預設不觸發 AF")


def test_truncated_download_is_an_error():
    """相機提前停止回傳時要報錯，不能默默交出半張圖。"""
    cam = fake_camera.FakeCamera()
    original = cam.get_big_partial_pict_file

    def stops_early(store, start, length):
        if start == 0:
            return original(store, start, 8)
        return type(original(store, start, 0))(AcquiredSize=0, PartialData=b"")

    cam.get_big_partial_pict_file = stops_early
    try:
        capture.capture(cam)
    except capture.CaptureError as e:
        assert "下載中斷" in str(e)
        print("✓ 下載中斷時報錯，不交出半張圖")
    else:
        raise AssertionError("應該要丟 CaptureError")


def test_failed_capture_status_is_an_error():
    from sigma_ptpy.enum import CaptStatus
    import types as _t
    cam = fake_camera.FakeCamera()
    cam.get_cam_capt_status = lambda i=0: _t.SimpleNamespace(
        CaptStatus=CaptStatus.ImageGenFailed, ImageId=0, ImageDBHead=0,
        ImageDBTail=0, DestToSave=None)
    try:
        capture.capture(cam)
    except capture.CaptureError as e:
        assert "ImageGenFailed" in str(e)
        print("✓ 相機回報失敗狀態時中止")
    else:
        raise AssertionError("應該要丟 CaptureError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_capture 全部通過")
