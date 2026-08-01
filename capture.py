#!/usr/bin/env python3
"""連機拍攝：按下快門，影像直接落到電腦上。

流程是 Sigma SDK 有文件的那條：

    1. SigmaSnapCommand 觸發拍攝
    2. 輪詢 CamCaptStatus 等到影像產生完成
    3. SigmaGetPictFileInfo2 取得檔案位址與大小
    4. SigmaGetBigPartialPictFile 分塊把資料抓下來

注意這裡沒有動到 DestToSave。那是獨立的設定（DataGroup3），決定影像要不要
「順便」寫進記憶卡 —— 下載一律是「跟相機要它 buffer 裡的資料」。
把兩者說成有因果關係是錯的。實測 DestToSave 為 Null 時照樣下載得到。

影像資料庫的模型（2026-08 於 fp 韌體 5.02 實測確認）：

    - 待取的項目佔用 [ImageDBHead, ImageDBTail) 這個半開區間。
    - 一次拍攝新增的項目，編號就是**拍攝前**的 ImageDBTail，之後 tail +1。
    - CamCaptStatus 是分項目的：查某個編號就得到那一筆的狀態。
    - ClearImageDBSingle 釋放一筆，head 隨之前進。
    - 項目沒釋放，下一次拍攝就不會觸發快門 —— 相機不出聲、不曝光，但
      tail 仍會 +1，所以「tail 前進」不代表拍成了。

先前「拍第一張成功、之後全部失敗」是這裡的 falsy-zero 造成的：第一張的
項目編號是 0，而 `if not image_id` 把合法的 0 當成沒取到，改去清拍攝後的
tail（1，不存在），於是項目 0 永遠洩漏、擋死後續所有拍攝。第二張起
tail_before >= 1 反而會誤打誤撞清對，所以只有開機後第一張會踩到。

同一個錯誤也讓失敗被誤報成成功：輪詢時會先看 slot 0，而 slot 0 停在上一
張的完成狀態，於是迴圈第一次檢查就「完成」，接著讀到上一張的
PictFileInfo2 —— 回報的檔名與大小與前一張完全相同。判斷成功不能看 slot 0，
要看這次拍攝自己那一筆。

錄影沒有對應的實作。opcode 存在（SigmaGetPartialMovieFile = 0x9037），
但 sigma-ptpy 沒有包，參數格式也沒有文件 —— 要做得先像挖 DataGroupMovie
那樣反推一次。
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigma_ptpy.enum import CaptStatus, CaptureMode
from sigma_ptpy.schema import SnapCommand

#: 單次 GetBigPartialPictFile 的請求大小。協定上限是 0x8000000，但一次要太多
#: 會讓單一 USB transaction 佔住相機太久 —— 這個 bridge 還要同時跑 live view。
CHUNK_BYTES = 1 << 20

#: 等待影像產生完成的上限
CAPTURE_TIMEOUT_S = 30.0

#: 目前進行到哪一步，(步驟名稱, 起始時間) 或 None。
#: 拍攝卡住時 PTP 全部沒反應，但這個變數不碰相機就讀得到 —— /api/status
#: 因此還答得出「卡在哪一個呼叫」，那是從外面唯一能取得的線索。
CURRENT_STEP: tuple[str, float] | None = None


def _step(name: str | None) -> None:
    global CURRENT_STEP
    CURRENT_STEP = (name, time.monotonic()) if name else None


def current_step() -> dict | None:
    """給 /api/status 用：目前卡在哪一步、卡了多久。"""
    step = CURRENT_STEP
    if step is None:
        return None
    return {"step": step[0], "seconds": round(time.monotonic() - step[1], 1)}


_DONE = {"ImageGenCompleted", "ImageDataStorageCompleted", "ShootSuccess"}
_FAILED = {"ImageGenFailed", "Failed", "BufferFull", "Interrupted", "AFFailed"}


class CaptureError(RuntimeError):
    """拍攝或下載失敗。"""


def _text(value) -> str:
    """PictFileInfo2 的字串欄位是 CString，解出來是 bytes。

    直接 str() 會得到 "b'SDIM0001.JPG'" —— 檔名就會用那個字面值寫到磁碟上。
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).split(b"\x00")[0].decode("ascii", "replace")
    return "" if value is None else str(value)


@dataclass
class CapturedImage:
    filename: str
    path_name: str
    format: str
    width: int
    height: int
    size: int
    data: bytes | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename, "path": self.path_name,
            "format": self.format, "width": self.width, "height": self.height,
            "size": self.size, "downloaded": self.data is not None,
        }


def _status(cam, image_id: int = 0):
    try:
        return cam.get_cam_capt_status(image_id)
    except Exception as e:
        raise CaptureError(f"讀取拍攝狀態失敗：{e}") from e


def _status_name(st) -> str:
    status = getattr(st, "CaptStatus", None)
    return getattr(status, "name", str(status))


def pending_range(cam) -> tuple[int, int]:
    """回傳 (head, tail)。兩者相等代表沒有待釋放的項目。"""
    st = _status(cam, 0)
    return (int(getattr(st, "ImageDBHead", 0) or 0),
            int(getattr(st, "ImageDBTail", 0) or 0))


def release_pending(cam) -> int:
    """釋放 [head, tail) 裡所有還沒被取走的項目，回傳釋放了幾筆。

    正常流程不會留下殘留 —— 每次拍攝都會在 finally 釋放自己那一筆。會用到
    這裡通常是上一輪中途死掉。留著項目的話下一次拍攝不會觸發快門，所以
    開拍前先清乾淨。
    """
    head, tail = pending_range(cam)
    for image_id in range(head, tail):
        clear_slot(cam, image_id)
    return max(0, tail - head)


def clear_slot(cam, image_id: int = 0) -> None:
    """清掉相機影像資料庫裡的一筆拍攝結果。

    失敗不丟例外 —— 這個指令的 payload 在 sigma-ptpy 裡註明 undocumented，
    不同韌體未必吃，而清不掉不代表這次拍攝不能進行。
    """
    try:
        cam.clear_image_db_single(image_id)
    except Exception as e:
        print(f"警告：清除影像資料庫項目 {image_id} 失敗：{e}", file=sys.stderr)


def wait_until_done(cam, image_id: int,
                    timeout_s: float = CAPTURE_TIMEOUT_S) -> str:
    """等指定的那一筆拍攝結果完成，回傳狀態名稱。

    只看 image_id 這一筆。**不要**順便看 slot 0 —— slot 0 會停在上一張的
    完成狀態，看它會讓迴圈第一次檢查就誤判完成，把失敗報成成功。

    Raises:
        CaptureError: 相機回報失敗狀態，或等到逾時。
    """
    deadline = time.monotonic() + timeout_s
    last = "?"
    while time.monotonic() < deadline:
        name = _status_name(_status(cam, image_id))
        if name in _DONE:
            return name
        if name in _FAILED:
            raise CaptureError(f"相機回報拍攝失敗：{name}（影像 {image_id}）")
        if name != "Cleared":
            last = name
        time.sleep(0.2)
    raise CaptureError(f"等待影像產生逾時（影像 {image_id}，最後狀態 {last}）")


def download(cam, info) -> bytes:
    """把相機緩衝區裡的影像分塊抓下來。

    Raises:
        CaptureError: 相機提前停止回傳資料。
    """
    total = int(info.FileSize)
    address = int(info.FileAddress)
    out = bytearray()
    while len(out) < total:
        want = min(CHUNK_BYTES, total - len(out))
        part = cam.get_big_partial_pict_file(address, len(out), want)
        got = bytes(part.PartialData[: part.AcquiredSize])
        if not got:
            raise CaptureError(
                f"下載中斷：已取得 {len(out)} / {total} bytes")
        out += got
    return bytes(out)


def capture(cam, save_dir: Path | None = None,
            autofocus: bool = False, fetch: bool = True,
            release_stale: bool = True, release: bool = True) -> CapturedImage:
    """拍一張，並（預設）把影像抓回電腦。

    Args:
        save_dir: 給了就把檔案寫進去，檔名沿用相機給的。
        autofocus: 預設關閉 —— 這個專案是用 PTP 手動控焦的，讓相機在拍攝前
            跑一次 AF 會把設好的焦點位置搶走。
        fetch: False 只拍不抓，用於「存在記憶卡就好」的情況。
        release_stale: 開拍前先釋放前一輪殘留的項目。殘留會讓快門不動作，
            所以預設開啟。關掉只在要觀察相機對殘留的原始反應時有用。
        release: 取走後釋放自己這一筆。關掉會擋死下一次拍攝，只供實驗用。

    Raises:
        CaptureError: 拍攝失敗、逾時、或下載中斷。
    """
    _step("release_stale")
    if release_stale:
        release_pending(cam)

    # 新的項目就落在拍攝前的 tail。這裡不能用「拍完再讀 tail」——那是 +1
    # 之後的值，指向還不存在的下一格。
    _step("pending_range")
    _, image_id = pending_range(cam)

    mode = CaptureMode.GeneralCapt if autofocus else CaptureMode.NonAFCapt
    _step("snap_command")
    cam.snap_command(SnapCommand(CaptureMode=mode, CaptureAmount=1))
    _step(f"wait_until_done(id={image_id})")
    wait_until_done(cam, image_id)

    _step("get_pict_file_info2")
    try:
        info = cam.get_pict_file_info2()
    except Exception as e:
        raise CaptureError(f"取得影像資訊失敗：{e}") from e

    image = CapturedImage(
        filename=_text(getattr(info, "FileName", "")) or "capture",
        path_name=_text(getattr(info, "PathName", "")),
        format=_text(getattr(info, "PictureFormat", "")),
        width=int(getattr(info, "SizeX", 0) or 0),
        height=int(getattr(info, "SizeY", 0) or 0),
        size=int(getattr(info, "FileSize", 0) or 0),
    )
    try:
        if fetch and image.size:
            _step(f"download({image.size:,} bytes)")
            image.data = download(cam, info)
            if save_dir is not None:
                save_dir.mkdir(parents=True, exist_ok=True)
                (save_dir / image.filename).write_bytes(image.data)
    finally:
        # 一定要釋放，即使下載失敗 —— 沒釋放的話下一次拍攝快門不會動作。
        _step("clear_slot")
        if release:
            clear_slot(cam, image_id)
        _step(None)
    return image
