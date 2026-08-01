#!/usr/bin/env python3
"""連機拍攝：拍一張並把影像抓回電腦。不需要相機。

假相機模擬的是實機驗證過的影像資料庫模型：待取項目佔 [head, tail)，
新的一筆落在拍攝前的 tail，沒釋放就會讓下一次拍攝的快門不動作。

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
    """相機回報失敗就要中止，不能繼續下載。"""
    from sigma_ptpy.enum import CaptStatus
    cam = fake_camera.FakeCamera()
    original = cam.snap_command

    def failing(data):
        original(data)
        cam.entries[cam.db_tail - 1] = CaptStatus.ImageGenFailed

    cam.snap_command = failing
    try:
        capture.capture(cam)
    except capture.CaptureError as e:
        assert "ImageGenFailed" in str(e), e
        print("✓ 相機回報失敗狀態時中止")
    else:
        raise AssertionError("應該要丟 CaptureError")


def test_the_first_capture_releases_entry_zero():
    """迴歸：開機後第一張的項目編號就是 0，而 0 是 falsy。

    先前寫成 `if not image_id: image_id = <拍攝後的 tail>`，於是第一張去清了
    不存在的 1，項目 0 永遠洩漏 —— 之後每次拍攝快門都不會動作。實測就是
    「重開機能拍一張、之後全滅」。第二張起 tail_before >= 1 反而會誤打誤撞
    清對，所以只有第一張會踩到，這個測試必須從全新的相機開始。
    """
    cam = fake_camera.FakeCamera()
    assert (cam.db_head, cam.db_tail) == (0, 0), "這個測試要從空的資料庫開始"
    capture.capture(cam)
    assert 0 not in cam.entries, "項目 0 沒有被釋放"
    assert cam.db_head == cam.db_tail, f"head 沒跟上：{cam.db_head}/{cam.db_tail}"
    print("✓ 開機後第一張正確釋放項目 0")


def test_consecutive_captures_each_fire_the_shutter():
    """迴歸：連拍三次曾經只有第一次真的按了快門，三次卻都「下載成功」。

    只數 snap_command 是抓不到的 —— 指令有送出、tail 也有前進，只是快門
    沒動作。要看實際曝光次數，以及每次拿到的是不是不同的影像。
    """
    cam = fake_camera.FakeCamera()
    names = []
    for i in range(3):
        img = capture.capture(cam)
        assert img.data, f"第 {i+1} 次沒有拿到資料"
        names.append(img.filename)
    assert cam.shutter_fires == 3, f"只真的拍了 {cam.shutter_fires} 次"
    assert len(set(names)) == 3, f"三次拿到同一張：{names}"
    assert cam.db_head == cam.db_tail, "資料庫項目累積了"
    print(f"✓ 連續拍攝每次都真的曝光：{names}")


def test_success_is_not_judged_from_slot_zero():
    """迴歸：判斷完成不能看 slot 0，否則失敗會被報成成功。

    slot 0 停在上一張的完成狀態時，輪詢一進迴圈就「完成」，接著讀到的是
    上一張的 PictFileInfo2 —— 回報的檔名與大小與前一張完全相同。
    """
    cam = fake_camera.FakeCamera()
    first = capture.capture(cam, release=False)      # 故意讓項目 0 留著
    assert 0 in cam.entries, "這個測試要留下未釋放的項目 0"
    before = cam.shutter_fires
    try:
        capture.capture(cam, release_stale=False)
    except capture.CaptureError:
        assert cam.shutter_fires == before, "快門不該動作"
        print(f"✓ 快門沒動作時如實報錯，不會回傳上一張（{first.filename}）")
    else:
        raise AssertionError("快門沒動作卻回報成功")


def test_stale_entries_are_released_before_shooting():
    """上一輪中途死掉留下的項目會擋住快門，開拍前要先清掉。"""
    cam = fake_camera.FakeCamera()
    capture.capture(cam, release=False)              # 模擬殘留
    assert cam.db_head < cam.db_tail
    before = cam.shutter_fires
    img = capture.capture(cam)                       # release_stale 預設開啟
    assert img.data and cam.shutter_fires == before + 1, "殘留沒被清掉"
    assert cam.db_head == cam.db_tail
    print("✓ 開拍前會釋放前一輪的殘留項目")


def test_an_absurd_file_size_is_refused_before_downloading():
    """迴歸：DNGAndJPEG 模式下 PictFileInfo2 回報 1,646,170,112 bytes。

    download() 照單全收，拿著一個同樣沒解對的位址去要 1.6 GB，相機從此不再
    回應 —— 整座橋停擺，要重啟才活得過來。實測那次 RSS 完全沒成長，代表
    連第一塊都沒拿到，不是「下載很慢」而是請求本身把相機打死了。

    這種值不可能是真的，所以在送出請求之前就要停手。
    """
    cam = fake_camera.FakeCamera()
    original = cam.get_pict_file_info2

    def absurd():
        info = original()
        info.FileSize = 1_646_170_112
        return info

    cam.get_pict_file_info2 = absurd
    before = cam.count("pict_chunk")
    try:
        capture.capture(cam)
    except capture.CaptureError as e:
        assert "沒解對" in str(e) and "1,646,170,112" in str(e), e
        assert cam.count("pict_chunk") == before, "已經開始下載了才發現不對"
        assert cam.db_head == cam.db_tail, "擋下之後資料庫項目沒被釋放"
        print("✓ 荒謬的 FileSize 在送出下載請求前就被擋下")
    else:
        raise AssertionError("應該要拒絕這個大小")


def test_shooting_without_fetching_works_even_with_a_bad_file_size():
    """破解未知版面要靠這條路：拍得成、不下載、項目正常釋放。

    大小檢查只擋下載。擋在更早的話，DNGAndJPEG 模式連拍都拍不了，
    也就拿不到原始位元組可以分析。
    """
    cam = fake_camera.FakeCamera()
    original = cam.get_pict_file_info2

    def absurd():
        info = original()
        info.FileSize = 1_646_170_112
        return info

    cam.get_pict_file_info2 = absurd
    img = capture.capture(cam, fetch=False)
    assert img.data is None and img.size == 1_646_170_112
    assert cam.shutter_fires == 1, "沒有真的拍"
    assert cam.db_head == cam.db_tail, "項目沒釋放"
    print("✓ fetch=0 在版面未知的模式下仍可安全取樣")


def test_a_normal_file_size_still_downloads():
    """上面的檢查不能把正常的 27 MB DNG 也擋掉。"""
    cam = fake_camera.FakeCamera()
    assert len(cam.pict_data) < capture.MAX_IMAGE_BYTES
    assert capture.capture(cam).data, "正常大小被誤擋"
    print(f"✓ 正常大小照常下載（上限 {capture.MAX_IMAGE_BYTES:,} bytes）")


def test_capture_releases_the_entry_even_without_downloading():
    """不下載也要釋放 —— 佔著就會讓下一次拍攝的快門不動作。"""
    cam = fake_camera.FakeCamera()
    capture.capture(cam, fetch=False)
    assert cam.db_head == cam.db_tail, "fetch=False 沒有釋放資料庫項目"
    before = cam.shutter_fires
    assert capture.capture(cam).data, "前一張沒下載就擋住了下一張"
    assert cam.shutter_fires == before + 1
    print("✓ 不下載也會釋放資料庫位置")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_capture 全部通過")
