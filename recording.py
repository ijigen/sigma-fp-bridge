#!/usr/bin/env python3
"""錄影控制與卡片檔案列舉。

錄影用 SigmaSnapCommand 的 StartRecMovie / StopRecMovie。列舉用標準 PTP 的
GetObjectHandles / GetObjectInfo —— 這兩個不是 Sigma 專屬指令，sigma-ptpy
本來就有包。

檔案列舉的用途不只是好玩：錄影格式（CinemaDNG / MOV）在協定裡只是個數字，
機身主畫面也不顯示，但錄出來的檔案騙不了人 —— MOV 是單一檔案，CinemaDNG
是一整包 .dng。錄一小段再看產出什麼，就能把那個數字對應到實際格式。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sigma_ptpy.enum import CaptureMode
from sigma_ptpy.schema import SnapCommand


class RecordingError(RuntimeError):
    """錄影指令失敗。"""


def _as_sequence(value, attribute: str) -> list:
    """ptpy 的陣列型回傳直接就是序列，不是包一層的物件。

    這裡兩種都接：實測 get_storage_ids() 回的是 [65537] 而不是帶
    .StorageIDs 的容器，但別的 ptpy 版本未必如此。
    """
    if value is None:
        return []
    if hasattr(value, attribute):
        value = getattr(value, attribute)
    if isinstance(value, (str, bytes)):
        return []
    try:
        return list(value)
    except TypeError:
        return []


@dataclass
class ObjectEntry:
    handle: int
    filename: str
    format_code: int
    size: int

    def __str__(self) -> str:
        return f"{self.filename} ({self.size} bytes, fmt 0x{self.format_code:04x})"


def start(cam, autofocus: bool = False) -> None:
    """開始錄影。

    Args:
        autofocus: True = 錄影開始時跑一次 AF。我們是靠 PTP 手動控焦的，
            預設關掉，免得相機把辛苦設好的焦點位置搶走。
    """
    mode = CaptureMode.StartRecMovieAF if autofocus else CaptureMode.StartRecMovie
    cam.snap_command(SnapCommand(CaptureMode=mode, CaptureAmount=1))


def stop(cam) -> None:
    """停止錄影。"""
    cam.snap_command(SnapCommand(CaptureMode=CaptureMode.StopRecMovie, CaptureAmount=1))


def capture_status(cam, image_id: int = 0) -> dict[str, Any]:
    """相機自己回報的拍攝狀態。

    這是判斷「剛才到底錄成了沒」唯一可靠的訊號：
      - CaptStatus 會走到 MovieGenCompleted，或停在 BufferFull / Failed /
        Interrupted 之類的失敗狀態。
      - ImageDBTail 是影像資料庫的尾端，錄成一段就會前進 —— 而且不分格式，
        CinemaDNG 也算，這點 SigmaGetMovieFileInfo 做不到（它只描述 MOV）。
    """
    try:
        st = cam.get_cam_capt_status(image_id)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    status = getattr(st, "CaptStatus", None)
    return {
        "status": getattr(status, "name", str(status)),
        "status_code": getattr(status, "value", None),
        "image_id": getattr(st, "ImageId", None),
        "db_head": getattr(st, "ImageDBHead", None),
        "db_tail": getattr(st, "ImageDBTail", None),
        "dest_to_save": str(getattr(st, "DestToSave", None)),
    }


#: 走到這些狀態就不用再等了
_TERMINAL_STATUS = {
    "MovieGenCompleted", "ImageGenCompleted", "ImageDataStorageCompleted",
    "ShootSuccess", "Cleared",
}
_FAILED_STATUS = {
    "BufferFull", "Failed", "ImageGenFailed", "Interrupted", "AFFailed", "CWBFailed",
}


def movie_file_info_raw(cam) -> bytes:
    """發 SigmaGetMovieFileInfo (0x9036)，取回最近一段影片的資訊。

    sigma-ptpy 定義了這個 opcode 但沒有包成方法（同 PictFileInfo2 的處境，
    那個有包）。結構未文件化，但檔名是 ASCII，肉眼就看得出來 —— 對「這段
    錄出來是 MOV 還是 DNG」這個問題來說已經夠用。
    """
    from construct import Container

    ptp = Container(
        OperationCode="SigmaGetMovieFileInfo",
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=[],
    )
    return bytes(cam.recv(ptp).Data)


def _ascii_runs(data: bytes, minimum: int = 4) -> list[str]:
    """把 bytes 裡的可列印字串抽出來。未文件化結構的土法煉鋼，但有效。"""
    out, current = [], []
    for b in data:
        if 32 <= b < 127:
            current.append(chr(b))
        else:
            if len(current) >= minimum:
                out.append("".join(current))
            current = []
    if len(current) >= minimum:
        out.append("".join(current))
    return out


def describe_last_movie(cam) -> dict[str, Any]:
    """最近一段影片的檔名 / 路徑。取不到就回 error 而不是丟例外。"""
    try:
        raw = movie_file_info_raw(cam)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"strings": _ascii_runs(raw), "raw_hex": raw.hex(), "bytes": len(raw)}


def list_objects(cam, limit: int = 40) -> list[ObjectEntry]:
    """列出卡片上的檔案（最新的在前）。

    取不到 info 的 handle 會被跳過而不是讓整份清單失敗 —— 正在寫入的檔案
    有可能查不到資訊。
    """
    entries: list[ObjectEntry] = []
    ids = _as_sequence(cam.get_storage_ids(), "StorageIDs")
    if not ids:
        # API 模式下相機不一定開放標準 PTP 的儲存列舉
        raise RecordingError("相機沒有回報任何 StorageID")
    for storage_id in ids:
        try:
            handles = _as_sequence(
                cam.get_object_handles(storage_id, all_formats=True), "ObjectHandles")
        except Exception:
            continue
        for handle in list(handles)[-limit:]:
            try:
                info = cam.get_object_info(handle)
            except Exception:
                continue
            entries.append(ObjectEntry(
                handle=handle,
                filename=getattr(info, "Filename", "?"),
                format_code=int(getattr(info, "ObjectFormat", 0) or 0),
                size=int(getattr(info, "ObjectCompressedSize", 0) or 0),
            ))
    entries.reverse()
    return entries


def record_clip(cam, seconds: float = 1.5) -> dict[str, Any]:
    """錄一小段，回報產出了哪些新檔案。

    這是「錄影格式那個數字到底是什麼」的探針：錄之前先記下檔案清單，
    錄完再比對，多出來的就是這次的產物。副檔名會直接告訴你答案。

    Returns:
        {"before": int, "new": [ObjectEntry], "seconds": float}
    """
    try:
        before = {e.handle for e in list_objects(cam)}
        listing_works = True
    except Exception:
        # 標準 PTP 列舉不見得能用 —— 還是要錄，改看 Sigma 自己的影片資訊
        before, listing_works = set(), False

    status_before = capture_status(cam)

    start(cam)
    try:
        time.sleep(max(0.2, seconds))
    finally:
        stop(cam)

    # 相機需要一點時間把檔案寫完並登錄
    new: list[ObjectEntry] = []
    if listing_works:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(1.0)
            try:
                new = [e for e in list_objects(cam) if e.handle not in before]
            except Exception:
                break
            if new:
                break
    else:
        time.sleep(2.0)

    # 等相機把檔案產生完 —— 這是唯一不分格式都有效的完成訊號
    status_after = capture_status(cam)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        name = status_after.get("status")
        if name in _TERMINAL_STATUS or name in _FAILED_STATUS or "error" in status_after:
            break
        time.sleep(0.5)
        status_after = capture_status(cam)

    tail_before = status_before.get("db_tail")
    tail_after = status_after.get("db_tail")
    produced = (
        bool(new)
        or (tail_before is not None and tail_after is not None and tail_after != tail_before)
    )

    return {
        "before": len(before),
        "seconds": seconds,
        "new": new,
        "listing_supported": listing_works,
        "movie_info": describe_last_movie(cam),
        "status_before": status_before,
        "status_after": status_after,
        "produced_something": produced,
    }
