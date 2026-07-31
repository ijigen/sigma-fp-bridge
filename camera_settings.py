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


def read_settings(cam) -> dict[str, Any]:
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

    return result


def apply_settings(cam, changes: dict[str, Any]) -> dict[str, Any]:
    """套用一批設定變更。

    同一個 DataGroup 的欄位會合併成一次寫入 —— 除了少一趟 USB，更重要的是
    相機看到的是一組一致的變更（例如同時改光圈和快門時，不會有中間狀態）。

    Raises:
        SettingError: 任何一個名稱或值不合法。整批都不會送出 —— 先全部驗完
            再寫，避免一半成功一半失敗這種難以收拾的狀態。
    """
    if not changes:
        return {}

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
        encoded = encode_value(setting, value)
        by_group.setdefault(setting.group, {})[setting.field] = encoded

    for number, fields in sorted(by_group.items()):
        payload = GROUP_CLASSES[number](**fields)
        getattr(cam, f"set_cam_data_group{number}")(payload)

    return {name: changes[name] for name in changes}


def describe() -> list[dict[str, Any]]:
    """所有設定的中繼資料，給 UI 生控制項用。"""
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
            # 換算表就是相機接受的離散值清單，直接給 UI 當選項
            values = sorted({v for _, v in setting.converter._ApexConverter__dectable})
            entry["choices"] = values
        out.append(entry)
    return out
