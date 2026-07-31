#!/usr/bin/env python3
"""Sigma PTP IFD（directory）解析工具。

sigma-ptpy 把 DataGroup / CanSetInfo 的 payload 編成一種類 EXIF IFD 的結構：

    offset 0   uint32  DataLength      entries + data 區的總長度
    offset 4   uint32  DirectoryCount  entry 數量
    offset 8   entry[DirectoryCount]   每個 12 bytes

每個 entry：

    uint16  Tag     注意是十進位，不是 hex —— 見 README Gotcha 1
    uint16  Type    DirectoryType
    uint32  Count   元素個數
    bytes4  Value   Count*size <= 4 時值直接放這裡，否則這是 data 區的 offset

這個 module 刻意不 import sigma-ptpy，方便離線分析別人貼來的 raw bytes
（例如有人回報新鏡頭的 CanSetInfo5 dump，手上沒相機也能解）。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

# DirectoryType → (名稱, 單一元素的 byte 數, struct 格式)
# struct 格式是 None 代表不做數值解讀，只留 raw bytes。
# 名稱與大小對照 sigma_ptpy.schema 的 DirectoryType / _sizes，保持一致。
TYPE_INFO: dict[int, tuple[str, int, str | None]] = {
    1: ("UInt8", 1, "B"),
    2: ("String", 1, None),
    3: ("UInt16", 2, "H"),
    4: ("UInt32", 4, "I"),
    5: ("URational", 8, None),
    6: ("Int8", 1, "b"),
    7: ("Any8", 1, None),
    8: ("Int16", 2, "h"),
    9: ("Int32", 4, "i"),
    10: ("Rational", 8, None),
    11: ("Float32", 4, "f"),
    12: ("Float64", 8, "d"),
}

HEADER_SIZE = 8
ENTRY_SIZE = 12


@dataclass
class IFDEntry:
    """一筆 directory entry。"""

    index: int
    tag: int
    type: int
    count: int
    value_bytes: bytes           # entry 裡原始的那 4 bytes
    inline: bool                 # True = 值就在 value_bytes 裡
    data_offset: int | None      # inline=False 時，value_bytes 解出來的 offset
    data_bytes: bytes | None     # 從 data 區撈到的 bytes（撈不到就 None）
    values: list | None          # 解出來的數值（型別不支援或撈不到就 None）

    @property
    def type_name(self) -> str:
        return TYPE_INFO.get(self.type, (f"Unknown({self.type})", 0, None))[0]


@dataclass
class IFD:
    """一整個 payload 的解析結果。"""

    data_length: int
    directory_count: int
    entries: list[IFDEntry]
    payload: bytes
    data_base: int               # out-of-line offset 的基準點（見 DATA_BASE_ABSOLUTE）
    truncated: bool              # payload 短於 header 宣稱的長度


class IFDParseError(ValueError):
    """payload 連 header 都湊不齊時丟這個。"""


# Rational 是「兩個 32-bit 整數（分子, 分母）」。5 是無號、10 是有號。
# 相機用這個表示帶小數的範圍，例如曝光補償 -5.0 ~ +5.0 step 0.2 會編成
# (-50/10, 50/10, 2/10)。
RATIONAL_FORMATS = {5: "I", 10: "i"}


def _decode_values(type_: int, count: int, raw: bytes) -> list | None:
    """把 raw bytes 依 type 解成數值 list。無法解就回 None。

    Rational / URational 會解成 [(分子, 分母), ...]，其他型別解成數值 list。
    """
    if type_ in RATIONAL_FORMATS:
        if len(raw) < 8 * count:
            return None
        try:
            parts = struct.unpack("<" + RATIONAL_FORMATS[type_] * (2 * count), raw[: 8 * count])
        except struct.error:
            return None
        return [(parts[i * 2], parts[i * 2 + 1]) for i in range(count)]

    info = TYPE_INFO.get(type_)
    if info is None:
        return None
    _, size, fmt = info
    if fmt is None or size == 0:
        return None
    if len(raw) < size * count:
        return None
    try:
        return list(struct.unpack("<" + fmt * count, raw[: size * count]))
    except struct.error:
        return None


def format_values(values: list | None) -> str:
    """把解出來的值排成人看得懂的樣子。Rational 額外附上小數。"""
    if values is None:
        return "None"
    parts = []
    for v in values:
        if isinstance(v, tuple) and len(v) == 2:
            num, den = v
            parts.append(f"{num}/{den} ({num / den:g})" if den else f"{num}/{den}")
        else:
            parts.append(str(v))
    return "[" + ", ".join(parts) + "]"


# out-of-line 的值，其 offset 是從 payload 開頭（含 header）算起的絕對位置。
# 這不是猜的，是對照 sigma-ptpy 的實作確認過的：
#
#   decode: payload = rawdata[i:i + n]        i = int.from_bytes(entry.Value)
#   encode: offset  = index_section_size + len(data_section)
#           其中 index_section_size = 8 + len(triples) * 12
#
# 也就是說第一筆 out-of-line 資料的 offset 剛好等於 directory 的結尾，
# 但寫進去的數字本身是絕對的。tests/test_ifd.py 會拿真的 encoder 回歸驗證。
DATA_BASE_ABSOLUTE = 0


def parse_ifd(payload: bytes, data_base: int | None = None) -> IFD:
    """解析一段 IFD payload。

    Args:
        payload: 完整的 raw bytes（含 8 bytes header）。
        data_base: out-of-line 值的 offset 基準點。None = 用 DATA_BASE_ABSOLUTE。

    Raises:
        IFDParseError: payload 連 8 bytes header 都不到。
    """
    payload = bytes(payload)
    if len(payload) < HEADER_SIZE:
        raise IFDParseError(f"payload 只有 {len(payload)} bytes，連 header 都不夠")

    data_length, directory_count = struct.unpack("<II", payload[:HEADER_SIZE])

    # header 宣稱的 entry 數可能超過實際拿到的 bytes（傳輸被截斷 / 解析基準錯）。
    # 這裡以實際長度為準，不要讓 struct.error 炸掉整個 dump。
    max_entries = max(0, (len(payload) - HEADER_SIZE) // ENTRY_SIZE)
    usable = min(directory_count, max_entries)
    truncated = usable < directory_count

    entries_end = HEADER_SIZE + usable * ENTRY_SIZE

    # 第一輪：先把 entry 骨架解出來，順便收集所有 out-of-line 的 offset
    if data_base is None:
        data_base = DATA_BASE_ABSOLUTE

    raw_entries = []
    for i in range(usable):
        off = HEADER_SIZE + i * ENTRY_SIZE
        tag, type_, count, value_bytes = struct.unpack(
            "<HHI4s", payload[off : off + ENTRY_SIZE]
        )
        size = TYPE_INFO.get(type_, ("", 0, None))[1]
        total = size * count
        inline = total <= 4
        data_offset = None
        if not inline:
            data_offset = struct.unpack("<I", value_bytes)[0]
        raw_entries.append((i, tag, type_, count, value_bytes, inline, data_offset, total))

    # 第二輪：撈 data 區、解數值
    entries: list[IFDEntry] = []
    for i, tag, type_, count, value_bytes, inline, data_offset, total in raw_entries:
        data_bytes = None
        if inline:
            values = _decode_values(type_, count, value_bytes)
        else:
            values = None
            if data_base is not None and data_offset is not None:
                start = data_base + data_offset
                if 0 <= start < len(payload):
                    data_bytes = payload[start : start + total]
                    values = _decode_values(type_, count, data_bytes)
        entries.append(
            IFDEntry(
                index=i,
                tag=tag,
                type=type_,
                count=count,
                value_bytes=value_bytes,
                inline=inline,
                data_offset=data_offset,
                data_bytes=data_bytes,
                values=values,
            )
        )

    return IFD(
        data_length=data_length,
        directory_count=directory_count,
        entries=entries,
        payload=payload,
        data_base=data_base,
        truncated=truncated,
    )


def hexdump(data: bytes, width: int = 16, indent: str = "  ") -> str:
    """經典 hex + ASCII dump。"""
    lines = []
    for i in range(0, len(data), width):
        chunk = data[i : i + width]
        hex_str = " ".join(f"{b:02x}" for b in chunk)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{indent}{i:04x}: {hex_str:<{width * 3}s} | {ascii_str}")
    return "\n".join(lines)


def format_ifd(ifd: IFD, highlight_tags: tuple[int, ...] = ()) -> str:
    """把解析結果印成人看得懂的報告。

    Args:
        highlight_tags: 想特別標記的 tag（會在該行加 ← 註記）。
    """
    out = []
    out.append(f"Payload: {len(ifd.payload)} bytes")
    out.append(f"  DataLength     = {ifd.data_length}")
    out.append(f"  DirectoryCount = {ifd.directory_count}")
    if ifd.truncated:
        out.append(
            f"  ⚠ 只解得出 {len(ifd.entries)} 筆 entry —— payload 比 header 宣稱的短"
        )
    if ifd.data_base != DATA_BASE_ABSOLUTE:
        out.append(f"  data 區基準點（自訂）= +{ifd.data_base}")
    out.append("")
    out.append("Hex dump:")
    out.append(hexdump(ifd.payload))
    out.append("")
    out.append("Directory entries（Tag 同時列十進位與 hex，方便對 SDK 文件）:")

    for e in ifd.entries:
        raw_hex = " ".join(f"{b:02x}" for b in e.value_bytes)
        mark = "  ← 候選" if e.tag in highlight_tags else ""
        out.append(
            f"  [{e.index:2d}] Tag={e.tag:<6d} (0x{e.tag:04x})  "
            f"Type={e.type} ({e.type_name})  Count={e.count}{mark}"
        )
        shown = format_values(e.values)
        if e.inline:
            out.append(f"         raw=[{raw_hex}]  values={shown}")
        else:
            loc = f"offset={e.data_offset}"
            if e.data_bytes is None:
                loc += "（撈不到，offset 超出 payload 範圍）"
            out.append(f"         raw=[{raw_hex}]  {loc}  values={shown}")
            if e.data_bytes:
                out.append(f"         data=[{' '.join(f'{b:02x}' for b in e.data_bytes)}]")
    return "\n".join(out)


def find_tag(ifd: IFD, tag: int) -> IFDEntry | None:
    """找第一筆符合 tag 的 entry。"""
    for e in ifd.entries:
        if e.tag == tag:
            return e
    return None
