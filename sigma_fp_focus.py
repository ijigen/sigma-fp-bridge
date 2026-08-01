#!/usr/bin/env python3
"""
Sigma fp / fp L 鏡頭焦點位置控制 PoC

驗證可以透過 USB PTP 直接驅動 L-mount 鏡頭內部步進馬達——不用外掛跟焦器、不用駭韌體。

需求：
  - Sigma fp 韌體 5.00+ 或 fp L 韌體 3.00+
  - 一顆有電子接點的 L-mount AF 鏡頭（例如 Sigma 17mm F4 DG DN）
  - 連到電腦的 USB-C 線
  - 相機 USB Mode 設為「PTP」（不是 Mass Storage）
  - Python 3.8+，pip install sigma-ptpy pyusb

執行：
  sudo python3 sigma_fp_focus.py        # Linux 需要 sudo（USB raw access）
  python3 sigma_fp_focus.py             # macOS / Windows 通常不用
  python3 sigma_fp_focus.py --dump-info5  # dump CanSetInfo5 原始 bytes（找焦點範圍用）
  python3 sigma_fp_focus.py --dump-movie  # dump DataGroupMovie（找錄影格式用，需切到 CINE）

注意事項：
  - 不確定 Focus Position 範圍時，先用 dry_run() 探索
  - 第一次測試「拆下鏡頭」，確認協定通了再裝回
  - 萬一鎖死，拔電池 10 秒重來
"""

from __future__ import annotations
import sys
import time
import struct
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ifd import parse_ifd, format_ifd, find_tag, IFDParseError
from sigma_ptpy import SigmaPTPy
from sigma_ptpy.schema import CamDataGroupFocus, DirectoryType
from sigma_ptpy.enum import FocusMode, PreConstAF, AFLock
import sigma_ptpy.sigma_ptpy as _sigma_ptpy_module
import sigma_ptpy.schema as _sigma_schema_module


# =============================================================================
# Patch: sigma-ptpy 不支援 Focus Position (Tag 0x81) 和 Focus State (Tag 0x80)。
# 這兩個 tag 是 SIGMA Camera Control SDK v3.00 (2023-02) 才加的，sigma-ptpy
# 還沒跟上。下面用 monkey-patch 加進去，等官方升級可移除。
# =============================================================================

class CamDataGroupFocusExt(CamDataGroupFocus):
    """擴充版 CamDataGroupFocus — 加入 Focus Position / Focus State。

    新增 attributes:
        FocusPosition (int | None): SHORT。小值=遠，大值=近。範圍由 CanSetInfo5
            tag 0x0658 動態提供，每顆鏡頭 / 變焦位置不同。
        FocusState (int | None): BYTE。0=Idle, 1=Moving。Read-only。
    """

    def __init__(self, FocusPosition=None, FocusState=None, **kwargs):
        super().__init__(**kwargs)
        self.FocusPosition = FocusPosition
        self.FocusState = FocusState

    def __str__(self):
        base = super().__str__().rstrip(')')
        return f"{base}, FocusPosition={self.FocusPosition}, FocusState={self.FocusState})"

    def encode(self):
        data = []
        if self.FocusMode is not None:
            data.append((1, DirectoryType.UInt8, self.FocusMode.value))
        if self.AFLock is not None:
            data.append((2, DirectoryType.UInt8, self.AFLock.value))
        if self.FaceEyeAF is not None:
            data.append((3, DirectoryType.UInt8, self.FaceEyeAF.value))
        if self.FocusArea is not None:
            data.append((10, DirectoryType.UInt8, self.FocusArea.value))
        if self.OnePointSelection is not None:
            data.append((11, DirectoryType.UInt8, self.OnePointSelection.value))
        if self.DMFSize is not None:
            data.append((12, DirectoryType.UInt8, self.DMFSize))
        if self.DMFPos is not None:
            data.append((13, DirectoryType.UInt8, self.DMFPos))
        if self.PreConstAF is not None:
            data.append((51, DirectoryType.UInt8, self.PreConstAF.value))
        if self.FocusLimit is not None:
            data.append((52, DirectoryType.UInt8, self.FocusLimit.value))
        if self.FocusPosition is not None:
            # SDK 文件 tag "0081" 是十進位 81，不是 hex 0x81！
            # SHORT (UInt16, type code 0x03)，負值會 wrap，UI 端要避免。
            data.append((81, DirectoryType.UInt16, self.FocusPosition))
        return self._encode(data)

    def decode(self, rawdata):
        super().decode(rawdata)
        for tag, val in self._decode(rawdata):
            if tag == 80:
                self.FocusState = val[0] if hasattr(val, '__getitem__') else int(val)
            elif tag == 81:
                # SHORT (UInt16, unsigned 16-bit)
                if hasattr(val, '__getitem__') and len(val) >= 1:
                    self.FocusPosition = val[0] if isinstance(val[0], int) \
                        else struct.unpack('<H', bytes(val[:2]))[0]
                else:
                    self.FocusPosition = int(val)


# Monkey-patch：把 sigma-ptpy 模組內部用的 CamDataGroupFocus 全部換成 Ext 版本
# 這樣 cam.get_cam_data_group_focus() 直接回傳擴充版，省得自己解 raw bytes。
def _install_focus_patch():
    _sigma_ptpy_module.CamDataGroupFocus = CamDataGroupFocusExt
    _sigma_schema_module.CamDataGroupFocus = CamDataGroupFocusExt


_install_focus_patch()


# =============================================================================
# Patch 2: 攔截 CanSetInfo5 的原始 bytes。
#
# 焦點位置的合法範圍藏在 CanSetInfo5 裡，但 sigma-ptpy 只解它認得的 tag，
# 解完就把 rawdata 丟掉，我們拿不到那個 tag。這裡在 decode() 外面包一層，
# 把原始 bytes 留下來自己解（用 ifd.py）。
#
# 跟 focus patch 不同的是這裡不換掉 class，只包 method —— 這樣不管
# sigma-ptpy 內部是從哪個 module 參考到這個 class 都攔得到。
# =============================================================================

# 最近一次收到的原始 payload，key 是資料群組名稱。
_LAST_RAW: dict[str, bytes] = {}


def _find_canset_info5_class():
    """找出 sigma-ptpy 裡代表 CanSetInfo5 的 class。

    版本之間命名可能不同，所以先試已知名稱，再退回掃描整個 schema module。
    """
    for name in ("CamCanSetInfo5", "CanSetInfo5"):
        cls = getattr(_sigma_schema_module, name, None)
        if isinstance(cls, type):
            return name, cls
    for name in dir(_sigma_schema_module):
        if "CanSetInfo5" in name:
            cls = getattr(_sigma_schema_module, name)
            if isinstance(cls, type):
                return name, cls
    return None, None


def _install_raw_capture() -> str | None:
    """把 CanSetInfo5.decode() 包一層以留下原始 bytes。回傳被 patch 的 class 名稱。"""
    name, cls = _find_canset_info5_class()
    if cls is None or not hasattr(cls, "decode"):
        return None
    if getattr(cls, "_bridge_raw_capture", False):
        return name  # 已經包過了，別疊第二層

    original_decode = cls.decode

    def decode(self, rawdata, *args, **kwargs):
        try:
            _LAST_RAW["CanSetInfo5"] = bytes(rawdata)
        except Exception:
            pass  # 純屬側錄，絕不能影響正常解碼
        return original_decode(self, rawdata, *args, **kwargs)

    cls.decode = decode
    cls._bridge_raw_capture = True
    return name


_RAW_CAPTURE_CLASS = _install_raw_capture()


# SDK 文件把焦點範圍的 tag 寫成 "0658"。依照 Gotcha 1（文件裡的 tag 是十進位、
# 不是 hex），它應該是十進位 658；但舊註解當成 hex 0x658 = 1624。手上沒有實機
# dump 可以定案，所以兩個都當候選試，dump 報告也會把兩個都標出來。
FOCUS_RANGE_TAG_CANDIDATES = (658, 1624)


# CanSetInfo5 裡以「定點數 / 256」表示的範圍。解讀方式是實機 dump 反推出來的：
#
#   tag 215 = [1280, 3328, 256, 85] → /256 → (5.0, 13.0, 1.0, 0.333)
#
# 後兩個是「整級」與「最小級距」，前兩個是上下限。ISO 用 APEX 的 Sv 值
# （Sv 5 = ISO 100），所以 Sv 13 = ISO 25600 —— 正好是 fp 的原生 ISO 上限，
# 而 tag 216 的 Sv 11 = ISO 6400 正好是 Auto ISO 的預設上限。曝光補償
# 那個 tag 直接就是 EV，±5.0 也對得上機身選單。
FIXED_POINT_SCALE = 256
RANGE_TAGS = {
    215: ("iso", "sv"),
    216: ("iso_auto", "sv"),
    217: ("exposure_compensation", "ev"),
}


def _sv_to_iso(sv: float) -> int:
    """APEX 感光度值 → ISO。Sv 5 = ISO 100。"""
    return int(round(100 * (2 ** (sv - 5))))


def read_capabilities(cam: SigmaPTPy) -> dict:
    """讀相機當下實際接受的數值範圍。

    這不是靜態表 —— 隨鏡頭、機身模式、ISO 擴展開關而變，所以要跟相機問。
    讀不到的項目不會出現在回傳值裡（呼叫端要能接受缺項）。
    """
    caps: dict[str, dict] = {}
    try:
        ifd = parse_ifd(read_info5_raw(cam))
    except Exception:
        return caps

    for tag, (name, kind) in RANGE_TAGS.items():
        entry = find_tag(ifd, tag)
        if entry is None or not entry.values or len(entry.values) < 2:
            continue
        lo, hi = (v / FIXED_POINT_SCALE for v in entry.values[:2])
        step = (entry.values[3] if len(entry.values) > 3 else entry.values[2]) / FIXED_POINT_SCALE
        if kind == "sv":
            caps[name] = {"min": _sv_to_iso(lo), "max": _sv_to_iso(hi), "step_ev": round(step, 3)}
        else:
            caps[name] = {"min": round(lo, 2), "max": round(hi, 2), "step": round(step, 3)}

    try:
        lo, hi = get_focus_range(cam)
        caps["focus_position"] = {"min": lo, "max": hi}
    except Exception:
        pass

    return caps


# =============================================================================
# 高階 API
# =============================================================================

_usb_backend_patched = False


def ensure_usb_backend() -> str | None:
    """確保 pyusb 找得到 libusb，回傳用的是哪一個（找不到回 None）。

    pyusb 預設靠 ctypes.util.find_library("usb-1.0") 找 libusb，在兩種很常見的
    狀況下會找不到而丟 NoBackendError：

      1. 機器上沒有 Homebrew / 沒裝 libusb。
      2. 用 sudo 跑的時候 —— dyld 會把 DYLD_* 環境變數整組剝掉，
         run_mac.sh 辛苦設的 DYLD_FALLBACK_LIBRARY_PATH 完全失效。
         而 macOS 上要 detach kernel driver 偏偏就需要 root。

    系統本來就找得到的話就不插手；否則退回用 libusb-package 附帶的 dylib
    （以絕對路徑 load，不受 DYLD_* 影響），並把它設成 usb.core.find 的預設
    backend —— ptpy 呼叫 find() 時不會自己傳 backend，所以只能從這裡塞。
    """
    global _usb_backend_patched

    import usb.backend.libusb1
    import usb.core

    if usb.backend.libusb1.get_backend() is not None:
        return "system libusb"

    if _usb_backend_patched:
        return "libusb-package"

    try:
        import libusb_package
    except ImportError:
        return None

    backend = libusb_package.get_libusb1_backend()
    if backend is None:
        return None

    original_find = usb.core.find

    def find_with_backend(*args, **kwargs):
        if kwargs.get("backend") is None:
            kwargs["backend"] = backend
        return original_find(*args, **kwargs)

    usb.core.find = find_with_backend
    _usb_backend_patched = True
    return f"libusb-package ({libusb_package.get_library_path()})"


def open_camera(serial_no: str | None = None) -> SigmaPTPy:
    """連到 fp，如果只有一台就不用指定 serial。

    sigma-ptpy 正確用法：SigmaPTPy() 建立 USB 連線後，
    用 cam.session() context manager 開 PTP session。
    我們手動 enter 然後把 session 物件掛在 cam 上，
    這樣 bridge shutdown 時能正確 close。
    """
    ensure_usb_backend()
    cam = SigmaPTPy()
    session_cm = cam.session()
    session_cm.__enter__()
    cam._bridge_session_cm = session_cm  # 掛在 cam 上方便 close
    cam.config_api()  # sigma-ptpy 不收參數
    return cam


def close_camera(cam: SigmaPTPy) -> None:
    """退出 API 模式並關閉 PTP session。

    一定要送 CloseApplication，不能只關 session。config_api() 會讓相機進入
    API 控制模式，SDK 文件寫得很明白：

        「API does not accept any operation other than the power-off operation.」

    也就是機身除了關機以外所有實體按鍵都被鎖住，直到 USB 斷開或收到
    sgm_CloseApplication。而且 config_api() 進入時會把相機設定重置為預設值，
    要等 API 連線正常關閉才會還原成使用者原本的設定。

    少送這一步的話，使用者拿回相機的唯一辦法就是拔線或關機 —— 設定也回不來。
    """
    try:
        cam.close_application()
    except Exception as e:
        # 這個指令的 payload 在 sigma-ptpy 裡是註明「undocumented」的，
        # 不同韌體有可能不吃。失敗不該擋住 session 關閉。
        print(f"警告：CloseApplication 失敗（相機可能仍鎖在 API 模式）：{e}",
              file=sys.stderr)

    cm = getattr(cam, "_bridge_session_cm", None)
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass

    # 關 session 還不夠：USB interface 是被 ptpy 的 USBTransport 佔著的，
    # 而釋放它的 _shutdown() 只註冊在 atexit —— 也就是只有行程結束才跑。
    # 不主動呼叫的話，release 之後再 acquire 會搶不到「自己上一個物件還
    # 沒放開」的裝置，而且每次都漏一個 EvtPolling 執行緒。
    shutdown = getattr(cam, "_shutdown", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception as e:
            print(f"警告：釋放 USB interface 失敗：{e}", file=sys.stderr)


def read_info5_raw(cam: SigmaPTPy) -> bytes:
    """跟相機要 CanSetInfo5，回傳它的原始 payload bytes。

    真正的擷取發生在 _install_raw_capture() 包的那層 decode()。

    Raises:
        RuntimeError: patch 沒掛上，或這台相機根本沒回 CanSetInfo5。
    """
    if _RAW_CAPTURE_CLASS is None:
        raise RuntimeError(
            "找不到 sigma-ptpy 的 CanSetInfo5 class，raw capture patch 沒掛上。"
            "可能是 sigma-ptpy 改了命名，檢查 _find_canset_info5_class()。"
        )
    _LAST_RAW.pop("CanSetInfo5", None)  # 清掉舊的，免得相機沒回卻拿到上一次的
    cam.get_cam_can_set_info5()
    raw = _LAST_RAW.get("CanSetInfo5")
    if raw is None:
        raise RuntimeError(
            f"呼叫 get_cam_can_set_info5() 之後沒攔到原始 bytes"
            f"（已 patch {_RAW_CAPTURE_CLASS}.decode）。"
            "可能是 sigma-ptpy 走了別條解碼路徑。"
        )
    return raw


def get_focus_range(cam: SigmaPTPy) -> tuple[int, int]:
    """從 CanSetInfo5 讀焦點位置有效範圍。

    Returns:
        (min, max): SHORT 範圍。每顆鏡頭 / 變焦位置都不一樣。

    Raises:
        LookupError: CanSetInfo5 裡沒有任何一個候選 tag。錯誤訊息會列出實際
            有哪些 tag，方便直接判斷該用哪一個。
    """
    raw = read_info5_raw(cam)
    ifd = parse_ifd(raw)
    for tag in FOCUS_RANGE_TAG_CANDIDATES:
        entry = find_tag(ifd, tag)
        if entry is not None and entry.values and len(entry.values) >= 2:
            lo, hi = int(entry.values[0]), int(entry.values[1])
            return (lo, hi) if lo <= hi else (hi, lo)

    found = ", ".join(str(e.tag) for e in ifd.entries) or "（一個都沒有）"
    raise LookupError(
        f"CanSetInfo5 裡找不到焦點範圍。試過的候選 tag: "
        f"{FOCUS_RANGE_TAG_CANDIDATES}；實際有的 tag: {found}。\n"
        f"請跑 `python3 sigma_fp_focus.py --dump-info5` 把完整輸出貼出來。"
    )


def dump_info5(cam: SigmaPTPy) -> None:
    """把 CanSetInfo5 的原始 bytes 跟解析結果印出來。

    這是給「還不知道焦點範圍藏在哪個 tag」用的探勘工具。裝不同鏡頭、
    變焦推到不同位置各跑一次，比對哪些數字會跟著變，就能認出範圍那個 tag。
    """
    print("=" * 70)
    print("CanSetInfo5 raw dump")
    print("=" * 70)
    print(f"raw capture patch: {_RAW_CAPTURE_CLASS or '未掛上 ⚠'}")

    # 焦點範圍是「當下這顆鏡頭、當下這個變焦位置」的值，所以 dump 一定要
    # 附上是哪顆鏡頭 —— 不然貼到 issue 上沒人知道這組數字屬於誰。
    print()
    print("-" * 70)
    print("當下狀態（焦點範圍隨鏡頭 / 變焦位置而變，回報時請一起附上）")
    try:
        g1 = cam.get_cam_data_group1()
        print(f"  鏡頭焦距: {g1.CurrentLensFocalLength} mm")
        # 這三個是 Sigma 的原始編碼值，不是 f/、ISO、秒數，別直接當人類單位讀
        print(f"  光圈/ISO/快門（原始編碼值）: "
              f"{g1.Aperture} / {g1.ISOSpeed} / {g1.ShutterSpeed}")
    except Exception as e:
        print(f"  DataGroup1 讀取失敗: {e}")
    try:
        f = get_focus_state(cam)
        print(f"  FocusMode: {f.FocusMode}")
        print(f"  FocusPosition: {f.FocusPosition}")
        print(f"  FocusState: {f.FocusState} (0=Idle, 1=Moving)")
    except Exception as e:
        print(f"  DataGroupFocus 讀取失敗: {e}")
    print("-" * 70)
    print()

    raw = read_info5_raw(cam)
    try:
        ifd = parse_ifd(raw)
    except IFDParseError as e:
        print(f"⚠ IFD 解析失敗：{e}")
        print("原始 bytes：")
        from ifd import hexdump
        print(hexdump(raw))
        return

    print(format_ifd(ifd, highlight_tags=FOCUS_RANGE_TAG_CANDIDATES))
    print()

    print("-" * 70)
    for tag in FOCUS_RANGE_TAG_CANDIDATES:
        entry = find_tag(ifd, tag)
        if entry is None:
            print(f"候選 tag {tag} (0x{tag:04x}): 不存在")
        else:
            print(f"候選 tag {tag} (0x{tag:04x}): 有！values={entry.values}")

    try:
        lo, hi = get_focus_range(cam)
        print(f"\n✓ 解出焦點範圍：{lo} ~ {hi}")
        try:
            current = get_focus_state(cam).FocusPosition
        except Exception:
            current = None
        if current is not None:
            inside = lo <= current <= hi
            print(f"  目前位置 {current} {'落在範圍內 ✓' if inside else '不在範圍內 ⚠'}")
            if not inside:
                print("  ⚠ 目前位置不在解出來的範圍內，代表這個 tag 可能不是焦點範圍。")
                print("    請把整份輸出回報。")
    except LookupError:
        print(
            "\n✗ 兩個候選 tag 都沒中。請把上面整份輸出貼出來 ——\n"
            "  裝不同鏡頭各跑一次，會跟著變的那個 tag 就是我們要找的。"
        )


def read_movie_group_raw(cam: SigmaPTPy) -> bytes:
    """發 SigmaGetCamDataGroupMovie (0x9033)，取回原始 payload。

    sigma-ptpy 定義了這個 opcode，但既沒有對應的 schema class 也沒有高階
    方法 —— 跟當初 FocusPosition 的處境一樣。這裡自己組 Container 送，
    拿回 IFD 原始 bytes 交給 ifd.py 解析。

    錄影相關的設定（RecordFormat / CinemaDNG 畫質 / 解析度 / FrameRate）
    很可能就在這個 DataGroup 裡，但它內部的 tag 編號是未知的 —— 不會跟
    CanSetInfo5 一樣（對照組：DataGroupFocus 用 tag 1/2/81，CanSetInfo5
    的同一批項目卻編成 600/601/612）。所以得先 dump 出來看。

    只讀不寫。
    """
    from construct import Container

    ptp = Container(
        OperationCode="SigmaGetCamDataGroupMovie",
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=[],
    )
    response = cam.recv(ptp)
    return bytes(response.Data)


def dump_movie_group(cam: SigmaPTPy) -> None:
    """把 DataGroupMovie 的原始 bytes 與解析結果印出來。

    這是探勘工具，用途跟 --dump-info5 一樣：先看清楚相機回什麼，
    才有辦法寫 setter。
    """
    print("=" * 70)
    print("CamDataGroupMovie raw dump (opcode 0x9033)")
    print("=" * 70)

    try:
        raw = read_movie_group_raw(cam)
    except Exception as e:
        print(f"讀取失敗：{type(e).__name__}: {e}")
        print()
        print("如果是 OperationNotSupported，代表這台機身 / 韌體不支援這個指令。")
        print("如果 payload 是空的，試試把機身撥桿切到 CINE 再跑一次 ——")
        print("CanSetInfo5 的錄影相關 tag 在 STILL 模式下也是空的。")
        return

    if not raw:
        print("相機回了空的 payload。")
        print("把機身撥桿切到 CINE 再跑一次 —— 錄影設定在 STILL 模式下不存在。")
        return

    try:
        ifd = parse_ifd(raw)
    except IFDParseError as e:
        print(f"⚠ 不是 IFD 結構（{e}），印原始 bytes：")
        from ifd import hexdump
        print(hexdump(raw))
        return

    print(format_ifd(ifd))
    print()
    print("-" * 70)
    print("這些 tag 的意義還未知。要判讀的話：改一項機身設定（例如幀率或")
    print("錄影格式）再跑一次，比對哪個 tag 的值跟著變 —— 那就是它。")


def get_focus_state(cam: SigmaPTPy) -> CamDataGroupFocusExt:
    """讀目前焦點狀態。

    Monkey-patch 已經把 sigma-ptpy 內部的 CamDataGroupFocus 換成
    CamDataGroupFocusExt（看本檔末尾 _install_focus_patch），所以
    cam.get_cam_data_group_focus() 直接回傳 Ext 版本。
    """
    return cam.get_cam_data_group_focus()


def set_focus_position(cam: SigmaPTPy, position: int) -> None:
    """直接驅動鏡頭對焦馬達到指定位置。

    Args:
        position: SHORT 範圍依鏡頭。小=遠，大=近。
                  先呼叫 get_focus_range() 確認有效範圍。

    必須同時關掉所有「自動搶回焦點」的子系統：
      - FocusMode=MF：不要 AF
      - AFLock=Off：不要鎖住舊位置
      - PreConstAF=Off：不要持續 AF（這個預設 ON，會 override 你的 position）
    """
    focus = CamDataGroupFocusExt(
        FocusMode=FocusMode.MF,
        AFLock=AFLock.Off,
        PreConstAF=PreConstAF.Off,
        FocusPosition=position,
    )
    cam.set_cam_data_group_focus(focus)


def wait_focus_idle(cam: SigmaPTPy, timeout_s: float = 3.0,
                    poll_interval_s: float = 0.05) -> bool:
    """等待焦點馬達停止移動。

    Returns:
        True if motor reached idle within timeout, False otherwise.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = get_focus_state(cam)
        if state.FocusState == 0:  # Idle
            return True
        time.sleep(poll_interval_s)
    return False


# =============================================================================
# 校準工具 — 建立 distance ↔ position 對應表
# =============================================================================

def interactive_calibration(cam: SigmaPTPy) -> list[tuple[float, int]]:
    """互動式建立 distance(m) ↔ FocusPosition 對應表。

    流程：
      1. 引導使用者把目標放在 0.5m / 1m / 2m / 5m / 無限遠
      2. 每點手動轉 FocusPosition 直到清晰
      3. 記錄
    """
    print("\n=== 焦點距離校準 ===")
    print("放鏡頭設成 MF mode，相機放穩，目標清晰時記錄該 position。")
    print("最少需要 5 點，越多越準。輸入 q 結束。\n")

    table = []
    while True:
        d = input("目標距離（公尺，q 退出）: ").strip()
        if d.lower() == 'q':
            break
        try:
            distance = float(d)
        except ValueError:
            print("錯誤的格式，請再試。")
            continue

        print(f"  現在請手動轉動 FocusPosition 直到 {distance}m 處清晰。")
        print("  輸入 'next' 移焦 +50，'prev' -50，'jump N' 直接跳 N，'ok' 確認。")
        current_pos = 0
        while True:
            cmd = input(f"  [position={current_pos}] > ").strip().lower()
            if cmd == 'ok':
                table.append((distance, current_pos))
                print(f"  ✓ 已記錄: {distance}m → position {current_pos}\n")
                break
            elif cmd == 'next':
                current_pos += 50
            elif cmd == 'prev':
                current_pos -= 50
            elif cmd.startswith('jump '):
                try:
                    current_pos = int(cmd.split()[1])
                except (ValueError, IndexError):
                    print("  jump 後面要接數字")
                    continue
            else:
                print("  指令：next / prev / jump N / ok")
                continue
            set_focus_position(cam, current_pos)
            if not wait_focus_idle(cam):
                print("  ⚠ 馬達超時，可能超出範圍")

    return sorted(table)


def distance_to_position(table: list[tuple[float, int]], distance_m: float) -> int:
    """以線性內插查表算出 position。實際用建議用 scipy 的 cubic spline。"""
    table = sorted(table)
    if distance_m <= table[0][0]:
        return table[0][1]
    if distance_m >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        d1, p1 = table[i]
        d2, p2 = table[i+1]
        if d1 <= distance_m <= d2:
            t = (distance_m - d1) / (d2 - d1)
            return int(p1 + t * (p2 - p1))
    return table[-1][1]


# =============================================================================
# Demo
# =============================================================================

def demo_basic_io(cam: SigmaPTPy):
    """最基本 sanity check — read only。"""
    print("\n=== Basic I/O Sanity Check ===")
    cs = cam.get_cam_status()
    print(f"  CamStatus: {cs}")

    g1 = cam.get_cam_data_group1()
    print(f"  DataGroup1: shutter={g1.ShutterSpeed} aperture={g1.Aperture} "
          f"ISO={g1.ISOSpeed} focal_len={g1.CurrentLensFocalLength}mm")

    focus = get_focus_state(cam)
    print(f"  FocusState: {focus}")


def demo_set_focus(cam: SigmaPTPy):
    """寫入測試 — 移焦到中間值。"""
    print("\n=== Focus Position Drive Test ===")
    print("⚠ 確認相機已切到 MF 模式，鏡頭已裝好")
    input("按 Enter 開始")

    # 用個保守的中間值（具體範圍要看鏡頭，這只是 placeholder）
    test_positions = [0, 256, 512, 256, 0]
    for pos in test_positions:
        print(f"  → 移焦到 position={pos}")
        set_focus_position(cam, pos)
        ok = wait_focus_idle(cam, timeout_s=2.0)
        state = get_focus_state(cam)
        print(f"    馬達 idle={ok}, 實際 state={state}")
        time.sleep(0.5)


def _main_dump(which: str) -> int:
    """--dump-info5 / --dump-movie 的共用進入點。只讀不寫，不會動到馬達。"""
    import logging
    import os

    # ptpy 的背景 EvtPolling thread 會固定噴 timeout，跟我們無關，只是噪音
    logging.getLogger("ptpy.transports.usb").setLevel(logging.CRITICAL)

    backend = ensure_usb_backend()
    print(f"USB backend: {backend or '找不到 libusb ⚠'}")
    print(f"以 {'root' if os.geteuid() == 0 else '一般使用者'} 身分執行")
    print()
    try:
        cam = open_camera()
    except Exception as e:
        print(f"連不上相機：{e}")
        print()
        print("檢查順序：")
        print("  1. USB 線、相機 USB Mode 要設成 PTP")
        print("  2. 上面若出現 'Could not detach kernel driver'，是 macOS 的")
        print("     ptpcamerad 佔住了裝置。先試 sudo 跑這支程式；還是不行的話")
        print("     才需要 SIGSTOP 那組 daemon（見 README Gotcha 4）。")
        return 1
    try:
        if which == "movie":
            dump_movie_group(cam)
        else:
            dump_info5(cam)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\ndump 失敗：{type(e).__name__}: {e}")
        return 1
    finally:
        close_camera(cam)
    return 0


if __name__ == '__main__':
    args = sys.argv[1:]

    if '--help' in args or '-h' in args:
        print(__doc__)
        sys.exit(0)

    if '--dump-info5' in args:
        sys.exit(_main_dump("info5"))

    if '--dump-movie' in args:
        sys.exit(_main_dump("movie"))

    print("Sigma fp Focus Position PoC")
    print("---------------------------")
    print("步驟：")
    print("1. 相機開機，USB Mode 設為 PTP")
    print("2. 用 USB-C 線接電腦")
    print("3. 確認 lsusb 看到 Sigma 裝置（VID 1003）")
    print()

    try:
        cam = open_camera()
    except Exception as e:
        print(f"連不上相機：{e}")
        print("檢查：USB 線、相機 USB Mode、權限（Linux 可能需要 sudo 或 udev rule）")
        sys.exit(1)

    try:
        demo_basic_io(cam)

        ans = input("\n要試寫入 (set FocusPosition) 嗎？(y/N): ").strip().lower()
        if ans == 'y':
            demo_set_focus(cam)

        ans = input("\n要進入校準模式嗎？(y/N): ").strip().lower()
        if ans == 'y':
            table = interactive_calibration(cam)
            print("\n校準完成，對應表：")
            for d, p in table:
                print(f"  {d:6.2f}m → position {p}")
            print("\n試算：")
            for d in [0.75, 1.5, 3.0, 7.0]:
                p = distance_to_position(table, d)
                print(f"  {d}m → ~{p}")
    finally:
        # 必須走 close_camera()：它會送 CloseApplication 讓相機退出 API 模式。
        # 直接 cam.__exit__() 只關 session，機身按鍵會一直鎖著。
        close_camera(cam)
        print("\n相機已斷開，機身操作已交還。")
