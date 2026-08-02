#!/usr/bin/env python3
"""相機設定的讀寫層：把 Sigma 的原始編碼換成人看得懂的值。

協定裡 ISO / 光圈 / 快門 / 曝光補償都是 8-bit APEX 碼（例如光圈 24 = f/2.0），
其他多數設定是 enum。這個 module 用一張表把兩邊對起來，讓上層可以直接講
{"aperture": 2.8, "exposure_mode": "Manual"}，不必碰編碼。

分組是有意義的：一次 set_cam_data_groupN 只能寫同一個 DataGroup 的欄位，
所以套用多筆變更時要先依 group 分堆，每組送一次。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sigma_ptpy import enum as E
from sigma_ptpy.apex import (
    Aperture3Converter,
    ExpComp3Converter,
    ISOSpeedConverter,
    ShutterSpeed3Converter,
)
from sigma_ptpy.schema import (
    CamDataGroup1,
    CamDataGroup2,
    CamDataGroup3,
    CamDataGroup4,
    CamDataGroup5,
)

# fp 實測是 1/3 級。判斷依據：dump 到的光圈碼 24 與快門碼 93 都只存在於
# 1/3 級的表裡（1/2 級的表沒有這兩個碼）。如果哪天遇到 1/2 級的機身 /
# 設定，這裡要改用 Aperture2Converter / ShutterSpeed2Converter。
APERTURE = Aperture3Converter
SHUTTER = ShutterSpeed3Converter
EXP_COMP = ExpComp3Converter
ISO = ISOSpeedConverter

# sigma-ptpy 的 Aperture3Converter 有個上游打錯：表裡是 (191, 57)，但 191
# 超出光圈碼的合理範圍，前後鄰居是 99 (f/51) 與 104 (f/64)，顯然應該是 101。
# 照原表 encode(57) 會送出 191 這個相機看不懂的值。這裡把它擋掉。
_BROKEN_APERTURE_CODES = {191}

GROUP_CLASSES = {
    1: CamDataGroup1,
    2: CamDataGroup2,
    3: CamDataGroup3,
    4: CamDataGroup4,
    5: CamDataGroup5,
}


@dataclass(frozen=True)
class Setting:
    """一個可讀 / 可寫的相機設定。"""

    name: str            # 對外的名稱（snake_case）
    group: int           # 屬於哪個 DataGroup
    field: str           # DataGroup 上的屬性名
    kind: str            # "apex" | "enum" | "int"
    unit: str = ""       # 顯示用單位
    enum_cls: Any = None
    converter: Any = None
    writable: bool = True
    note: str = ""
    #: "both" | "stills" | "movie" —— 這個設定在哪個機身模式下有意義
    applies_to: str = "both"


SETTINGS: tuple[Setting, ...] = (
    # ── 曝光三要素 ───────────────────────────────────────────────────
    Setting("exposure_mode", 2, "ExposureMode", "enum", enum_cls=E.ExposureMode,
            note="P / A / S / M。A 模式下快門由相機決定，S 模式下光圈由相機決定。"),
    Setting("aperture", 1, "Aperture", "apex", unit="f", converter=APERTURE),
    Setting("shutter_speed", 1, "ShutterSpeed", "apex", unit="s", converter=SHUTTER,
            applies_to="stills",
            note="CINE 模式下無效 —— 錄影的快門由 shutter_angle 控制，"
                 "寫這個欄位相機會收下然後丟掉。"),
    Setting("iso", 1, "ISOSpeed", "apex", unit="ISO", converter=ISO),
    Setting("iso_auto", 1, "ISOAuto", "enum", enum_cls=E.ISOAuto),
    Setting("exposure_compensation", 1, "ExpComp", "apex", unit="EV", converter=EXP_COMP),
    Setting("metering_mode", 2, "AEMeteringMode", "enum", enum_cls=E.AEMeteringMode),

    # ── 白平衡 ───────────────────────────────────────────────────────
    Setting("white_balance", 2, "WhiteBalance", "enum", enum_cls=E.WhiteBalance,
            note="要用 color_temp 指定色溫的話，這裡要先設成 ColorTemp。"),
    Setting("color_temp", 5, "ColorTemp", "int", unit="K",
            note="開氏溫度。只有 white_balance=ColorTemp 時才生效。"),

    # ── 影像格式 ─────────────────────────────────────────────────────
    Setting("image_quality", 2, "ImageQuality", "enum", enum_cls=E.ImageQuality,
            applies_to="stills",
            note="靜態照片的格式（DNG / JPEG）。錄影格式看 record_format。"),
    Setting("dng_quality", 4, "DNGQuality", "enum", enum_cls=E.DNGQuality,
            applies_to="stills",
            note="靜態 DNG 的位元深度。CinemaDNG 的深度看 cinema_dng_quality。"),
    Setting("resolution", 2, "Resolution", "enum", enum_cls=E.Resolution,
            applies_to="stills"),
    Setting("aspect_ratio", 5, "AspectRatio", "enum", enum_cls=E.AspectRatio,
            applies_to="stills",
            note="實測錄影模式下寫入無效（相機收下後不改變）。"),

    # ── 影像風格 ─────────────────────────────────────────────────────
    Setting("color_mode", 3, "ColorMode", "enum", enum_cls=E.ColorMode),
    Setting("color_space", 3, "ColorSpace", "enum", enum_cls=E.ColorSpace,
            applies_to="stills",
            note="實測錄影模式下寫入無效。"),
    Setting("tone_effect", 5, "ToneEffect", "enum", enum_cls=E.ToneEffect,
            applies_to="stills",
            note="實測錄影模式下寫入無效。"),

    # ── 連機拍攝 ─────────────────────────────────────────────────────
    Setting("dest_to_save", 3, "DestToSave", "enum", enum_cls=E.DestToSave,
            note="控制拍下的影像要不要寫進記憶卡：InCamera／Both 會寫，"
                 "Null／InComputer 不寫（實機對卡驗證過）。但四個值都不影響 "
                 "/api/capture 能不能把影像抓回電腦 —— 下載讀的是相機的 buffer。"
                 "要連機拍攝又不佔卡，設 InComputer 或 Null。"),

    # ── 影像處理與穩定 ───────────────────────────────────────────────
    #
    # 這一批一直在 DataGroup4/5 裡，只是沒被接出來。
    #
    # 刻意不做能力驗證：CanSetInfo5 對這些欄位的編碼形狀不一致 —— EImageStab
    # 宣告 [0, 1] 但實際值是 Off(2)，DCCropMode 宣告 [1, 0, -1] 而 -1 不是合法
    # 的 enum 值，ShutterSound 的 [0, 5, 1] 又像是 [min, max, step]。把它們當成
    # 可選值清單會擋掉合法的設定。所以這裡靠寫入後讀回比對 —— 寫不進去會如實
    # 回報 requested/actual，而不是先用一個我讀不懂的清單去猜。
    Setting("electronic_stabilization", 4, "EImageStab", "enum", enum_cls=E.EImageStab,
            note="電子防手震。錄影時有效，會裁切畫面。"),
    Setting("dc_crop_mode", 4, "DCCropMode", "enum", enum_cls=E.DCCropMode,
            note="APS-C 裁切模式。Auto 會依鏡頭自動判斷。"),
    Setting("high_iso_ext", 4, "HighISOExt", "enum", enum_cls=E.HighISOExt,
            applies_to="stills", note="高感光度擴展。"),
    Setting("cont_shoot_speed", 4, "ContShootSpeed", "enum", enum_cls=E.ContShootSpeed,
            applies_to="stills", note="連拍速度。"),
    Setting("hdr", 4, "HDR", "enum", enum_cls=E.HDR, applies_to="stills"),
    Setting("fill_light", 4, "FillLight", "int", applies_to="stills",
            note="補光（Fill Light）強度。"),

    # ── 鏡頭光學校正 ─────────────────────────────────────────────────
    Setting("loc_distortion", 4, "LOCDistortion", "enum", enum_cls=E.LOCDistortion,
            note="畸變校正。"),
    Setting("loc_chromatic_aberration", 4, "LOCChromaticAberration", "enum",
            enum_cls=E.LOCChromaticAberration, note="色差校正。"),
    Setting("loc_diffraction", 4, "LOCDiffraction", "enum", enum_cls=E.LOCDiffraction,
            note="繞射校正。"),
    Setting("loc_vignetting", 4, "LOCVignetting", "enum", enum_cls=E.LOCVignetting,
            note="周邊光量校正。"),

    # ── 間隔計時器（縮時攝影）─────────────────────────────────────────
    #
    # CanSetInfo5 只在 STILL 模式下宣告這兩個可設定（CINE 下是空的），
    # 所以標成 stills。
    Setting("interval_timer_seconds", 5, "IntervalTimerSecond", "int", unit="s",
            applies_to="stills", note="間隔計時器的間隔秒數。"),
    Setting("interval_timer_frames", 5, "IntervalTimerFrame", "int", unit="張",
            applies_to="stills", note="間隔計時器的張數。0 = 無限。"),

    # ── 驅動 ─────────────────────────────────────────────────────────
    Setting("drive_mode", 2, "DriveMode", "enum", enum_cls=E.DriveMode,
            applies_to="stills"),
)

BY_NAME = {s.name: s for s in SETTINGS}


class SettingError(ValueError):
    """設定名稱不存在，或值無法轉成相機接受的編碼。"""


# ─────────────────────────────────────────────────────────────────────
# 快門角度
#
# 電影圈習慣用角度而不是秒數，因為角度跟幀率綁在一起才有意義：
# 180° 表示「曝光時間佔一個影格的一半」，換到任何幀率都維持同樣的動態模糊。
#
#     角度 = 360 × 曝光秒數 × 幀率
#     秒數 = 角度 / (360 × 幀率)
#
# 協定裡沒有獨立的「快門角度」欄位可寫（CanSetInfo5 的 tag 214 ShutterAngle
# 只是能力回報）。所以這裡把角度換算成秒數，再走一般的 ShutterSpeed 欄位 ——
# 對相機來說沒有差別，對使用者來說是他習慣的單位。
#
# 因此角度**一定要有幀率**才有意義。幀率由呼叫端提供，而 bridge 會先從
# DataGroupMovie tag 61 讀出相機實際的幀率再傳進來。
# ─────────────────────────────────────────────────────────────────────

#: 電影 / 影片常見的快門角度
COMMON_SHUTTER_ANGLES = (11.25, 22.5, 45, 90, 120, 144, 172.8, 180, 270, 360)

DEFAULT_FRAME_RATE = 24.0


def angle_to_seconds(angle: float, frame_rate: float) -> float:
    """快門角度 → 曝光秒數。"""
    if frame_rate <= 0:
        raise SettingError(f"幀率必須大於 0，收到 {frame_rate}")
    if not 0 < angle <= 360:
        raise SettingError(f"快門角度要在 0–360 之間，收到 {angle}")
    return angle / (360.0 * frame_rate)


def seconds_to_angle(seconds: float, frame_rate: float) -> float:
    """曝光秒數 → 快門角度。超過 360°（曝光比一個影格還長）會夾在 360。"""
    if frame_rate <= 0 or not seconds:
        raise SettingError("幀率與快門秒數都必須大於 0")
    return min(360.0, round(360.0 * seconds * frame_rate, 1))


def nearest_shutter_angle(angle: float, frame_rate: float) -> tuple[float, float]:
    """把想要的角度對到相機真的做得到的快門值。

    快門是離散的（APEX 表），所以不是每個角度都做得出來。這裡先換成秒數、
    對到最近的合法快門，再換回角度 —— 回傳 (實際角度, 實際秒數)，
    讓呼叫端能誠實告訴使用者拿到的是什麼，而不是假裝設成了 180.0。
    """
    wanted = angle_to_seconds(angle, frame_rate)
    code = SHUTTER.encode_uint8(wanted)
    actual = SHUTTER.decode_uint8(code)
    if actual is None:
        raise SettingError(f"快門角度 {angle}° @ {frame_rate}fps 換算不出合法的快門值")
    return seconds_to_angle(actual, frame_rate), actual


# ─────────────────────────────────────────────────────────────────────
# 編碼 / 解碼
# ─────────────────────────────────────────────────────────────────────


def decode_value(setting: Setting, raw) -> Any:
    """相機的原始值 → 人看得懂的值。無法解讀時回 None。"""
    if raw is None:
        return None
    if setting.kind == "enum":
        # sigma-ptpy 多半已經回傳 enum member 了，但保險起見兩種都接
        if hasattr(raw, "name"):
            return raw.name
        try:
            return setting.enum_cls(raw).name
        except (ValueError, TypeError):
            return None
    if setting.kind == "apex":
        return setting.converter.decode_uint8(int(raw))
    return int(raw)


def encode_value(setting: Setting, value) -> Any:
    """人給的值 → 相機的原始值。

    Raises:
        SettingError: 值不合法，或（光圈的情況）踩到 sigma-ptpy 表裡的壞碼。
    """
    if not setting.writable:
        raise SettingError(f"{setting.name} 是唯讀的")

    if setting.kind == "enum":
        if isinstance(value, setting.enum_cls):
            return value
        # 名稱優先
        if isinstance(value, str):
            try:
                return setting.enum_cls[value]
            except KeyError:
                pass
        # 數值：enum 認得就轉成成員，不認得就原樣送出去。
        #
        # 後者是必要的 —— 相機宣告的可用值可能超出 sigma-ptpy 的 enum（實測
        # 色彩模式有 16 個，enum 只認得 12 個）。UI 把認不得的顯示成數字，
        # 這裡就要收得下那個數字，否則畫得出來卻按不下去。
        try:
            raw = int(value)
        except (TypeError, ValueError):
            allowed = ", ".join(m.name for m in setting.enum_cls)
            raise SettingError(
                f"{setting.name} 不接受 {value!r}；可用值：{allowed}"
            ) from None
        try:
            return setting.enum_cls(raw)
        except ValueError:
            return raw

    if setting.kind == "apex":
        try:
            code = setting.converter.encode_uint8(float(value))
        except (TypeError, ValueError):
            raise SettingError(f"{setting.name} 需要數值，收到 {value!r}") from None
        if code is None:
            raise SettingError(f"{setting.name}={value!r} 換算不出相機編碼")
        if setting.name == "aperture" and code in _BROKEN_APERTURE_CODES:
            raise SettingError(
                f"f/{value} 對應到 sigma-ptpy 換算表裡的壞碼 {code}"
                "（上游把 101 打成 191），請改用相鄰的光圈值"
            )
        return int(code)

    try:
        return int(value)
    except (TypeError, ValueError):
        raise SettingError(f"{setting.name} 需要整數，收到 {value!r}") from None


# ─────────────────────────────────────────────────────────────────────
# 讀 / 寫
# ─────────────────────────────────────────────────────────────────────


SHUTTER_ANGLE = "shutter_angle"


#: 相機主動回報、但不是「設定」的欄位 —— 唯讀狀態。
#:
#: 這些一直都在 DataGroup1/3 裡，只是沒被讀出來。其中 MediaFreeSpace 特別值得
#: 一提：這個專案先前懷疑「拍不成是不是記憶卡滿了」，還記下「主機端查不到」——
#: 其實一直查得到，只是沒去看。
STATUS_FIELDS = (
    ("media_free_space", 1, "MediaFreeSpace"),
    ("media_status", 1, "MediaStatus"),
    ("battery_state", 1, "BatteryState"),
    ("frame_buffer_state", 1, "FrameBufferState"),
    ("battery_kind", 3, "BatteryKind"),
    ("lens_focal_mm", 1, "CurrentLensFocalLength"),
    ("lens_wide_mm", 3, "LensWideFocalLength"),
    ("lens_tele_mm", 3, "LensTeleFocalLength"),
    # live view 放大倍率。CanSetInfo5 701 是空的（不可設定），所以只讀不寫。
    ("lv_magnify_ratio", 4, "LVMagnifyRatio"),
    # 間隔計時器的剩餘量。這兩個是相機自己倒數的，不是設定。
    ("interval_seconds_remain", 5, "IntervalTimerSecondRemain"),
    ("interval_frames_remain", 5, "IntervalTimerFrameRemain"),
)


def read_status(cam) -> dict[str, Any]:
    """相機的唯讀狀態：卡片剩餘空間、電池、緩衝區、鏡頭焦段範圍。

    跟 read_settings 分開，因為這些不是設定 —— 寫不進去，也不該出現在
    設定介面裡。某一組讀失敗不會拖垮其他組。
    """
    out: dict[str, Any] = {}
    cache: dict[int, Any] = {}
    for name, group, attr in STATUS_FIELDS:
        if group not in cache:
            try:
                cache[group] = getattr(cam, f"get_cam_data_group{group}")()
            except Exception:
                cache[group] = None
        obj = cache[group]
        value = getattr(obj, attr, None) if obj is not None else None
        if value is None:
            out[name] = None
        elif hasattr(value, "name"):
            out[name] = value.name
        else:
            out[name] = value
    return out


def read_settings(cam, frame_rate: float | None = None) -> dict[str, Any]:
    """讀回全部設定（人看得懂的值）。

    某個 DataGroup 讀失敗不會拖垮其他組 —— 該組的欄位留 None。
    """
    result: dict[str, Any] = {}
    groups: dict[int, Any] = {}

    for number in sorted(GROUP_CLASSES):
        try:
            groups[number] = getattr(cam, f"get_cam_data_group{number}")()
        except Exception:
            groups[number] = None

    for setting in SETTINGS:
        group = groups.get(setting.group)
        if group is None:
            result[setting.name] = None
            continue
        try:
            result[setting.name] = decode_value(setting, getattr(group, setting.field, None))
        except Exception:
            result[setting.name] = None

    # 衍生值：由快門秒數與幀率算出來，不是相機的獨立欄位
    seconds = result.get("shutter_speed")
    if seconds and frame_rate:
        try:
            result[SHUTTER_ANGLE] = seconds_to_angle(seconds, frame_rate)
        except SettingError:
            result[SHUTTER_ANGLE] = None
    else:
        result[SHUTTER_ANGLE] = None

    return result


def check_applies_to_mode(name: str, mode: str | None,
                          also_allowed: set | None = None) -> None:
    """擋掉在目前機身模式下無效的設定。

    不擋的話，相機會收下寫入然後默默丟掉 —— 使用者只看到「設了沒反應」，
    這正是實測時在 CINE 模式寫 shutter_speed 遇到的狀況。與其讓人查半天，
    不如直接說清楚哪裡不對、該用什麼代替。
    """
    if mode is None:
        return
    if also_allowed and name in also_allowed:
        return
    setting = BY_NAME.get(name)
    if setting is None or setting.applies_to in ("both", mode):
        return
    raise SettingError(
        f"{name} 在{'錄影' if mode == 'movie' else '拍照'}模式下無效"
        + (f"。{setting.note}" if setting.note else "")
    )


def apply_settings(cam, changes: dict[str, Any],
                   capabilities: dict | None = None,
                   frame_rate: float | None = None,
                   mode: str | None = None,
                   also_allowed: set | None = None) -> dict[str, Any]:
    """套用一批設定變更。

    同一個 DataGroup 的欄位會合併成一次寫入 —— 除了少一趟 USB，更重要的是
    相機看到的是一組一致的變更（例如同時改光圈和快門時，不會有中間狀態）。

    Raises:
        SettingError: 任何一個名稱或值不合法。整批都不會送出 —— 先全部驗完
            再寫，避免一半成功一半失敗這種難以收拾的狀態。
    """
    if not changes:
        return {}

    changes = dict(changes)

    # 設 ISO 但沒關 ISO Auto 是白費工 —— 相機的自動曝光會立刻覆蓋掉。
    # 實測確認過：iso_auto=Auto 時寫 iso 完全沒作用，先設成 Manual 就成功。
    # 這跟 set_focus_position() 必須在同一次寫入裡關掉 AFLock / PreConstAF
    # 是同一個道理：要手動控制，就得先把搶控制權的自動子系統關掉。
    # 兩者同屬 DataGroup1，會合併成一次寫入，不會有中間狀態。
    if "iso" in changes and "iso_auto" not in changes:
        changes["iso_auto"] = "Manual"

    # shutter_angle 沒有對應的相機欄位 —— 換算成 shutter_speed 再照一般流程走
    if SHUTTER_ANGLE in changes:
        if "shutter_speed" in changes:
            raise SettingError("shutter_angle 與 shutter_speed 不能同時指定，它們是同一件事")
        if not frame_rate:
            raise SettingError(
                "設定 shutter_angle 需要幀率 —— 角度要有幀率才有意義"
            )
        angle = changes.pop(SHUTTER_ANGLE)
        try:
            actual_angle, seconds = nearest_shutter_angle(float(angle), frame_rate)
        except (TypeError, ValueError) as e:
            raise SettingError(f"快門角度不合法：{angle!r}") from e
        changes["shutter_speed"] = seconds
        applied_angle = actual_angle
    else:
        applied_angle = None

    unknown = set(changes) - set(BY_NAME)
    if unknown:
        raise SettingError(
            f"不認得的設定：{', '.join(sorted(unknown))}。"
            f"可用：{', '.join(sorted(BY_NAME))}"
        )

    # 先全部驗證 + 編碼，通過了才動相機
    by_group: dict[int, dict[str, Any]] = {}
    for name, value in changes.items():
        setting = BY_NAME[name]
        check_applies_to_mode(name, mode, also_allowed)
        check_within_capabilities(name, value, capabilities)
        encoded = encode_value(setting, value)
        by_group.setdefault(setting.group, {})[setting.field] = encoded

    for number, fields in sorted(by_group.items()):
        payload = GROUP_CLASSES[number](**fields)
        getattr(cam, f"set_cam_data_group{number}")(payload)

    result = {name: changes[name] for name in changes}
    if applied_angle is not None:
        # 誠實回報實際拿到的角度：快門是離散的，要的 180° 未必做得出來
        result[SHUTTER_ANGLE] = applied_angle
    return result


#: 會被相機的自動曝光子系統蓋掉的設定，以及要手動控制它所需要的模式。
#
# 這跟對焦是同一個模式：set_focus_position() 必須同時關掉 AFLock 與
# PreConstAF，否則相機的自動對焦會立刻把你設的位置搶回去。曝光也一樣 ——
# 在 P / A 模式下寫快門，相機的自動曝光下一瞬間就覆蓋掉了，寫入本身
# 「成功」但值不會留下。
AUTO_OVERRIDE_HINTS = {
    "shutter_speed": ("Manual", "ShutterPriority"),
    "shutter_angle": ("Manual", "ShutterPriority"),
    "aperture": ("Manual", "AperturePriority"),
}


#: 比對回讀值時允許的相對誤差。只用來吸收浮點表示誤差，不是用來容忍
#: 相機給了不同的值 —— 先前設 2% 的後果是：寫 29.97 相機存 30，
#: 差 0.1% 被判定為相等，於是「寫入沒生效」完全不會被報出來。
VALUE_EPSILON = 1e-6


def _roughly_equal(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == 0 or b == 0:
            return a == b
        return abs(a - b) / max(abs(a), abs(b)) < VALUE_EPSILON
    return str(a) == str(b)


def verify_applied(cam, requested: dict[str, Any],
                   frame_rate: float | None = None) -> dict[str, Any]:
    """寫入後回讀，找出相機實際上沒有接受的設定。

    「寫了沒反應」是最難查的一種問題 —— PTP 寫入會回 OK，但相機可能
    默默忽略。與其讓使用者盯著沒變的畫面猜，不如直接比對並講清楚。

    Returns:
        {name: {"requested": x, "actual": y, "hint": str | None}}，只含不符的項目。
    """
    actual = read_settings(cam, frame_rate)
    mode = actual.get("exposure_mode")
    rejected: dict[str, Any] = {}

    for name, wanted in requested.items():
        if name not in actual:
            continue
        got = actual[name]
        if _roughly_equal(got, wanted):
            continue
        hint = None
        modes = AUTO_OVERRIDE_HINTS.get(name)
        if modes and mode not in modes:
            hint = (
                f"曝光模式目前是 {mode}，相機的自動曝光會覆蓋手動設的{name}。"
                f"先把 exposure_mode 設成 {' 或 '.join(modes)} 再試。"
            )
        rejected[name] = {"requested": wanted, "actual": got, "hint": hint}

    return rejected


def check_within_capabilities(name: str, value, capabilities: dict | None) -> None:
    """拿相機回報的實際範圍驗證數值。

    APEX 換算表涵蓋的範圍遠大於任何一台實機接受的範圍（ISO 表從 6 到
    102400，但 fp 只吃 100–25600）。不擋的話，選到範圍外的值就是送出去
    被相機默默拒絕 —— 使用者只會看到「設了沒反應」。

    Raises:
        SettingError: 超出相機回報的範圍。
    """
    if not capabilities:
        return
    limits = capabilities.get(name)
    if not limits:
        return
    lo, hi = limits.get("min"), limits.get("max")
    if lo is None or hi is None:
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return
    if not (lo <= numeric <= hi):
        raise SettingError(
            f"{name}={value} 超出相機回報的範圍 {lo}–{hi}"
        )


#: 設定 ↔ CanSetInfo5 的 tag。用來知道「相機**現在**接受哪些值」。
#:
#: 為什麼需要：enum 只說明「這個欄位理論上有哪些值」，而 sigma-ptpy 的 enum
#: 未必跟得上韌體。實例：這台相機宣告 16 個色彩模式（含 13/14/15/16），
#: sigma-ptpy 只認得 12 個 —— 於是 UI 少了四個選項，其中一個就是 CinemaDNG
#: 會用到的「Off」。以相機宣告的為準，enum 只負責提供名稱。
#:
#: 名稱取自 sigma-ptpy 的 CanSetInfo5 欄位表。
#: ⚠️ 只列**確認過是值列表**的。開關類設定（500~505、810）的編碼不是這個
#: 形狀 —— DCCropMode 宣告 [1, 0, -1]（-1 不是合法 enum 值），LOCVignetting
#: 宣告 [0, -1] 而目前值是 Off(2) 根本不在裡面。我先前記下了「這些欄位的編碼
#: 形狀不一致」，然後還是把它們當值列表用，結果 DC 裁切的選項變成「關 自動 -1」。
#:
#: 判斷依據：目前生效的值必須出現在宣告清單裡。不在，那就不是值列表。
#: 只用來判斷「現在能不能設」，不看內容。
#:
#: 這兩件事的可靠程度不同：清單的**內容**編碼不一致（DCCropMode 給
#: [1, 0, -1]，LOCVignetting 給 [0, -1] 卻不含目前值），但**空不空**是一致
#: 的 —— 空就是當下不可設定。實測 EImageStab 在 MOV 下是 [0, 1]，切到
#: CinemaDNG 就變成 []，而 fp 的電子防手震確實不支援 CinemaDNG。
INFO5_AVAILABILITY_TAGS = {
    "drive_mode": 1, "cont_shoot_speed": 2,
    "interval_timer_frames": 3, "interval_timer_seconds": 4,
    "image_quality": 11, "dng_quality": 12,
    "resolution": 20, "aspect_ratio": 21,
    "exposure_mode": 200, "metering_mode": 250,
    "white_balance": 301, "color_mode": 320,
    "fill_light": 340, "hdr": 350,
    "dc_crop_mode": 500,
    "loc_distortion": 501, "loc_chromatic_aberration": 502,
    "loc_diffraction": 503, "loc_vignetting": 504,
    "electronic_stabilization": 810,
}

INFO5_CHOICE_TAGS = {
    "drive_mode": 1,
    "image_quality": 11,
    "dng_quality": 12,
    "resolution": 20,
    "aspect_ratio": 21,
    "exposure_mode": 200,
    "metering_mode": 250,
    "white_balance": 301,
    "color_mode": 320,
}


def label_for_value(setting: "Setting", raw: int) -> str:
    """把原始數值換成名稱；enum 不認得就回數字字串。

    不認得**不代表不能用** —— 相機宣告它可設定就是可設定。顯示成數字至少
    讓人選得到，也讓「這是韌體有而函式庫沒有的值」這件事看得出來。
    """
    if setting.enum_cls is not None:
        try:
            return setting.enum_cls(raw).name
        except ValueError:
            pass
    return str(raw)


def describe(capabilities: dict | None = None,
             mode: str | None = None,
             also_allowed: set | None = None,
             choice_values: dict | None = None,
             current_values: dict | None = None) -> list[dict[str, Any]]:
    """所有設定的中繼資料，給 UI 生控制項用。

    Args:
        capabilities: sigma_fp_focus.read_capabilities() 的結果。給了的話，
            數值型選項會依相機實際接受的範圍過濾 —— UI 才不會列出一堆
            按下去只會失敗的值。
    """
    out = []
    for setting in SETTINGS:
        # 不適用於目前模式的設定直接不列 —— 列出來只會誘人去踩空。
        # also_allowed 是例外：錄影模式下相機切到「快門速度」時，
        # shutter_speed 就是有效的（見 DataGroupMovie tag 6）。
        if (mode and setting.applies_to not in ("both", mode)
                and not (also_allowed and setting.name in also_allowed)):
            continue
        entry: dict[str, Any] = {
            "name": setting.name,
            "applies_to": setting.applies_to,
            "kind": setting.kind,
            "unit": setting.unit,
            "group": setting.group,
            "writable": setting.writable,
            "note": setting.note,
        }
        # 相機宣告的空清單 = 當下不可設定。只看空不空，不看內容 ——
        # 內容的編碼不一致，但這個信號是一致的。
        declared_any = (choice_values or {}).get(setting.name)
        if declared_any is not None and len(declared_any) == 0:
            entry["writable"] = False
            entry["note"] = (setting.note + " " if setting.note else "") + \
                "（相機在目前的格式／模式下不開放調整）"
        if setting.kind == "enum":
            # 相機宣告的優先，但要先確認那真的是一份值列表。
            #
            # 防線：清單裡不能有負數（不是合法的 enum 值），而且目前生效的值
            # 必須在裡面。兩者任一不成立，那個 tag 的編碼就不是「可選值」——
            # 有些欄位放的是 min/max/step 之類的東西。
            declared = (choice_values or {}).get(setting.name)
            current = (current_values or {}).get(setting.name)
            usable = bool(declared) and all(v >= 0 for v in declared)
            if usable:
                # 驗不了就不信。這類清單已經騙過兩次（DC 裁切的 [1,0,-1]、
                # 周邊光量的 [0,-1]），所以「無法確認」要當成「不是值列表」，
                # 不是當成通過。代價只是設定還沒讀到時先用 enum，下一輪就對了。
                usable = False
                try:
                    usable = setting.enum_cls[current].value in declared
                except (KeyError, TypeError):
                    pass
            if usable:
                entry["choices"] = [label_for_value(setting, v) for v in declared]
            else:
                entry["choices"] = [m.name for m in setting.enum_cls]
        elif setting.kind == "apex":
            # 換算表是這個型別「理論上」的所有值
            values = sorted({v for _, v in setting.converter._ApexConverter__dectable})
            limits = (capabilities or {}).get(setting.name)
            if limits and limits.get("min") is not None and limits.get("max") is not None:
                lo, hi = limits["min"], limits["max"]
                values = [v for v in values if lo <= v <= hi]
                entry["range"] = {"min": lo, "max": hi}
            entry["choices"] = values
        out.append(entry)

    # 衍生設定：沒有對應的相機欄位，由 shutter_speed + 幀率換算。
    # 錄影模式下不要列 —— 那裡有真正的 DataGroupMovie tag 7，兩個同名會打架，
    # 而且真的那個才是相機實際採用的值。
    if mode == "movie":
        return out
    out.append({
        "name": SHUTTER_ANGLE,
        "kind": "derived",
        "unit": "deg",
        "group": 1,
        "writable": True,
        "note": "電影用的快門表示法。需要幀率才有意義；實際角度受限於"
                "相機的離散快門值，回應會告訴你真正拿到的角度。",
        "choices": list(COMMON_SHUTTER_ANGLES),
    })
    return out
