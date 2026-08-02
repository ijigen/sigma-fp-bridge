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

    Raises:
        ProbeError: opcode 名稱不認得，或相機拒絕。
    """
    from construct import Container

    if isinstance(opcode, str):
        if opcode not in known_opcodes():
            raise ProbeError(f"不認得的 opcode：{opcode}")
    elif isinstance(opcode, int):
        if not 0x1000 <= opcode <= 0xFFFF:
            raise ProbeError(f"opcode 超出範圍：0x{opcode:04x}")
    else:
        raise ProbeError("opcode 必須是名稱字串或數值")
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
