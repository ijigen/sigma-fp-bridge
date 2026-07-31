#!/usr/bin/env python3
"""
獨立測試：印出 set_focus_position(500) 真正會送出去的 bytes，
讓我們驗證 IFD 編碼是否合法。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from ifd import parse_ifd, format_ifd, find_tag
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
print(format_ifd(parse_ifd(payload), highlight_tags=(81,)))

print("\n---")
entry = find_tag(parse_ifd(payload), 81)
if entry is None:
    print("✗ 完全沒有 Tag=81 —— encode() 沒把 FocusPosition 放進去。")
elif entry.type == 3 and entry.values == [500]:
    print("✓ Tag=81 是 Type 3 (UInt16)、值 500 —— 編碼正確。")
    print("  還是不動的話，要麼相機 ignore（韌體不支援 / Linear Focus 沒開），")
    print("  要麼 SDK 文件寫 SHORT 但實際是別的型別。")
else:
    print(f"✗ Tag=81 解出來是 Type {entry.type} / values={entry.values}，預期 Type 3 / [500]。")
