#!/usr/bin/env python3
"""ifd.py 的解析測試。純資料，不需要相機。

執行：python3 tests/test_ifd.py
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ifd import ENTRY_SIZE, HEADER_SIZE, IFDParseError, find_tag, format_ifd, parse_ifd


def build(entries, data_blob=b""):
    """entries: [(tag, type, count, 4-byte value), ...]"""
    body = b"".join(struct.pack("<HHI4s", t, ty, c, v) for t, ty, c, v in entries)
    return struct.pack("<II", len(body) + len(data_blob), len(entries)) + body + data_blob


def test_inline_values():
    """set_focus_position(500) 實際會送出去的那種 payload。"""
    payload = build([
        (1, 1, 1, struct.pack("<B", 3) + b"\0\0\0"),     # FocusMode = MF
        (2, 1, 1, b"\x00\0\0\0"),                         # AFLock = Off
        (51, 1, 1, b"\x00\0\0\0"),                        # PreConstAF = Off
        (81, 3, 1, struct.pack("<H", 500) + b"\0\0"),     # FocusPosition = 500
    ])
    ifd = parse_ifd(payload)
    assert ifd.directory_count == 4
    assert find_tag(ifd, 81).values == [500]
    assert find_tag(ifd, 81).inline
    assert find_tag(ifd, 1).values == [3]
    print("✓ inline 值")


def test_focus_range_shape():
    """焦點範圍最可能的樣子：2 個 UInt16，剛好塞得進 4 bytes。"""
    ifd = parse_ifd(build([(658, 3, 2, struct.pack("<HH", 120, 3200))]))
    assert find_tag(ifd, 658).values == [120, 3200]
    print("✓ 焦點範圍（inline 2x UInt16）")


def test_roundtrip_against_real_encoder():
    """拿 sigma-ptpy 自己的 encoder 產生 payload，確認我們解得回來。

    這是最重要的一項：合成測資只能證明解析器自洽，這項才證明它跟真的
    相機協定對得上。沒裝 sigma-ptpy 的話跳過。
    """
    try:
        from sigma_ptpy.schema import DirectoryType as DT
        from sigma_ptpy.schema import _DirectoryEntrySchema
    except ImportError:
        print("- 跳過真實 encoder 回歸（沒裝 sigma-ptpy）")
        return

    encoder = type("E", (_DirectoryEntrySchema,), {})()
    payload = encoder._encode([
        (1, DT.UInt8, 3),                      # FocusMode
        (81, DT.UInt16, 500),                  # FocusPosition
        (658, DT.UInt16, [120, 3200]),         # 4 bytes -> inline
        (999, DT.UInt16, [10, 20, 30, 40]),    # 8 bytes -> out-of-line
        (1000, DT.UInt32, [1, 2, 3]),          # 12 bytes -> out-of-line
    ])
    ifd = parse_ifd(payload)
    expected = {1: [3], 81: [500], 658: [120, 3200],
                999: [10, 20, 30, 40], 1000: [1, 2, 3]}
    for tag, want in expected.items():
        entry = find_tag(ifd, tag)
        assert entry is not None, f"tag {tag} 不見了"
        assert entry.values == want, (tag, entry.values, want)
    assert find_tag(ifd, 658).inline is True    # 4 bytes 剛好塞得下
    assert find_tag(ifd, 999).inline is False   # 8 bytes 進 data 區
    assert find_tag(ifd, 1000).inline is False  # 12 bytes 進 data 區
    print("✓ 對真實 sigma-ptpy encoder 回歸（含 out-of-line）")


def test_out_of_line_values():
    """超過 4 bytes 的值放在 data 區，Value 欄位是從 payload 開頭算的絕對 offset。"""
    entries_end = HEADER_SIZE + ENTRY_SIZE
    data = struct.pack("<HHHH", 10, 20, 30, 40)
    ifd = parse_ifd(build([(658, 3, 4, struct.pack("<I", entries_end))], data_blob=data))
    entry = find_tag(ifd, 658)
    assert not entry.inline
    assert entry.values == [10, 20, 30, 40], entry.values
    print("✓ out-of-line 值（絕對 offset）")


def test_truncated_payload_does_not_explode():
    """傳輸被截斷時要好好標記，不能讓 struct.error 炸掉整個 dump。"""
    ifd = parse_ifd(build([(81, 3, 1, b"\xf4\x01\0\0")])[:-4])
    assert ifd.truncated
    print("✓ 截斷的 payload")


def test_garbage_raises_cleanly():
    try:
        parse_ifd(b"\x01\x02")
    except IFDParseError:
        print("✓ 垃圾輸入丟 IFDParseError")
    else:
        raise AssertionError("應該要丟 IFDParseError")


def test_unknown_type_code():
    """未知的 DirectoryType 只留 raw bytes，不要爆炸。"""
    ifd = parse_ifd(build([(999, 77, 3, b"\xaa\xbb\xcc\xdd")]))
    entry = find_tag(ifd, 999)
    assert entry.values is None
    assert entry.value_bytes == b"\xaa\xbb\xcc\xdd"
    print("✓ 未知 type code")


def test_format_runs():
    """報告產生器本身不能炸。"""
    out = format_ifd(parse_ifd(build([(658, 3, 2, struct.pack("<HH", 1, 2))])),
                     highlight_tags=(658, 1624))
    assert "0x0292" in out and "候選" in out
    print("✓ format_ifd 報告")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print("\ntest_ifd 全部通過")
