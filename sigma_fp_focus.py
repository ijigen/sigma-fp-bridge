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

注意事項：
  - 不確定 Focus Position 範圍時，先用 dry_run() 探索
  - 第一次測試「拆下鏡頭」，確認協定通了再裝回
  - 萬一鎖死，拔電池 10 秒重來
"""

from __future__ import annotations
import sys
import time
import struct
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
# 高階 API
# =============================================================================

def open_camera(serial_no: str | None = None) -> SigmaPTPy:
    """連到 fp，如果只有一台就不用指定 serial。

    sigma-ptpy 正確用法：SigmaPTPy() 建立 USB 連線後，
    用 cam.session() context manager 開 PTP session。
    我們手動 enter 然後把 session 物件掛在 cam 上，
    這樣 bridge shutdown 時能正確 close。
    """
    cam = SigmaPTPy()
    session_cm = cam.session()
    session_cm.__enter__()
    cam._bridge_session_cm = session_cm  # 掛在 cam 上方便 close
    cam.config_api()  # sigma-ptpy 不收參數
    return cam


def close_camera(cam: SigmaPTPy) -> None:
    """關閉 PTP session（建議在 bridge 結束時呼叫）。"""
    cm = getattr(cam, "_bridge_session_cm", None)
    if cm is not None:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def get_focus_range(cam: SigmaPTPy) -> tuple[int, int]:
    """從 CanSetInfo5 讀焦點位置有效範圍。

    Returns:
        (min, max): SHORT 範圍。每顆鏡頭 / 變焦位置不同。
    """
    info5 = cam.get_cam_can_set_info5()
    # CanSetInfo5 tag 0x0658 = Focus Position range
    # sigma-ptpy 也沒原生支援，要從 raw 資料拿
    # TODO: 第一次跑時用 cam.get_last_command_data() dump 出來分析
    raise NotImplementedError(
        "首次執行請先看 cam.get_last_command_data() 的內容，找 tag 0x0658 解碼方式")


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


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--help':
        print(__doc__)
        sys.exit(0)

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
        cam.__exit__(None, None, None)
        print("\n相機已斷開。")
