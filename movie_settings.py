#!/usr/bin/env python3
"""錄影（CINE）模式的設定：DataGroupMovie，opcode 0x9033 / 0x9034。

sigma-ptpy 定義了這兩個 opcode，但沒有 schema class 也沒有方法 —— 跟當初
FocusPosition 的處境一樣。這裡自己組 IFD 收送。

**為什麼需要它**：機身撥桿切到 CINE 之後，曝光改由這個 DataGroup 管。
寫 DataGroup1 的 ShutterSpeed 相機會收下然後丟掉，值完全不動 —— 實測踩過。

tag 編號怎麼來的
----------------
不是猜的。DataGroupMovie 的 tag 對應 CanSetInfo5 的 tag 減 100：

    CanSetInfo5 161 FrameRate              -> movie 61   讀到 23.98
    CanSetInfo5 151 CinemaDNGImageQuality  -> movie 51   讀到 12（12-bit）
    CanSetInfo5 150 RecordFormat           -> movie 50
    CanSetInfo5 160 MovieResolution        -> movie 60
    CanSetInfo5 110..113 音訊那組          -> movie 10..13

快門角度不遵循這個位移（CanSetInfo5 是 214），但確認方式更直接：
CanSetInfo5 tag 214 回報的合法值清單第一個就是 (112, 3600)，而 movie tag 7
當下讀到的正是 (112, 3600)。整份清單換算後是標準的電影快門角度序列
（11.2 / 22.5 / 45 / … / 172.8 / 180 / … / 360），不可能是別的東西。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sigma_ptpy.schema import DirectoryType as DT
from sigma_ptpy.schema import _DirectoryEntrySchema

from ifd import find_tag, parse_ifd

#: 快門角度的分母固定 3600 = 360.0° × 10，所以分子就是「角度 × 10」。
ANGLE_SCALE = 10
ANGLE_DENOMINATOR = 3600


class MovieSettingError(ValueError):
    """錄影設定的名稱或值不合法。"""


@dataclass(frozen=True)
class MovieSetting:
    name: str
    tag: int
    type: Any
    kind: str          # "angle" | "rational" | "int"
    unit: str = ""
    note: str = ""


MOVIE_SETTINGS: tuple[MovieSetting, ...] = (
    MovieSetting("shutter_angle", 7, DT.URational, "angle", "deg",
                 "電影快門角度。CINE 模式下快門只能從這裡設，"
                 "寫 DataGroup1 的 shutter_speed 沒有作用。"),
    MovieSetting("record_format", 50, DT.UInt8, "int",
                 note="1 = CinemaDNG、2 = MOV，兩者都以實際錄製的產物確認過。"),
    MovieSetting("cinema_dng_quality", 51, DT.UInt8, "int", "bit",
                 "CinemaDNG 位元深度（12 / 10 / 8）。改 record_format 時相機會"
                 "自己連動調整這個值，寫入後務必回讀。"),
    MovieSetting("mov_image_quality", 52, DT.UInt8, "int"),
    MovieSetting("movie_resolution", 60, DT.UInt8, "int"),
    MovieSetting("frame_rate", 61, DT.URational, "rational", "fps"),
)

MOVIE_BY_NAME = {s.name: s for s in MOVIE_SETTINGS}

#: 已經用實機確認過的數值標籤。沒確認的一律不列 —— 猜錯的標籤比沒有標籤更糟，
#: 因為後面的人會相信它。
VALUE_LABELS: dict[str, dict[int, str]] = {
    # 空卡上各錄一段、由人眼確認產物：
    #   record_format=2 -> CINEMA/A001_001_20260801.MOV
    #   record_format=1 -> CinemaDNG 序列（8-bit UHD 23.98fps）
    "record_format": {1: "CinemaDNG", 2: "MOV"},
    # 上面那段 CinemaDNG 錄出來是 UHD，當時 movie_resolution = 2。
    # 合法值是 [2, 1]，所以 1 很可能是 FHD —— 但沒實測過，不列。
    "movie_resolution": {2: "UHD"},
}

#: CanSetInfo5 裡對應的「合法值清單」tag，用來限制可設的範圍。
CAPABILITY_TAGS = {
    "shutter_angle": 214,
    "record_format": 150,
    "cinema_dng_quality": 151,
    "mov_image_quality": 152,
    "movie_resolution": 160,
    "frame_rate": 161,
}


def _encoder():
    return type("_MovieIFD", (_DirectoryEntrySchema,), {})()


# ─────────────────────────────────────────────────────────────────────
# 傳輸
# ─────────────────────────────────────────────────────────────────────


def read_raw(cam) -> bytes:
    """發 SigmaGetCamDataGroupMovie (0x9033)，取回原始 IFD payload。"""
    from construct import Container

    ptp = Container(
        OperationCode="SigmaGetCamDataGroupMovie",
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=[],
    )
    return bytes(cam.recv(ptp).Data)


def write_raw(cam, entries: list[tuple[int, Any, Any]]):
    """發 SigmaSetCamDataGroupMovie (0x9034)。

    Args:
        entries: [(tag, DirectoryType, value)]，只放要改的欄位 ——
            IFD 是稀疏的，沒帶到的 tag 相機不會動。
    """
    from construct import Container

    payload = _encoder()._encode(entries)
    ptp = Container(
        OperationCode="SigmaSetCamDataGroupMovie",
        SessionID=cam._session,
        TransactionID=cam._transaction,
        Parameter=[],
    )
    return cam.send(ptp, payload)


# ─────────────────────────────────────────────────────────────────────
# 值的換算
# ─────────────────────────────────────────────────────────────────────


def decode(setting: MovieSetting, values) -> Any:
    if not values:
        return None
    v = values[0]
    if setting.kind == "angle":
        num, _den = v
        return num / ANGLE_SCALE
    if setting.kind == "rational":
        num, den = v
        return round(num / den, 3) if den else None
    return int(v)


def encode(setting: MovieSetting, value) -> Any:
    if setting.kind == "angle":
        try:
            return (int(round(float(value) * ANGLE_SCALE)), ANGLE_DENOMINATOR)
        except (TypeError, ValueError):
            raise MovieSettingError(f"{setting.name} 需要數值，收到 {value!r}") from None
    if setting.kind == "rational":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise MovieSettingError(f"{setting.name} 需要數值，收到 {value!r}") from None
        # 23.98 / 29.97 這種要保留兩位小數才對得上相機回報的 (2398, 100)
        return (int(round(f * 100)), 100)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise MovieSettingError(f"{setting.name} 需要整數，收到 {value!r}") from None


def _capability_values(info5_ifd, name: str) -> list | None:
    """從 CanSetInfo5 取這個設定的合法值清單。"""
    tag = CAPABILITY_TAGS.get(name)
    if tag is None or info5_ifd is None:
        return None
    entry = find_tag(info5_ifd, tag)
    if entry is None or not entry.values:
        return None
    setting = MOVIE_BY_NAME[name]
    if setting.kind == "angle":
        return [num / ANGLE_SCALE for num, _ in entry.values]
    if setting.kind == "rational":
        return [round(num / den, 3) for num, den in entry.values if den]
    return list(entry.values)


# ─────────────────────────────────────────────────────────────────────
# 高階讀寫
# ─────────────────────────────────────────────────────────────────────


def read_settings(cam) -> dict[str, Any]:
    """讀目前的錄影設定。相機不在錄影模式時回空 dict。"""
    raw = read_raw(cam)
    if not raw:
        return {}
    ifd = parse_ifd(raw)
    out: dict[str, Any] = {}
    for setting in MOVIE_SETTINGS:
        entry = find_tag(ifd, setting.tag)
        out[setting.name] = decode(setting, entry.values) if entry else None
    return out


def read_capabilities(cam, info5_raw: bytes | None) -> dict[str, list]:
    """每個錄影設定的合法值清單（來自 CanSetInfo5）。"""
    if not info5_raw:
        return {}
    try:
        ifd = parse_ifd(info5_raw)
    except Exception:
        return {}
    caps = {}
    for name in MOVIE_BY_NAME:
        values = _capability_values(ifd, name)
        if values:
            caps[name] = values
    return caps


def apply_settings(cam, changes: dict[str, Any],
                   capabilities: dict[str, list] | None = None) -> dict[str, Any]:
    """套用錄影設定。

    跟靜態設定同樣的規矩：整批先驗證再送，一筆不合法就整批不動。

    Raises:
        MovieSettingError: 名稱不存在，或值不在相機回報的合法清單裡。
    """
    if not changes:
        return {}

    unknown = set(changes) - set(MOVIE_BY_NAME)
    if unknown:
        raise MovieSettingError(
            f"不認得的錄影設定：{', '.join(sorted(unknown))}。"
            f"可用：{', '.join(sorted(MOVIE_BY_NAME))}"
        )

    entries = []
    for name, value in changes.items():
        setting = MOVIE_BY_NAME[name]
        allowed = (capabilities or {}).get(name)
        if allowed is not None:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = None
            if numeric is not None and not any(abs(numeric - a) < 1e-6 for a in allowed):
                raise MovieSettingError(
                    f"{name}={value} 不在相機接受的清單裡：{allowed}"
                )
        entries.append((setting.tag, setting.type, encode(setting, value)))

    write_raw(cam, entries)
    return dict(changes)


def describe(capabilities: dict[str, list] | None = None) -> list[dict[str, Any]]:
    """給 UI 用的中繼資料。"""
    caps = capabilities or {}
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "unit": s.unit,
            "tag": s.tag,
            "note": s.note,
            "choices": caps.get(s.name),
            "labels": VALUE_LABELS.get(s.name),
        }
        for s in MOVIE_SETTINGS
    ]
