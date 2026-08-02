# -*- mode: python ; coding: utf-8 -*-
"""打包成單一執行檔。

    .venv/bin/python -m PyInstaller sigma-fp-bridge.spec

libusb 不用自己處理 —— libusb-package 附了 PyInstaller hook，會把它的
dylib 一起收進去。那個套件本來就是為了「sudo 之下 DYLD_* 被剝掉」而加的，
打包後同樣受用。

static/ 要明確帶進去：網頁是從檔案讀的，不在 import 圖裡，PyInstaller
看不到。
"""

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
    name='sigma-fp-bridge',
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
