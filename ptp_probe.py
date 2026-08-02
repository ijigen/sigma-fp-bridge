#!/usr/bin/env python3
"""原始 PTP 探測：指定 opcode 和參數，把相機回的位元組原封不動拿回來。

存在的理由是成本。這個專案的協定工作幾乎都是同一個循環：猜一個參數、改
程式、重啟 bridge、看結果。重啟要人工介入，於是每個猜測都很貴，貴到會讓人
傾向「先寫好一個假設再驗證」—— 而那正是這個專案一路踩坑的來源。

有了這個端點，一次重啟之後就能連續試幾十種組合。

⚠️ 這是研究工具，不是一般功能：
  - 亂送 opcode 有機會讓相機不再回應（已經發生過，見 CameraStuck）
  - 只做 data-in（recv）。要送資料給相機的指令不從這裡走。
"""
from __future__ import annotations

from typing import Any


class ProbeError(RuntimeError):
    """探測失敗。"""


def known_opcodes() -> dict[str, int]:
    """sigma-ptpy 認得的 Sigma 專屬 opcode。"""
    import re
    import pathlib
    import sigma_ptpy

    out: dict[str, int] = {}
    root = pathlib.Path(sigma_ptpy.__file__).parent
    pattern = re.compile(r"(Sigma\w+)\s*=\s*(0x[0-9A-Fa-f]+)")
    for path in root.rglob("*.py"):
        for match in pattern.finditer(path.read_text()):
            out[match.group(1)] = int(match.group(2), 16)
    return out


def recv_raw(cam, opcode, params: list[int] | None = None) -> bytes:
    """發一個 data-in 的 PTP 指令，回傳未經解析的 payload。

    Args:
        opcode: sigma-ptpy 的 OperationCode 名稱（例如 "SigmaGetMovieFileInfo"），
            或直接給數值（例如 0x9010）來探未文件化的指令。ptpy 的
            OperationCode 是 construct 的 Enum 且 default=Pass，未知數值會原樣
            通過，所以數字送得出去。
        params: 指令參數。空的話送不帶參數的版本 —— 有些指令的差別就在這裡。

    ⚠️ 用數值探未知 opcode 是有風險的：沒辦法知道那是讀取還是寫入指令，而這台
    相機對不該收的指令的反應包括「USB 逾時後掉線」，只能靠斷電復原。

    已經掃過的空號（fp 韌體 5.02）：

      **0x902C → 變更通知（已驗證）。**85 bytes 的 IFD，tag 4 是一串讀取類
          opcode，而那串會隨相機操作改變：

              起點        [CaptStatus, 0x902E]
              改 ISO 後    [CaptStatus, 0x902E, GetCamDataGroupFocus]
              移動對焦後    [GetDG2, CaptStatus, 0x902E, GetCamCanSetInfo5]

          tag 1 跟著清單長度走（72 → 78 → 80），是 payload 大小。
          ⚠️ 保留：bridge 背景一直在輪詢對焦，所以「清單會變」是確定的，
          「哪一項對應哪個動作」不是 —— 沒辦法把單一項目歸因到單一操作。

          對 bridge 的實際價值：目前是定時重讀全部設定，有了它可以只重讀
          被點名的那幾組。

      **0x9039 → 有內容，25 bytes 的 IFD**：DataLength=20、一個 entry，
          tag 1 是 UInt8，值 0。單一旗標，意義不明。完整位元組：
          14 00 00 00 01 00 00 00 01 00 01 00 01 00 00 00 00 00 00 00 00 00 00 00 18

      0x902C 原始內容：

          tag 1  UInt32 = 84
          tag 2  UInt16 = 1
          tag 3  UInt16 = 0
          tag 4  UInt16 x6 = 0x9013 0x9014 0x9015 0x9023 0x9027 0x902E
          tag 5  UInt16 x0 = 空

          tag 4 全部是**讀取類 opcode**（GetCamDataGroup2/3/4/5、
          GetCamCaptStatus，加上未文件化的 0x902E）。這個形狀最像變更通知
          ——「這幾樣變了，該重讀」。相機 SDK 常見的模式，主機不必輪詢全部。
          尚未驗證：改一個設定之後再問一次，看清單會不會跟著變。

          附帶價值：0x902E **每一份清單裡都有**，但不論什麼時機、帶什麼參數
          （[0] [1] [0x902E] [1,0]）讀它都是空的。相機持續點名一個永遠沒有
          內容的東西，這點還沒解釋。

      0x9010, 0x9011, 0x901A → 正常完成但 payload 永遠 0 bytes。
          帶參數也試過（[0] [1] [2] [0,0] [1,0] [0xFFFFFFFF]），全部一樣。
      0x9020, 0x9021, 0x9025, 0x9026, 0x902E → 同上，空 payload。
      0x901E, 0x901F → AttributeError: Data（回應裡沒有資料相位）。
      0x901D, 0x9038 → USBTimeoutError，相機掉線，只能斷電。
      0x903A → USBTimeoutError，相機掉線。
      0x903B ~ 0x9042 → AttributeError: Data（沒有資料相位），而且相機安然無恙。

    掃描到此完整（0x9010 ~ 0x9042）。結論：除了 0x902C 之外沒有任何未文件化的
    指令提供得出內容，也沒有任何跟外接 SSD 或 USB 模式有關的東西。

    位置值得記：0x902C 就在 0x902D（GetPictFileInfo2）前面，0x902E 在後面。

    Raises:
        ProbeError: opcode 名稱不認得，或相機拒絕。
    """
    from construct import Container

    if isinstance(opcode, str):
        if opcode not in known_opcodes():
            raise ProbeError(f"unknown opcode: {opcode}")
    elif isinstance(opcode, int):
        if not 0x1000 <= opcode <= 0xFFFF:
            raise ProbeError(f"opcode out of range: 0x{opcode:04x}")
    else:
        raise ProbeError("opcode must be a name or a number")
    ptp = Container(
        OperationCode=opcode,
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=list(params or []),
    )
    try:
        return bytes(cam.recv(ptp).Data)
    except Exception as e:
        raise ProbeError(f"{type(e).__name__}: {e}") from e


def drain_events(cam, seconds: float = 2.0, interval: float = 0.05) -> list[dict]:
    """把相機發出的 PTP 事件收集一段時間。

    ptpy 有一條背景執行緒在輪詢事件端點並丟進佇列，這裡只是把佇列讀乾淨 ——
    **不對相機發任何指令**，所以錄影期間呼叫是安全的，也不會佔住 worker。

    用途：錄影當下相機有沒有主動說些什麼？影片檔案要到錄完才登錄，所以如果
    真有「邊錄邊取」的機制，事件是唯一還沒排除的入口。
    """
    import time

    out: list[dict] = []
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        try:
            evt = cam.event(wait=False)
        except Exception as e:
            out.append({"error": f"{type(e).__name__}: {e}"})
            break
        if evt is None:
            time.sleep(interval)
            continue
        out.append({
            "code": str(getattr(evt, "EventCode", None)),
            "session": getattr(evt, "SessionID", None),
            "transaction": getattr(evt, "TransactionID", None),
            "params": [int(x) for x in (getattr(evt, "Parameter", None) or [])],
        })
    return out


def describe(raw: bytes) -> dict[str, Any]:
    """把 payload 攤成方便肉眼比對的幾種視角。"""
    words = [int.from_bytes(raw[i:i + 4], "little")
             for i in range(0, len(raw) - 3, 4)]
    return {
        "bytes": len(raw),
        "raw_hex": raw.hex(),
        "ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in raw),
        "uint32_le": words,
        "all_zero": raw == b"\x00" * len(raw),
    }
