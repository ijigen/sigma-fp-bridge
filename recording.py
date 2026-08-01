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


def movie_file_info_raw(cam, image_id: int = 0) -> bytes:
    """發 SigmaGetMovieFileInfo (0x9036)，取回某一筆影片項目的資訊。

    參數是影像資料庫的項目編號，跟 get_cam_capt_status(image_id) 同一套 ——
    每段錄影佔一個項目，編號從拍攝前的 tail 開始遞增。實測連錄三段（head/tail
    0/3）時，[0][1][2] 分別回報三個不同的檔名，[3] 回空殼。

    先前這裡寫死不帶參數（等同查 0），所以只有「進入 API 模式後的第一段」
    讀得到 —— 因為每次錄影前的 clear 會讓 head 前進，新項目落在 head 而不是
    0。那個症狀被誤判成「影片資訊會鎖住不更新」很久。

    sigma-ptpy 定義了這個 opcode 但沒有包成方法（同 PictFileInfo2 的處境，
    那個有包）。結構未文件化，但檔名是 ASCII，肉眼就看得出來 —— 對「這段
    錄出來是 MOV 還是 DNG」這個問題來說已經夠用。
    """
    from construct import Container

    ptp = Container(
        OperationCode="SigmaGetMovieFileInfo",
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=[int(image_id)],
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
    """最近一段影片的檔名 / 路徑。取不到就回 error 而不是丟例外。

    ⚠️ 這個回覆在同一個 PTP session 內會過期。實測：連錄五段之後它仍回報
    第一段的檔名與大小，直到 bridge 重啟（新 session）才更新。所以它可以
    用來知道「曾經錄過什麼」，**不能**用來判斷「剛才那段錄成了沒」——
    那要看 capture_status() 的 CaptStatus 與 ImageDBTail。

    對 CinemaDNG 更是完全沒用：它只描述 MOV。
    """
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


# ── 影片下載 ────────────────────────────────────────────────────────────
#
# 兩個 opcode 都沒有文件，sigma-ptpy 也沒包，是照實機位元組反推出來的。
#
# SigmaGetMovieFileInfo (0x9036) 的版面跟 PictFileInfo2 同一套設計
# （長度 → 數量 → 偏移表 → 記錄），但欄位是 64-bit，而且記錄裡**沒有**
# FileAddress —— 影片是直接用偏移讀的，不需要位址。
#
#     0   uint64  DataLength（整包長度）
#     8   uint64  FileCount
#     16  uint64  RecordOffset[FileCount]
#     記錄：
#       +0   char[8]  Format（"MOV"）
#       +8   uint64   FileSize
#       +16  uint64   PathNameOffset
#       +24  uint64   FileNameOffset
#
# SigmaGetPartialMovieFile (0x9037) 的參數形狀是 (0, offset, 0, length)。
# 第 2 個是位元組偏移（用重疊比對驗證：從 N 讀到的資料等於從 0 讀的第 N
# 個 byte 之後），第 4 個是長度。第 1、3 個必須是 0 —— 第 3 個推測是偏移的
# 高 32 位，設成 1 會直接失敗。

#: 單次請求的大小。實測 4 MB 也能正確取回，1 MB 是為了不讓單一 USB
#: transaction 佔住相機太久 —— 這個 bridge 還要同時跑 live view。
#:
#: 這個常數繞了很大一圈。曾經因為下載失敗而先後被歸咎於「4 MB 太大」「64 KB
#: 太大」「只有 4 KB 以下安全」，三個都是錯的：在健康的相機上，256 bytes 到
#: 4 MB 全部通過內容與重疊驗證。
#:
#: 真正的原因跟尺寸無關 —— 見下面 partial_movie() 的說明。
MOVIE_CHUNK_BYTES = 1 << 20

#: 影片檔案大小的合理上限。純粹是「解錯版面」的防線，跟照片那道同樣理由。
MAX_MOVIE_BYTES = 64 << 30


@dataclass
class MovieFile:
    filename: str
    path_name: str
    format: str
    size: int
    #: 影像資料庫的項目編號。只有 0 能下載，見 download_movie()。
    index: int = 0
    data: bytes | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"filename": self.filename, "path": self.path_name,
                "format": self.format, "size": self.size, "index": self.index,
                "downloadable": self.index == 0,
                "downloaded": self.data is not None}


def _u64(raw: bytes, off: int) -> int:
    return int.from_bytes(raw[off:off + 8], "little")


def parse_movie_file_info(raw: bytes) -> list[MovieFile]:
    """解 SigmaGetMovieFileInfo 的 payload。實機位元組反推，非官方文件。

    沒有影片可報時相機回 16 bytes 全零，這時回空清單。

    ⚠️ 「欄位是 64-bit」這件事只有一份單檔樣本佐證，而單檔樣本其實分辨不了 ——
    值都很小，little-endian 下讀 4 或 8 bytes 結果一樣。支持它的是版面本身：
    記錄偏移落在 16 而不是 12，count 後面跟著 4 個零。要真正確認得有一份
    兩個檔案的樣本（連錄兩段？），目前沒有。
    """
    if len(raw) < 24:
        return []
    count = _u64(raw, 8)
    if not 0 < count < 16:          # 不合理就當作沒解對，別硬湊
        return []
    out: list[MovieFile] = []
    for i in range(count):
        table = 16 + i * 8
        if table + 8 > len(raw):
            break
        rec = _u64(raw, table)
        if rec + 32 > len(raw):
            break
        out.append(MovieFile(
            filename=_cstring(raw, _u64(raw, rec + 24)),
            path_name=_cstring(raw, _u64(raw, rec + 16)),
            format=_cstring(raw, rec),
            size=_u64(raw, rec + 8),
        ))
    return out


def _cstring(raw: bytes, off: int) -> str:
    if not 0 <= off < len(raw):
        return ""
    end = raw.find(b"\x00", off)
    return raw[off:end if end >= 0 else len(raw)].decode("ascii", "replace")


def movie_entry_range(cam) -> tuple[int, int]:
    """影像資料庫裡待釋放項目的 [head, tail)。"""
    st = cam.get_cam_capt_status(0)
    return (int(getattr(st, "ImageDBHead", 0) or 0),
            int(getattr(st, "ImageDBTail", 0) or 0))


def movie_files(cam) -> list[MovieFile]:
    """列出資料庫裡每一筆影片項目，依項目編號。

    要逐筆問 —— 0x9036 一次只描述一個項目。
    """
    head, tail = movie_entry_range(cam)
    out: list[MovieFile] = []
    for index in range(head, tail):
        for one in parse_movie_file_info(movie_file_info_raw(cam, index)):
            one.index = index
            out.append(one)
    return out


def partial_movie(cam, offset: int, length: int) -> bytes:
    """讀影片檔案的一段。參數形狀 (0, offset, 0, length)。

    ⚠️ 相機會進入一種狀態，讓這個指令不再回傳影片資料，改回固定 122,868
    bytes 的內容。進入之後每次呼叫都一樣，不分尺寸也不分偏移。

    那不是壞掉的封包，是相機正式的回應：
      - 122,868 + 12 = 122,880 = 120 KB 整。12 bytes 正是 PTP over USB 資料
        相位的容器標頭（ContainerLength 4 + Type 2 + Code 2 + TransactionID 4），
        所以相機送回來的是剛好 120 KB 的完整容器。
      - ptpy 的 recv 會核對 SessionID / TransactionID / OperationCode 三者
        一致，不符就 __dev.reset() 並拋 PTPError。我們沒有收到錯誤，代表這是
        相機針對這個請求正式回覆的，不是讀到別人的封包或相位錯開。
      - 內容會跟著前一個指令的回應變（問過 MovieFileInfo 就以它的 payload
        開頭），後面接舊資料。

    合起來看：相機有一個 120 KB 的共用傳輸緩衝區，服務不了這個請求時就把
    緩衝區原樣送回，而沒有先填入影片資料。

    **重啟 bridge 沒有用** —— 那已經是新的 PTP session 了，狀態仍在。只有把
    相機斷電重開才會恢復。這件事花了很久才看出來，因為每次失敗我都只重啟
    bridge，於是一直在同一個壞狀態裡測，還連續三次把原因誤判成請求尺寸。

    ⚠️⚠️ **相機沒有影片項目可服務時呼叫這個指令，相機會 USB 逾時並掉線。**
    只能斷電重開。已經隔離出來、重現兩次：
      - 在 dest_to_save=InComputer 的錄影中呼叫（那種錄影不留下檔案）
      - 把資料庫項目全部清掉之後呼叫
    所以呼叫前一定要先確認索引 0 有影片，download_movie() 就是這樣做的。

    另外，**只有資料庫索引 0 的影片讀得到**。用檔尾邊界驗證過：連錄三段
    （項目 0/1/2，大小 29,352,984 / 29,251,032 / 26,482,656）時，讀到
    offset 29,352,984 才被拒 —— 正好是項目 0 那個檔案的長度。第一個參數不是
    索引（設 1、2 只會拿到 120 KB 的拒絕回應），MovieFileInfo(idx) 也不會
    「選定」要讀哪個。

    所以要下載第二段影片，得先 release + acquire 讓資料庫歸零，再錄。
    """
    from construct import Container

    ptp = Container(
        OperationCode="SigmaGetPartialMovieFile",
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=[0, int(offset), 0, int(length)],
    )
    return bytes(cam.recv(ptp).Data)


def download_movie(cam, movie: MovieFile, save_dir=None,
                   progress=None) -> MovieFile:
    """把一段影片抓回電腦。

    Args:
        progress: 每抓完一塊呼叫一次，參數是 (已完成 bytes, 總 bytes)。
            影片檔案很大，沒有進度回報的話看起來就像當掉了。

    Raises:
        RecordingError: 影片不在索引 0、大小不合理，或相機提前停止回傳。
    """
    if movie.index != 0:
        # 0x9037 只服務索引 0 那一筆。硬讀別筆不會拿到它，只會拿到索引 0 的
        # 資料，靜靜地存成錯的檔案。
        raise RecordingError(
            f"只有資料庫索引 0 的影片能下載，這一筆在索引 {movie.index}。"
            "先 release + acquire 讓資料庫歸零，再錄一段就會落在索引 0。")

    # 沒有影片可服務時呼叫 0x9037 會讓相機 USB 逾時掉線，只能斷電。
    # 這道檢查不是禮貌，是避免弄壞硬體狀態。
    if not movie_files(cam):
        raise RecordingError(
            "相機沒有任何影片項目 —— 這時發下載指令會讓相機掉線，已中止。")

    if not 0 < movie.size <= MAX_MOVIE_BYTES:
        raise RecordingError(
            f"影片大小看起來沒解對：{movie.size:,} bytes")

    out = bytearray()
    while len(out) < movie.size:
        want = min(MOVIE_CHUNK_BYTES, movie.size - len(out))
        got = partial_movie(cam, len(out), want)
        if not got:
            raise RecordingError(
                f"下載中斷：已取得 {len(out):,} / {movie.size:,} bytes")
        if len(got) > want:
            # 要 N 個 byte 卻拿到更多，代表讀到的是別的東西的殘留。
            # 這裡曾經寫成 got[:want] 默默截斷 —— 結果是大小正確、內容全錯
            # 的檔案，而且一路到最後才發現。寧可在第一塊就停。
            raise RecordingError(
                f"相機沒有回傳影片資料：要求 {want:,} bytes，卻回了 {len(got):,}。"
                "相機進入了不再服務影片傳輸的狀態 —— 重啟 bridge 沒有用，"
                "要把相機斷電重開。")
        out += got
        if progress is not None:
            progress(len(out), movie.size)

    movie.data = bytes(out)
    if save_dir is not None:
        from pathlib import Path
        import os
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        # 檔名是裝置送來的資料，不能直接接路徑
        base = os.path.basename(movie.filename.replace("\\", "/")).strip()
        movie.filename = base if base and not base.startswith(".") else "movie.MOV"
        (save_dir / movie.filename).write_bytes(movie.data)
    return movie
