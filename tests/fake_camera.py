"""假的 sigma-ptpy / 相機，讓 bridge 可以在沒有 Sigma fp 的機器上測。

用法：在 import mac_bridge_server 之前先 import 這個 module 並呼叫 install()。
它會把假的 sigma_fp_focus 塞進 sys.modules，所以 bridge 完全不會碰到真的
USB / libusb。
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeFocusState:
    """對應 CamDataGroupFocusExt 的最小介面。"""

    def __init__(self, position=0, state=0, mode=None):
        self.FocusPosition = position
        self.FocusState = state
        self.FocusMode = mode


class FakeCamera:
    """記錄自己被呼叫過幾次，方便斷言 USB 流量。"""

    def __init__(self, op_delay: float = 0.0):
        self.op_delay = op_delay
        self.position = 0
        self.set_log: list[int] = []
        self.calls: list[tuple[str, float]] = []
        # 讀第幾次 focus 之後才回報 Idle（模擬馬達still在動）
        self.idle_after_reads = 0
        # CanSetInfo5 tag 658 回報的合法範圍。設成 None 模擬相機不回報。
        self.focus_range: tuple[int, int] | None = (5974, 11116)
        self.focal_length = 28

    def _tick(self, label: str) -> None:
        if self.op_delay:
            time.sleep(self.op_delay)
        self.calls.append((label, time.monotonic()))

    def count(self, label: str) -> int:
        return sum(1 for c in self.calls if c[0] == label)

    def reset(self) -> None:
        self.position = 0
        self.set_log.clear()
        self.calls.clear()
        self.idle_after_reads = 0
        self.focus_range = (5974, 11116)
        self.focal_length = 28

    # -- sigma-ptpy 那邊會用到的 --------------------------------------

    def get_view_frame(self):
        self._tick("frame")
        n = self.count("frame")
        return types.SimpleNamespace(
            Data=b"\xff\xd8" + f"frame{n}".encode() + b"\xff\xd9"
        )

    def get_cam_data_group1(self):
        self._tick("datagroup1")
        return types.SimpleNamespace(CurrentLensFocalLength=self.focal_length)


def install(camera: FakeCamera | None = None) -> FakeCamera:
    """把假的 sigma_fp_focus 裝進 sys.modules，回傳那台假相機。"""
    cam = camera or FakeCamera()

    def get_focus_state(c):
        c._tick("focus")
        moving = c.count("focus") < c.idle_after_reads
        return FakeFocusState(c.position, 1 if moving else 0)

    def set_focus_position(c, position):
        c._tick("set")
        c.position = position
        c.set_log.append(position)

    def get_focus_range(c):
        c._tick("range")
        if c.focus_range is None:
            raise LookupError("CanSetInfo5 裡找不到焦點範圍（模擬相機不回報）")
        return c.focus_range

    mod = types.ModuleType("sigma_fp_focus")
    mod.open_camera = lambda: cam
    mod.close_camera = lambda c: None
    mod.get_focus_state = get_focus_state
    mod.get_focus_range = get_focus_range
    mod.set_focus_position = set_focus_position
    mod.distance_to_position = lambda table, d: int(d * 2000)  # 3.0m -> 6000，在範圍內
    mod.CamDataGroupFocusExt = FakeFocusState
    sys.modules["sigma_fp_focus"] = mod

    sys.path.insert(0, str(REPO_ROOT))
    return cam
