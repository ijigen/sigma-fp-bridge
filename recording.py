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


def list_objects(cam, limit: int = 40) -> list[ObjectEntry]:
    """列出卡片上的檔案（最新的在前）。

    取不到 info 的 handle 會被跳過而不是讓整份清單失敗 —— 正在寫入的檔案
    有可能查不到資訊。
    """
    entries: list[ObjectEntry] = []
    for storage_id in cam.get_storage_ids().StorageIDs:
        try:
            handles = cam.get_object_handles(storage_id, all_formats=True).ObjectHandles
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
    before = {e.handle for e in list_objects(cam)}

    start(cam)
    try:
        time.sleep(max(0.2, seconds))
    finally:
        stop(cam)

    # 相機需要一點時間把檔案寫完並登錄
    new: list[ObjectEntry] = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(1.0)
        new = [e for e in list_objects(cam) if e.handle not in before]
        if new:
            break

    return {"before": len(before), "seconds": seconds, "new": new}
