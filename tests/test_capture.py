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
    """影像產生了但相機回報失敗，要中止而不是繼續下載。"""
    from sigma_ptpy.enum import CaptStatus
    import types as _t
    cam = fake_camera.FakeCamera()
    # tail 要前進，流程才會走到查詢該筆狀態那一步
    def failing(image_id=0):
        return _t.SimpleNamespace(
            CaptStatus=(CaptStatus.ImageGenFailed if image_id
                        else CaptStatus.Cleared),
            ImageId=image_id, ImageDBHead=0,
            ImageDBTail=1 if cam.calls else 0, DestToSave=None)
    cam.get_cam_capt_status = failing
    try:
        capture.capture(cam)
    except capture.CaptureError as e:
        assert "ImageGenFailed" in str(e), e
        print("✓ 相機回報失敗狀態時中止")
    else:
        raise AssertionError("應該要丟 CaptureError")


def test_consecutive_captures_each_take_a_new_photo():
    """實測踩過：連拍三次只有第一次真的按了快門，三次卻都「下載成功」——
    因為讀到的是同一張的殘留 buffer。

    正確做法有兩個要件：以 ImageDBTail 前進判斷真的有新影像，
    以及下載後清除資料庫項目讓相機願意再拍。
    """
    cam = fake_camera.FakeCamera()
    for i in range(3):
        img = capture.capture(cam)
        assert img.data, f"第 {i+1} 次沒有拿到資料"
    assert cam.count("snap:NonAFCapt") == 3, "不是每次都真的拍了"
    assert cam.count("snap_rejected") == 0, "有拍攝被相機拒絕"
    assert cam.count("clear_db") == 3, "下載後沒有清除資料庫項目"
    print("✓ 連續拍攝每次都是新照片，且下載後釋放資料庫")


def test_an_unfetched_capture_does_not_block_the_next_one():
    """前一張沒有取走，也不該讓下一次拍不了。

    拍攝前的清除就是為了這個 —— 相機的影像資料庫只有一個位置在用時，
    沒清就會拒絕下一次拍攝（實測：連拍三次只有第一次按了快門）。
    """
    cam = fake_camera.FakeCamera()
    capture.capture(cam, fetch=False)      # 拍了但沒下載、沒釋放
    img = capture.capture(cam)             # 這次必須還能拍
    assert img.data, "前一張沒取走就拍不了下一張"
    assert cam.count("snap_rejected") == 0
    print("✓ 前一張未取走不會擋住下一次拍攝")


def test_consecutive_captures_each_take_a_new_photo():
    """實測踩過：連拍三次只有第一次真的按了快門，三次卻都「下載成功」——
    讀到的是同一張的殘留 buffer。

    拍攝前先清資料庫，看到的完成狀態才確定是自己的；取走後再清一次。
    """
    cam = fake_camera.FakeCamera()
    for i in range(3):
        img = capture.capture(cam)
        assert img.data, f"第 {i+1} 次沒有拿到資料"
    assert cam.count("snap:NonAFCapt") == 3, "不是每次都真的拍了"
    assert cam.count("snap_rejected") == 0, "有拍攝被相機拒絕"
    assert cam.count("clear_db") >= 3, "沒有清除資料庫項目"
    print("✓ 連續拍攝每次都是新照片")


def test_status_is_polled_for_the_new_image_not_slot_zero():
    """狀態綁在 image_id 上：查 0 一律回 Cleared。

    實測拍三張，ImageDBTail 每次都前進（影像確實產生），但一律查 slot 0
    的話狀態全程是 Cleared，等待邏輯以為什麼都沒發生而逾時。
    """
    cam = fake_camera.FakeCamera()
    img = capture.capture(cam)
    assert img.data
    # 新影像的 id 來自 ImageDBTail，狀態要用那個 id 查
    assert cam.db_tail >= 1
    print("✓ 用新影像的 id 查狀態，而非固定查 slot 0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_capture 全部通過")
