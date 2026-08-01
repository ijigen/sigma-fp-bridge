#!/usr/bin/env python3
"""連機拍攝：按下快門，影像直接落到電腦上。

流程是 Sigma SDK 有文件的那條：

    1. SigmaSnapCommand 觸發拍攝
    2. 輪詢 CamCaptStatus 等到影像產生完成
    3. SigmaGetPictFileInfo2 取得檔案位址與大小
    4. SigmaGetBigPartialPictFile 分塊把資料抓下來

注意這裡沒有動到 DestToSave。那是獨立的設定（DataGroup3），決定影像要不要
「順便」寫進記憶卡 —— 下載一律是「跟相機要它 buffer 裡的資料」。
把兩者說成有因果關係是錯的。

尚未驗證：DestToSave 設成 InCamera 時，buffer 還讀不讀得到。可能一直都能讀
（Camera Control 模式本來就會 buffer），也可能要把 PC 列為目的地才有。

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


def wait_for_new_id(cam, tail_before: int | None,
                    timeout_s: float = 10.0) -> int:
    """等相機的影像資料庫多出一筆，回傳新那筆的 id。

    ImageDBTail 是資料庫尾端，拍成一張就會 +1。實測拍三張是 0→1→2→3。
    （先前以為靜態拍攝不會動它 —— 那次讀到 0 是因為相機在錄影模式。）

    Raises:
        CaptureError: 等到逾時都沒有新項目，代表相機沒有真的拍。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        tail = getattr(_status(cam), "ImageDBTail", None)
        if tail is not None and tail != tail_before:
            return int(tail)
        time.sleep(0.2)
    raise CaptureError(
        f"相機沒有產生新影像（ImageDBTail 停在 {tail_before}）")


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
    """等指定的影像產生完成。

    **一定要帶那張影像的 id。** 一律查 0 會永遠讀到 Cleared —— 實測拍三張，
    ImageDBTail 每次都前進（有影像），但 get_cam_capt_status(0) 全程回
    Cleared，於是等待邏輯以為什麼都沒發生。

    Raises:
        CaptureError: 相機回報失敗狀態，或等到逾時。
    """
    deadline = time.monotonic() + timeout_s
    last = "?"
    while time.monotonic() < deadline:
        st = _status(cam, image_id)
        last = _status_name(st)
        if last in _FAILED:
            raise CaptureError(f"相機回報拍攝失敗：{last}")
        if last in _DONE:
            return last
        time.sleep(0.2)
    raise CaptureError(f"等待影像 {image_id} 產生逾時（最後狀態 {last}）")


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
            autofocus: bool = False, fetch: bool = True) -> CapturedImage:
    """拍一張，並（預設）把影像抓回電腦。

    Args:
        save_dir: 給了就把檔案寫進去，檔名沿用相機給的。
        autofocus: 預設關閉 —— 這個專案是用 PTP 手動控焦的，讓相機在拍攝前
            跑一次 AF 會把設好的焦點位置搶走。
        fetch: False 只拍不抓，用於「存在記憶卡就好」的情況。

    Raises:
        CaptureError: 拍攝失敗、逾時、或下載中斷。
    """
    # 記下資料庫尾端 —— 拍成一張它就會 +1，這才是「真的拍了」的證據。
    # 不預先清除 slot 0：先前那樣做等於把自己要查的位置清空，然後
    # 回頭輪詢它，於是狀態永遠是 Cleared。
    tail_before = getattr(_status(cam), "ImageDBTail", None)

    mode = CaptureMode.GeneralCapt if autofocus else CaptureMode.NonAFCapt
    cam.snap_command(SnapCommand(CaptureMode=mode, CaptureAmount=1))
    image_id = wait_for_new_id(cam, tail_before)
    wait_until_done(cam, image_id)

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
    if not fetch or not image.size:
        return image

    image.data = download(cam, info)
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / image.filename).write_bytes(image.data)
    # 取走了就釋放位置。是否真的必要還沒證實 —— 實測連拍三次只有第一次
    # 按了快門，資料庫沒清是最像的解釋，但也可能另有原因。
    clear_slot(cam, image_id)
    return image
