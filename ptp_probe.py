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


def recv_raw(cam, opcode: str, params: list[int] | None = None) -> bytes:
    """發一個 data-in 的 PTP 指令，回傳未經解析的 payload。

    Args:
        opcode: sigma-ptpy 的 OperationCode 名稱，例如 "SigmaGetMovieFileInfo"。
        params: 指令參數。空的話送不帶參數的版本 —— 有些指令的差別就在這裡。

    Raises:
        ProbeError: opcode 不認得，或相機拒絕。
    """
    from construct import Container

    if opcode not in known_opcodes():
        raise ProbeError(f"不認得的 opcode：{opcode}")
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
