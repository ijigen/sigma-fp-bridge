"""假的 sigma-ptpy / 相機，讓 bridge 可以在沒有 Sigma fp 的機器上測。

用法：在 import mac_bridge_server 之前先 import 這個 module 並呼叫 install()。
它會把假的 sigma_fp_focus 塞進 sys.modules，所以 bridge 完全不會碰到真的
USB / libusb。
"""
from __future__ import annotations

import sys
import time

from sigma_ptpy.enum import FocusMode as _FocusMode
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

    def __init__(self, position=0, state=0, mode=None, **extra):
        self.FocusPosition = position
        self.FocusState = state
        # focus_settings 用的是相機欄位名，優先於位置參數
        self.FocusMode = extra.get("FocusMode", mode)
        # 臉眼偵測 / 區域 / 對焦點
        self.FaceEyeAF = extra.get("FaceEyeAF")
        self.FaceEyeAFStatus = extra.get("FaceEyeAFStatus")
        self.FocusArea = extra.get("FocusArea")
        self.DMFPos = extra.get("DMFPos")
        self.DMFSize = extra.get("DMFSize")
        self.PreConstAF = extra.get("PreConstAF")
        self.AFLock = extra.get("AFLock")


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
        self.movie = {1: 2, 6: 2, 7: (1728, 3600), 50: 2, 51: 12, 52: 2, 60: 2, 61: (2398, 100)}
        self.movie_write_log: list = []
        # ptpy 的 Container 需要這兩個欄位
        self._session = 1
        self._transaction = 1
        self.recording = False
        self.pict_data = b"\xff\xd8" + b"IMAGE" * 3000 + b"\xff\xd9"
        self.last_capture = "movie"
        from sigma_ptpy.enum import CaptStatus
        self.status0 = CaptStatus.Cleared
        # 影像資料庫：待取項目佔 [db_head, db_tail)，entries 存各筆的狀態
        self.entries: dict = {}
        self.db_head = 0
        self.db_tail = 0
        #: 真的曝光了幾次。測試用來分辨「拍成了」與「讀到上一張的殘留」
        self.shutter_fires = 0
        #: 影像格式。DNGAndJPEG 時一次拍攝產生兩個檔案、兩筆資料庫項目。
        self.image_quality = "JPEGFine"
        #: 記憶卡剩餘空間（實機回報的單位是 MB）
        self.media_free_space = 19366
        #: 對焦模式 / 臉眼偵測 / 區域 / 對焦點
        # 實機任何時候都回報得出對焦座標（開機就是正中央），不會是 None
        # 起始狀態給 MF —— 沒有模式時 set_focus_position 會以為剛從 AF 過來
        # 而多補一次寫入，那會讓不相干的測試（例如合併）多出一筆
        self.focus_settings: dict = {"DMFPos": (340, 512),
                                     "FocusMode": _FocusMode.MF}
        #: 相機宣告接受的列舉值。照實機：色彩模式有 16 個，其中 13~16 是
        #: sigma-ptpy 的 enum 不認得的（fp 的 Off / Teal and Orange 之類）。
        #: 相機宣告的對焦選項。實機 600 = [MF, AF_C, AF_S]，沒有 AF(2)。
        self.focus_choices: dict = {
            "focus_modes": ["MF", "AF_C", "AF_S"],
            "face_eye_options": ["FaceEyeAuto", "FaceOnly", "Off"],
            "focus_areas": ["MultiAutoFocusPoints", "OnePointSelection"],
        }
        self.choice_values: dict = {
            "color_mode": [15, 14, 13, 12, 8, 7, 6, 10, 9, 5, 4, 3, 2, 1, 11, 16],
        }
        self.files: list = []
        # /api/dump/* 用的原始 IFD payload
        # 照實機：658 是焦點範圍，215/216/217 是 ISO 與曝光補償的定點數範圍，
        # 612/613 是對焦點的座標區域（原始像素，不是定點數）。
        # 612/613 一定要在 —— 它們曾經被誤塞進 read_capabilities 的回傳值裡，
        # 而那個字典的每個值都會被呼叫 .get("min")。
        self.info5_raw = _sample_ifd([
            (215, 3, [1280, 3328, 256, 85]),
            (612, 3, [682, 1024]),
            (613, 3, [85, 597, 96, 928]),
            (658, 3, [5974, 11116]),
        ])
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
        self.focus_settings = {"DMFPos": (340, 512), "FocusMode": _FocusMode.MF}
        self.focal_length = 28
        self.api_mode = True
        self.usb_claimed = True
        self.ignored_fields = set()
        self.movie = {1: 2, 6: 2, 7: (1728, 3600), 50: 2, 51: 12, 52: 2, 60: 2, 61: (2398, 100)}
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
        # 唯讀狀態欄位。實機一直有回報，只是這個專案很久沒去讀。
        g = types.SimpleNamespace(
            CurrentLensFocalLength=self.focal_length,
            MediaFreeSpace=self.media_free_space,
            MediaStatus=1, BatteryState=1, FrameBufferState=9)
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
        g = self._get_group(3)
        # 鏡頭焦段範圍與電池種類也是唯讀狀態
        g.LensWideFocalLength = self.focal_length
        g.LensTeleFocalLength = self.focal_length
        g.BatteryKind = "BodyBattery"
        return g

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

    def _pict_payload(self):
        """照實機的版面組 SigmaGetPictFileInfo2 的 payload。

            uint32 DataLength / uint32 FileCount / uint32 RecordOffset[FileCount]
            每筆記錄：address, size, pathOffset, nameOffset, format[4], w, h

        釋放之後實機只回 8 bytes 的空殼，這裡照做 —— 「拍完就沒東西可報」
        是解析器必須處理的狀況。
        """
        import struct
        if self.db_head >= self.db_tail:
            return struct.pack("<II", 4, 0)

        n = max(1, self.shutter_fires)
        files = [("DNG", len(self.pict_data) * 3, 6064, 4042, f"SDIM{n:04d}.DNG"),
                 ("JPG", len(self.pict_data), 6000, 4000, f"SDIM{n:04d}.JPG")]
        if self.image_quality != "DNGAndJPEG":
            fmt = "DNG" if self.image_quality == "DNG" else "JPG"
            files = [f for f in files if f[0] == fmt]

        count = len(files)
        head = 8 + count * 4
        records, strings = b"", b""
        string_base = head + count * 24
        offsets = []
        for fmt, size, w, h, name in files:
            offsets.append(head + len(records))
            path_off = string_base + len(strings)
            strings += b"/DCIM/100SIGMA\x00"
            name_off = string_base + len(strings)
            strings += name.encode() + b"\x00"
            records += struct.pack("<IIII", 0x1000, size, path_off, name_off)
            records += fmt.encode().ljust(4, b"\x00") + struct.pack("<HH", w, h)
        body = struct.pack("<I", count) + b"".join(
            struct.pack("<I", o) for o in offsets) + records + strings
        return struct.pack("<I", len(body)) + body

    def recv(self, ptp):
        self._tick("recv:" + ptp.OperationCode)
        if ptp.OperationCode == "SigmaGetPictFileInfo2":
            return types.SimpleNamespace(Data=self._pict_payload())
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

    # -- 錄影 / 檔案列舉 -------------------------------------------------

    def snap_command(self, data):
        self._tick("snap:" + data.CaptureMode.name)
        from sigma_ptpy.enum import CaptStatus, CaptureMode
        if data.CaptureMode in (CaptureMode.GeneralCapt, CaptureMode.NonAFCapt,
                                CaptureMode.StartCap):
            # 實測：前一筆沒被 ClearImageDBSingle 釋放的話，快門根本不動作，
            # 但 tail 照樣 +1 —— 所以「tail 前進」不代表拍成了。
            self.last_capture = "still"
            image_id = self.db_tail
            blocked = self.db_head < self.db_tail
            # DNGAndJPEG 一次拍攝產生兩個檔案，資料庫也是兩筆（實測 6→8）
            produced = 2 if self.image_quality == "DNGAndJPEG" else 1
            for k in range(produced):
                self.entries[image_id + k] = (
                    CaptStatus.ImageGenFailed if blocked
                    else CaptStatus.ImageGenCompleted)
            self.db_tail += produced
            if not blocked:
                self.shutter_fires += 1
        elif data.CaptureMode in (CaptureMode.StartRecMovie,
                                  CaptureMode.StartRecMovieAF,
                                  CaptureMode.StopRecMovie):
            self.last_capture = "movie"
        if data.CaptureMode in (CaptureMode.StartRecMovie, CaptureMode.StartRecMovieAF):
            self.recording = True
        elif data.CaptureMode == CaptureMode.StopRecMovie:
            self.status0 = CaptStatus.MovieGenCompleted
            if self.recording:
                # 錄完產生檔案，副檔名照目前的 record_format 決定
                n = len(self.files) + 1
                name = f"CLIP{n:04d}.MOV" if self.movie.get(50) == 1 else f"A{n:03d}_C001.DNG"
                self.files.append((100 + n, name, 0x300a, 1024 * n))
                self.db_tail += 1
            self.recording = False

    def get_cam_capt_status(self, image_id=0):
        """狀態是分項目的：查哪一筆就回哪一筆。

        slot 0 沒有魔法 —— 它只是編號 0 的那一筆。錄影的完成狀態沒有對應的
        項目，仍沿用 status0 從 slot 0 回報。
        """
        from sigma_ptpy.enum import CaptStatus
        status = self.entries.get(image_id)
        if status is None:
            status = self.status0 if image_id == 0 else CaptStatus.Cleared
        return types.SimpleNamespace(
            ImageId=image_id, ImageDBHead=self.db_head, ImageDBTail=self.db_tail,
            CaptStatus=status, DestToSave=None)

    # -- 連機拍攝 -------------------------------------------------------

    def clear_image_db_single(self, image_id):
        """釋放一筆，head 隨之前進到下一個還沒被取走的位置。"""
        from sigma_ptpy.enum import CaptStatus
        self._tick("clear_db")
        self.entries.pop(image_id, None)
        if image_id == 0:
            self.status0 = CaptStatus.Cleared
        while self.db_head < self.db_tail and self.db_head not in self.entries:
            self.db_head += 1

    def get_pict_file_info2(self):
        self._tick("pict_info")
        return types.SimpleNamespace(
            FileAddress=0x1000, FileSize=len(self.pict_data),
            # 實機回的是 CString → bytes，假相機也要照做，
            # 不然「檔名被寫成 b'...'」這個 bug 測不出來
            # 檔名跟著實際曝光次數走 —— 讀到上一張的殘留就會露餡
            PathName=b"/DCIM/100SIGMA\x00",
            FileName=b"SDIM%04d.JPG\x00" % max(1, self.shutter_fires),
            PictureFormat=b"JPG\x00", SizeX=6000, SizeY=4000)

    def get_big_partial_pict_file(self, store_address, start_address, max_length):
        # 緩衝區照請求的長度重複延伸 —— DNG 比 JPEG 大，兩者要能各自抓滿
        self._tick("pict_chunk")
        buf = self.pict_data
        while len(buf) < start_address + max_length:
            buf += self.pict_data
        chunk = buf[start_address:start_address + max_length]
        return types.SimpleNamespace(AcquiredSize=len(chunk), PartialData=chunk)

    def get_storage_ids(self):
        # 照 ptpy 實際行為：直接回傳陣列，不是包一層的物件。
        # （先前這裡包了 .StorageIDs，把錯誤假設複製進測試，
        #   結果測試全過但實機 502。）
        return [0x00010001]

    def get_object_handles(self, storage_id, **kw):
        return [h for h, _, _, _ in self.files]

    def get_object_info(self, handle):
        for h, name, fmt, size in self.files:
            if h == handle:
                return types.SimpleNamespace(
                    Filename=name, ObjectFormat=fmt, ObjectCompressedSize=size)
        raise RuntimeError("no such handle")

    def close_application(self):
        self._tick("close_application")
        self.api_mode = False

    def config_api(self):
        self._tick("config_api")
        self.api_mode = True

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
        return FakeFocusState(c.position, 1 if moving else 0, **c.focus_settings)

    def set_focus_position(c, position):
        # 忠實反映真機：為了不讓相機搶回焦點，這裡會強制切到 MF。
        # 「拉過滑桿就回不去 AF」的成因就在這裡，要能被測出來。
        #
        # 而且**從 AF 切到 MF 的那一筆，位置會被相機忽略**。實測起點 AF-S、
        # 位置 8999 時寫 6500，讀回仍是 8999 而模式變成 MF；再寫一次才生效。
        # 假相機照做，不然「叫它去某個位置卻靜靜地沒去」測不出來。
        from sigma_ptpy.enum import FocusMode, PreConstAF
        c._tick("set")
        c.set_log.append(position)
        was_af = getattr(c.focus_settings.get("FocusMode"), "name", None) != "MF"
        c.focus_settings["FocusMode"] = FocusMode.MF
        c.focus_settings["PreConstAF"] = PreConstAF.Off
        if not was_af:
            c.position = position

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
    mod.leave_api_mode = lambda c: c.close_application()
    mod.enter_api_mode = lambda c: c.config_api()
    mod.get_focus_state = get_focus_state

    # 對焦模式 / 臉眼偵測 / 對焦區域 / 對焦點。假相機把值記在 focus_settings
    # 裡，讀回來就看得到 —— 「拉過滑桿還回得去 AF」這件事要能被測到。
    def set_focus_mode(c, mode, continuous_af=None):
        from sigma_ptpy.enum import FocusMode, PreConstAF
        if isinstance(mode, str):
            try:
                mode = FocusMode[mode]
            except KeyError as e:
                raise ValueError(f"不認得的對焦模式：{mode}") from e
        if continuous_af is None:
            # 跟真的 sigma_fp_focus.set_focus_mode 一致：Pre-AF 只跟 AF-C 走。
            # 這裡曾經是 `mode is not FocusMode.MF`，真的那邊修掉之後假的
            # 忘了跟，於是套件全過而真機行為不同 —— 假相機一分岔，測試就
            # 開始說謊。
            continuous_af = mode is FocusMode.AF_C
        c._tick("set_focus_mode")
        c.focus_settings["FocusMode"] = mode
        c.focus_settings["PreConstAF"] = PreConstAF.On if continuous_af else PreConstAF.Off

    def set_face_eye_af(c, value):
        from sigma_ptpy.enum import FaceEyeAF
        if isinstance(value, str):
            try:
                value = FaceEyeAF[value]
            except KeyError as e:
                raise ValueError(f"不認得的值：{value}") from e
        c._tick("set_face_eye")
        c.focus_settings["FaceEyeAF"] = value

    def set_focus_area(c, area):
        from sigma_ptpy.enum import FocusArea
        if isinstance(area, str):
            try:
                area = FocusArea[area]
            except KeyError as e:
                raise ValueError(f"不認得的對焦區域：{area}") from e
        c._tick("set_focus_area")
        c.focus_settings["FocusArea"] = area

    def set_focus_point(c, y, x):
        # 實機上寫座標不會驅動鏡頭 —— 實測失焦狀態下寫入六秒沒動。假相機
        # 也不動，否則「改了對焦目標卻沒人叫它去對」這個 bug 測不出來。
        c._tick("set_focus_point")
        c.focus_settings["DMFPos"] = (int(y), int(x))

    def set_focus_point_size(c, index):
        c._tick("set_point_size")
        c.focus_settings["DMFSize"] = int(index)

    def read_focus_area_bounds(c):
        # 照實機：三種方框與移動步進（步進就是對焦點會吸附的原因）
        return {"height": 682, "width": 1024,
                "top": 85, "bottom": 597, "left": 96, "right": 928,
                "point_sizes": [[128, 128], [64, 64], [32, 32]],
                "point_step": [32, 16]}

    mod.set_focus_mode = set_focus_mode
    mod.set_face_eye_af = set_face_eye_af
    mod.set_focus_area = set_focus_area
    mod.set_focus_point = set_focus_point
    mod.set_focus_point_size = set_focus_point_size
    mod.read_focus_area_bounds = read_focus_area_bounds

    def read_focus_choices(c):
        # 照實機：600 只有 MF / AF_C / AF_S —— enum 裡的 AF(2) 不在清單裡
        return dict(c.focus_choices)

    mod.read_focus_choices = read_focus_choices

    def trigger_af(c):
        # 實機上這是唯一會讓鏡頭動的動作。假相機用位置變化來代表「有對焦」，
        # 「改了對焦目標鏡頭卻沒動」這件事才測得出來。
        c._tick("trigger_af")
        c.position += 43

    mod.trigger_af = trigger_af
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

    def read_choice_values(c):
        c._tick("choice_values")
        return dict(c.choice_values)

    mod.read_choice_values = read_choice_values
    mod.set_focus_position = set_focus_position
    mod.CamDataGroupFocusExt = FakeFocusState
    sys.modules["sigma_fp_focus"] = mod

    sys.path.insert(0, str(REPO_ROOT))
    return cam
