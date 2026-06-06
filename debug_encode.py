#!/usr/bin/env python3
"""
獨立測試：印出 set_focus_position(500) 真正會送出去的 bytes，
讓我們驗證 IFD 編碼是否合法。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from sigma_fp_focus import CamDataGroupFocusExt
from sigma_ptpy.enum import FocusMode, AFLock, PreConstAF

# 模擬我們真正發送的 payload
focus = CamDataGroupFocusExt(
    FocusMode=FocusMode.MF,
    AFLock=AFLock.Off,
    PreConstAF=PreConstAF.Off,
    FocusPosition=500,
)

payload = focus.encode()
print(f"Encoded payload size: {len(payload)} bytes")
print(f"Hex dump:")
for i in range(0, len(payload), 16):
    chunk = payload[i:i+16]
    hex_str = " ".join(f"{b:02x}" for b in chunk)
    ascii_str = "".join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
    print(f"  {i:04x}: {hex_str:<48s} | {ascii_str}")

# 解析 IFD header
print(f"\n解析 IFD header:")
import struct
data_length, dir_count = struct.unpack('<II', payload[:8])
print(f"  DataLength: {data_length}")
print(f"  DirectoryCount: {dir_count}")

print(f"\nDirectory entries:")
offset = 8
for i in range(dir_count):
    entry = payload[offset:offset+12]
    tag, type_, count, val = struct.unpack('<HHI4s', entry)
    val_hex = " ".join(f"{b:02x}" for b in val)
    print(f"  [{i}] Tag={tag} (decimal) | Type={type_} | Count={count} | Value=[{val_hex}]")
    # 嘗試解讀 value
    if type_ == 1:  # UInt8
        print(f"        → UInt8 value: {val[0]}")
    elif type_ == 3:  # UInt16
        v = struct.unpack('<H', val[:2])[0]
        print(f"        → UInt16 value: {v}")
    elif type_ == 4:  # UInt32
        v = struct.unpack('<I', val)[0]
        print(f"        → UInt32 value: {v}")
    elif type_ == 8:  # Int16
        v = struct.unpack('<h', val[:2])[0]
        print(f"        → Int16 value: {v}")
    offset += 12

# 印關鍵 tag 81 entry
print("\n---")
print("如果 Tag=81 的 Type 是 3 (UInt16) 且 Value 解讀出來是 500，那編碼就是對的。")
print("剩下要麼相機 ignore（韌體不支援），要麼 SDK 文件騙人說 SHORT 但實際是別的。")
