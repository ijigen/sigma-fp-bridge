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


SETTINGS: tuple[Setting, ...] = (
    # ── 曝光三要素 ───────────────────────────────────────────────────
    Setting("exposure_mode", 2, "ExposureMode", "enum", enum_cls=E.ExposureMode,
            note="P / A / S / M。A 模式下快門由相機決定，S 模式下光圈由相機決定。"),
    Setting("aperture", 1, "Aperture", "apex", unit="f", converter=APERTURE),
    Setting("shutter_speed", 1, "ShutterSpeed", "apex", unit="s", converter=SHUTTER),
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
            note="DNG / JPEG / 兩者。這是靜態照片的格式，不是錄影格式。"),
    Setting("dng_quality", 4, "DNGQuality", "enum", enum_cls=E.DNGQuality,
            note="DNG 位元深度（12 / 14 bit）。"),
    Setting("resolution", 2, "Resolution", "enum", enum_cls=E.Resolution),
    Setting("aspect_ratio", 5, "AspectRatio", "enum", enum_cls=E.AspectRatio),

    # ── 影像風格 ─────────────────────────────────────────────────────
    Setting("color_mode", 3, "ColorMode", "enum", enum_cls=E.ColorMode),
    Setting("color_space", 3, "ColorSpace", "enum", enum_cls=E.ColorSpace),
    Setting("tone_effect", 5, "ToneEffect", "enum", enum_cls=E.ToneEffect),

    # ── 驅動 ─────────────────────────────────────────────────────────
    Setting("drive_mode", 2, "DriveMode", "enum", enum_cls=E.DriveMode),
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
# 因此角度**一定要有幀率**才有意義。幀率目前由呼叫端提供（機身的實際幀率
# 在 DataGroupMovie 裡，但那個 DataGroup 的 tag 編號還沒解出來）。
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
        try:
            if isinstance(value, str):
                return setting.enum_cls[value]
            return setting.enum_cls(int(value))
        except (KeyError, ValueError, TypeError):
            allowed = ", ".join(m.name for m in setting.enum_cls)
            raise SettingError(
                f"{setting.name} 不接受 {value!r}；可用值：{allowed}"
            ) from None

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


def apply_settings(cam, changes: dict[str, Any],
                   capabilities: dict | None = None,
                   frame_rate: float | None = None) -> dict[str, Any]:
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


def _roughly_equal(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == 0 or b == 0:
            return a == b
        return abs(a - b) / max(abs(a), abs(b)) < 0.02
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


def describe(capabilities: dict | None = None) -> list[dict[str, Any]]:
    """所有設定的中繼資料，給 UI 生控制項用。

    Args:
        capabilities: sigma_fp_focus.read_capabilities() 的結果。給了的話，
            數值型選項會依相機實際接受的範圍過濾 —— UI 才不會列出一堆
            按下去只會失敗的值。
    """
    out = []
    for setting in SETTINGS:
        entry: dict[str, Any] = {
            "name": setting.name,
            "kind": setting.kind,
            "unit": setting.unit,
            "group": setting.group,
            "writable": setting.writable,
            "note": setting.note,
        }
        if setting.kind == "enum":
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

    # 衍生設定：沒有對應的相機欄位，由 shutter_speed + 幀率換算
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
