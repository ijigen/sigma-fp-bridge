# -*- mode: python ; coding: utf-8 -*-
"""打包成單一執行檔。

    .venv/bin/python -m PyInstaller sigma-fp-bridge.spec

libusb 不用自己處理 —— libusb-package 附了 PyInstaller hook，會把它的
dylib 一起收進去。那個套件本來就是為了「sudo 之下 DYLD_* 被剝掉」而加的，
打包後同樣受用。

static/ 要明確帶進去：網頁是從檔案讀的，不在 import 圖裡，PyInstaller
看不到。

檔名帶版本、平台與架構，而且都是算出來的不是寫死的 —— 寫死的話在 Intel Mac
上 build 會產出一個名字騙人的檔案，而那正是別人下載後才會發現的那種錯。

版本從 mac_bridge_server.py 用正規表示式讀，不 import —— import 會把 usb、
aiohttp 那一串連帶拉進 spec 的執行環境，只為了一個字串。
"""
import platform
import re
import sys
from pathlib import Path

_SRC = Path(SPECPATH) / "mac_bridge_server.py"
_m = re.search(r'^VERSION = "([^"]+)"', _SRC.read_text(), re.M)
if not _m:
    raise SystemExit("找不到 VERSION —— 檔名會少掉版本號，寧可現在就停")
VERSION = _m.group(1)

_OS = {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
NAME = f"sigma-fp-bridge-v{VERSION}-{_OS}-{platform.machine()}"

a = Analysis(
    ['mac_bridge_server.py'],
    pathex=[],
    binaries=[],
    datas=[('static', 'static')],
    # 這幾個是靠 sys.path.insert 之後才 import 的同層模組，讓 PyInstaller
    # 確實收進去，不要靠它自己推。
    hiddenimports=[
        'camera_settings', 'movie_settings', 'recording', 'capture',
        'ptp_probe', 'ifd', 'sigma_fp_focus',
        'sigma_ptpy', 'construct', 'usb.backend.libusb1',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'PIL', 'esprima', 'pyinstaller'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
