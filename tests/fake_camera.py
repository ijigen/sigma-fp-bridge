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


def _sample_ifd(entries) -> bytes:
    """用 sigma-ptpy 自己的 encoder 產生一段像樣的 IFD payload。"""
    from sigma_ptpy.schema import DirectoryType as DT
    from sigma_ptpy.schema import _DirectoryEntrySchema

    encoder = type("E", (_DirectoryEntrySchema,), {})()
    type_map = {1: DT.UInt8, 3: DT.UInt16}
    return encoder._encode([(tag, type_map[t], vals) for tag, t, vals in entries])


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
        self.api_mode = True
        self.usb_claimed = True
        # 這些欄位寫進去會被相機默默忽略（模擬自動曝光覆蓋手動值）
        self.ignored_fields: set = set()
        # DataGroupMovie 的內容（tag -> 值）。照實機讀到的樣子。
        self.movie = {7: (1728, 3600), 50: 2, 51: 12, 52: 2, 60: 2, 61: (2398, 100)}
        self.movie_write_log: list = []
        # ptpy 的 Container 需要這兩個欄位
        self._session = 1
        self._transaction = 1
        # /api/dump/* 用的原始 IFD payload
        self.info5_raw = _sample_ifd([(658, 3, [5974, 11116])])
        self.movie_raw = _sample_ifd([(1, 1, [2]), (10, 3, [25])])
        # 照實機回報：ISO 100–25600、曝光補償 ±5EV
        self.capabilities = {
            "iso": {"min": 100, "max": 25600, "step_ev": 0.333},
            "iso_auto": {"min": 100, "max": 6400, "step_ev": 0.333},
            "exposure_compensation": {"min": -5.0, "max": 5.0, "step": 0.333},
        }
        self.set_group_log: list = []
        self.groups: dict[int, dict] = self._default_groups()

    @staticmethod
    def _default_groups() -> dict[int, dict]:
        """一組合理的初始設定：f/2.0、ISO 6400、1/25s（照實機 dump 的值）。"""
        from sigma_ptpy import enum as E
        return {
            1: {"Aperture": 24, "ShutterSpeed": 93, "ISOSpeed": 80,
                "ExpComp": 0, "ISOAuto": E.ISOAuto.Manual},
            2: {"ExposureMode": E.ExposureMode.Manual,
                "WhiteBalance": E.WhiteBalance.Auto,
                "ImageQuality": E.ImageQuality.DNG,
                "Resolution": E.Resolution.High,
                "DriveMode": E.DriveMode.SingleCapture,
                "AEMeteringMode": E.AEMeteringMode.Evaluative},
            3: {"ColorMode": E.ColorMode.Standard, "ColorSpace": E.ColorSpace.sRGB},
            4: {"DNGQuality": E.DNGQuality.Q14bit},
            5: {"ColorTemp": 5600, "AspectRatio": E.AspectRatio.W16H9,
                "ToneEffect": E.ToneEffect.Null},
        }

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
        self.api_mode = True
        self.usb_claimed = True
        self.ignored_fields = set()
        self.movie = {7: (1728, 3600), 50: 2, 51: 12, 52: 2, 60: 2, 61: (2398, 100)}
        self.movie_write_log.clear()
        self.capabilities = {
            "iso": {"min": 100, "max": 25600, "step_ev": 0.333},
            "iso_auto": {"min": 100, "max": 6400, "step_ev": 0.333},
            "exposure_compensation": {"min": -5.0, "max": 5.0, "step": 0.333},
        }
        self.set_group_log.clear()
        self.groups = self._default_groups()

    # -- sigma-ptpy 那邊會用到的 --------------------------------------

    def get_view_frame(self):
        self._tick("frame")
        n = self.count("frame")
        return types.SimpleNamespace(
            Data=b"\xff\xd8" + f"frame{n}".encode() + b"\xff\xd9"
        )

    def get_cam_data_group1(self):
        self._tick("datagroup1")
        g = types.SimpleNamespace(CurrentLensFocalLength=self.focal_length)
        for k, v in self.groups[1].items():
            setattr(g, k, v)
        return g

    # -- DataGroup 2..5：設定的讀寫 -------------------------------------

    def _get_group(self, n):
        self._tick(f"getgroup{n}")
        return types.SimpleNamespace(**self.groups[n])

    def get_cam_data_group2(self):
        return self._get_group(2)

    def get_cam_data_group3(self):
        return self._get_group(3)

    def get_cam_data_group4(self):
        return self._get_group(4)

    def get_cam_data_group5(self):
        return self._get_group(5)

    def _set_group(self, n, payload):
        self._tick(f"setgroup{n}")
        for k, v in vars(payload).items():
            if v is not None and k not in self.ignored_fields:
                self.groups[n][k] = v
        self.set_group_log.append((n, {k: v for k, v in vars(payload).items() if v is not None}))

    def set_cam_data_group1(self, p):
        self._set_group(1, p)

    def set_cam_data_group2(self, p):
        self._set_group(2, p)

    def set_cam_data_group3(self, p):
        self._set_group(3, p)

    def set_cam_data_group4(self, p):
        self._set_group(4, p)

    def set_cam_data_group5(self, p):
        self._set_group(5, p)

    # -- DataGroupMovie：走 recv/send 的原始 opcode 路徑 ------------------

    def recv(self, ptp):
        self._tick("recv:" + ptp.OperationCode)
        if ptp.OperationCode == "SigmaGetCamDataGroupMovie":
            return types.SimpleNamespace(Data=self._encode_movie())
        raise RuntimeError(f"假相機不支援 {ptp.OperationCode}")

    def send(self, ptp, payload):
        self._tick("send:" + ptp.OperationCode)
        if ptp.OperationCode != "SigmaSetCamDataGroupMovie":
            raise RuntimeError(f"假相機不支援 {ptp.OperationCode}")
        import sys as _sys
        from pathlib import Path as _Path
        _sys.path.insert(0, str(REPO_ROOT))
        from ifd import parse_ifd
        for e in parse_ifd(bytes(payload)).entries:
            if e.values:
                self.movie[e.tag] = e.values[0]
        self.movie_write_log.append(dict(self.movie))
        return None

    def _encode_movie(self) -> bytes:
        from sigma_ptpy.schema import DirectoryType as DT
        from sigma_ptpy.schema import _DirectoryEntrySchema
        enc = type("E", (_DirectoryEntrySchema,), {})()
        rows = []
        for tag, v in sorted(self.movie.items()):
            rows.append((tag, DT.URational if isinstance(v, tuple) else DT.UInt8, v))
        return enc._encode(rows)

    def close_application(self):
        self._tick("close_application")
        self.api_mode = False

    def _shutdown(self):
        """對應 ptpy USBTransport._shutdown()：釋放 USB interface。"""
        self._tick("shutdown")
        self.usb_claimed = False


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

    def read_capabilities(c):
        c._tick("capabilities")
        caps = dict(c.capabilities)
        if c.focus_range:
            caps["focus_position"] = {"min": c.focus_range[0], "max": c.focus_range[1]}
        return caps

    def get_focus_range(c):
        c._tick("range")
        if c.focus_range is None:
            raise LookupError("CanSetInfo5 裡找不到焦點範圍（模擬相機不回報）")
        return c.focus_range

    mod = types.ModuleType("sigma_fp_focus")
    def open_camera():
        if cam.usb_claimed:
            raise RuntimeError("No USB PTP device found.（上一個連線還佔著 interface）")
        cam.usb_claimed = True
        return cam

    mod.open_camera = open_camera

    def close_camera(c):
        # 忠實反映真實的 close_camera()：先送 CloseApplication 讓相機退出
        # API 模式，再釋放 USB interface。少了後者，release 之後的 acquire
        # 會搶不到自己還沒放開的裝置。
        c.close_application()
        c._shutdown()

    mod.close_camera = close_camera
    mod.get_focus_state = get_focus_state
    def read_info5_raw(c):
        c._tick("info5_raw")
        return c.info5_raw

    def read_movie_group_raw(c):
        c._tick("movie_raw")
        return c.movie_raw

    mod.get_focus_range = get_focus_range
    mod.read_info5_raw = read_info5_raw
    mod.read_movie_group_raw = read_movie_group_raw
    mod.read_capabilities = read_capabilities
    mod.set_focus_position = set_focus_position
    mod.distance_to_position = lambda table, d: int(d * 2000)  # 3.0m -> 6000，在範圍內
    mod.CamDataGroupFocusExt = FakeFocusState
    sys.modules["sigma_fp_focus"] = mod

    sys.path.insert(0, str(REPO_ROOT))
    return cam
