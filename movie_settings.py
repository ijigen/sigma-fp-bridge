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

已用對照法解出的低位 tag（改一項設定、dump、看哪個值跟著動）：

    tag 3 = 光圈自動旗標   Manual 0 / A 0 / S 1 / P 1
    tag 4 = 快門自動旗標   Manual 0 / A 1 / S 0 / P 1

    也就是說錄影模式把曝光模式拆成兩個獨立的自動旗標，而不是一個 enum。
    （不需要靠它們設定曝光模式 —— DataGroup2 的 ExposureMode 在 CINE 下
    照樣有效，實測過。）

    tag 1 = 2、tag 5 = 0、tag 6 = 2 對曝光模式 / 補償 / 測光 / ISO 自動 /
    光圈的變更都不動，意義未知。

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
    MovieSetting("capture_mode", 1, DT.UInt8, "int",
                 note="機身的拍照 / 錄影模式。1 = STILL、2 = CINE，兩者都以實機"
                      "確認（寫入後機身螢幕真的切換，且能力清單整組對調）。"
                      "注意切換模式會連帶改變 shutter_unit。"),
    MovieSetting("shutter_angle", 7, DT.URational, "angle", "deg",
                 "電影快門角度。CINE 模式下快門只能從這裡設，"
                 "寫 DataGroup1 的 shutter_speed 沒有作用。"),
    MovieSetting("shutter_unit", 6, DT.UInt8, "int",
                 note="錄影時快門用「速度」還是「角度」表示。1 = 速度、2 = 角度，"
                      "兩者都以實機寫入確認。這個 tag 決定相機接受哪個欄位 —— "
                      "在角度模式下寫 shutter_speed 會被收下然後丟棄。"),
    MovieSetting("record_format", 50, DT.UInt8, "int",
                 note="1 = CinemaDNG、2 = MOV，兩者都以實際錄製的產物確認過。"),
    MovieSetting("cinema_dng_quality", 51, DT.UInt8, "int", "bit",
                 "CinemaDNG 位元深度（12 / 10 / 8）。改 record_format 時相機會"
                 "自己連動調整這個值，寫入後務必回讀。"),
    MovieSetting("mov_image_quality", 52, DT.UInt8, "int",
                 note="值 1 / 2 的意義未確認。實測相機在 MOV / CinemaDNG × "
                      "UHD / FHD 四種組合、以及多個幀率下都不開放調整它，"
                      "所以無法用『各錄一段比對產出』的方式判讀。"),
    MovieSetting("movie_resolution", 60, DT.UInt8, "int"),

    # ── 音訊 ─────────────────────────────────────────────────────────
    #
    # 名稱來自 sigma-ptpy 的 CamCanSetInfo5 欄位表（schema.py），對照關係是
    # 「CanSetInfo5 tag = DataGroupMovie tag + 100」—— 這個規律在所有已識別的
    # 設定上都成立（150/50、151/51、152/52、160/60、161/61、162/62）。
    #
    # 這解釋了 tag 10 為什麼是主開關：錄音關掉之後，聲道數、增益方式、手動
    # 增益、風切濾波全都沒有意義，所以 112/113/114 一起變成不可設定。也解釋
    # 了為什麼它們對影像管線和 HDMI 輸出都毫無影響 —— 它們不是影像設定。
    #
    # ⚠️ 尚未在硬體上驗證。實測 tag 11 = 2 時錄出來的音訊仍是 2ch/16bit/48kHz、
    # 192192 samples，跟 tag 11 = 1 完全相同，這與「聲道數」的名稱對不上。
    # 可能是值不直接等於聲道數，也可能是名稱本身不精確。要確認的方法是把
    # audio_record 關掉錄一段 —— 沒有音訊軌就證實了。
    MovieSetting("audio_record", 10, DT.UInt8, "int",
                 note="錄音開關（CanSetInfo5: AudioRecord）。關掉之後 11/12/13/14 "
                      "全部變成不可設定。"),
    MovieSetting("voice_channels", 11, DT.UInt8, "int",
                 note="聲道數（CanSetInfo5: NumOfVoiceChannels）。未驗證 —— "
                      "實測改值不影響錄出來的音訊軌。"),
    MovieSetting("gain_adjust_method", 12, DT.Int8, "int",
                 note="增益調整方式（CanSetInfo5: GainAdjustMethod）。未驗證。"),
    MovieSetting("manual_gain_ev", 13, DT.Int8, "int",
                 note="手動增益 EV（CanSetInfo5: ManualGainAdjustEV）。未驗證 —— "
                      "相機宣告 -128~127，但實測只收 0/1。"),
    MovieSetting("wind_noise_canceller", 14, DT.UInt8, "int",
                 note="風切聲抑制（CanSetInfo5: WindNoiseCanceller）。未驗證 —— "
                      "DataGroupMovie 讀出來沒有 tag 14。"),
    MovieSetting("frame_rate", 61, DT.URational, "rational", "fps",
                 note="⚠ 寫 29.97 相機存成 30、寫 59.94 存成 60，而 23.98 / 25 / 50 "
                      "都正常 —— 儘管相機自己把 29.97 與 59.94 列為合法、"
                      "30 與 60 反而不在清單裡。原因未知。"),
)

MOVIE_BY_NAME = {s.name: s for s in MOVIE_SETTINGS}

#: 相機沒有回報合法值清單、但我們知道值域的設定。
FALLBACK_CHOICES = {"shutter_unit": [1, 2], "capture_mode": [1, 2]}

#: 已經用實機確認過的數值標籤。沒確認的一律不列 —— 猜錯的標籤比沒有標籤更糟，
#: 因為後面的人會相信它。
VALUE_LABELS: dict[str, dict[int, str]] = {
    # 空卡上各錄一段、由人眼確認產物：
    #   record_format=2 -> CINEMA/A001_001_20260801.MOV
    #   record_format=1 -> CinemaDNG 序列（8-bit UHD 23.98fps）
    "record_format": {1: "CinemaDNG", 2: "MOV"},
    # 實測確認：tag 6 設成 1 之後，錄影模式下寫 shutter_speed 精準生效
    # （1/500、1/50、1/125 三個值回讀完全相符）；設成 2 則只有角度可設。
    "shutter_unit": {1: "速度", 2: "角度"},
    # 寫入後機身螢幕真的從錄影切換成拍照，能力清單也整組對調
    "capture_mode": {1: "STILL", 2: "CINE"},
    # 2 = UHD 來自那段 CinemaDNG 的產物；1 = FHD 由使用者檢視錄出來的檔案確認。
    "movie_resolution": {1: "FHD", 2: "UHD"},
}

#: 推測但沒有實測確認的標籤。跟 VALUE_LABELS 分開放，這樣「哪些是證據、
#: 哪些是推論」在程式裡就分得清楚。UI 顯示時會標註未確認。
# 目前沒有待確認的項目。機制保留著 —— record_format 的 mov_image_quality
# 與 movie_resolution 以外的數值仍然沒有名字，將來補上時會先經過這裡。
INFERRED_LABELS: dict[str, dict[int, str]] = {}

#: CanSetInfo5 裡對應的「合法值清單」tag，用來限制可設的範圍。
CAPABILITY_TAGS = {
    # shutter_unit 在 CanSetInfo5 裡沒有對應的能力 tag（106 不存在），
    # 所以沒有合法值清單可以驗證 —— 值域由下面的 CHOICES 補上。
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


def _capability_raw(info5_ifd, name: str) -> list | None:
    """合法值的原始編碼（rational 保留 (分子, 分母)）。

    寫回時原封送回相機自己宣告的那組數字，而不是從浮點數重新推導 ——
    重新推導會引進「我算的分數跟相機講的不同」這個變因。實測相機對
    25 收到 2500/100 存成 25/1、對 50 收到 5000/100 存成 50/1，
    可見它確實會重新詮釋我送的形式。
    """
    tag = CAPABILITY_TAGS.get(name)
    if tag is None or info5_ifd is None:
        return None
    entry = find_tag(info5_ifd, tag)
    if entry is None or not entry.values:
        return None
    return list(entry.values)


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


#: 合法值的原始編碼，key 與 read_capabilities() 相同。
RAW_CAPABILITIES: dict[str, list] = {}


def read_capabilities(cam, info5_raw: bytes | None) -> dict[str, list]:
    """每個錄影設定的合法值清單（來自 CanSetInfo5）。

    同時把原始編碼記進 RAW_CAPABILITIES，寫入時可以原封送回。
    """
    RAW_CAPABILITIES.clear()
    _LAST_CAPABILITIES.clear()
    # 相機沒回報能力時，至少保留我們自己知道值域的那幾個
    caps = dict(FALLBACK_CHOICES)
    if not info5_raw:
        _LAST_CAPABILITIES.update(caps)
        return caps
    try:
        ifd = parse_ifd(info5_raw)
    except Exception:
        _LAST_CAPABILITIES.update(caps)
        return caps
    for name in MOVIE_BY_NAME:
        values = _capability_values(ifd, name)
        if values:
            caps[name] = values
            _LAST_CAPABILITIES[name] = values
            raw = _capability_raw(ifd, name)
            if raw:
                RAW_CAPABILITIES[name] = raw
    _LAST_CAPABILITIES.update(caps)
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
        entries.append((setting.tag, setting.type, _encode_preferring_camera_form(setting, value)))

    write_raw(cam, entries)
    return dict(changes)


def _encode_preferring_camera_form(setting: MovieSetting, value) -> Any:
    """能對上相機宣告的合法值時，原封送回它的原始編碼。

    對不上才用 encode() 自行推導。這樣「我們送出去的就是相機說它接受的」
    是一句真話 —— 之後再排查寫入沒生效時，少一個變因要排除。
    """
    allowed = (capabilities_for(setting.name) or [])
    raw = RAW_CAPABILITIES.get(setting.name) or []
    if len(allowed) == len(raw):
        try:
            wanted = float(value)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for decoded, original in zip(allowed, raw):
                if abs(float(decoded) - wanted) < 1e-9:
                    return original
    return encode(setting, value)


def capabilities_for(name: str) -> list | None:
    """最近一次 read_capabilities() 得到的合法值清單。"""
    return _LAST_CAPABILITIES.get(name)


#: read_capabilities() 最近一次的結果，供 _encode_preferring_camera_form 對照
_LAST_CAPABILITIES: dict[str, list] = {}


#: 探測用途允許的型別。刻意只開這幾個 —— 寫入未知欄位本來就該把
#: 可能造成的影響壓到最小。
PROBE_TYPES = {
    "UInt8": DT.UInt8,
    "Int8": DT.Int8,
    "UInt16": DT.UInt16,
    "Int16": DT.Int16,
    "URational": DT.URational,
}


def write_tag(cam, tag: int, type_name: str, value) -> None:
    """把單一 tag 寫進 DataGroupMovie。純探測用。

    這是為了找出協定裡意義不明的欄位而存在的，不是給一般操作用的 —— 一般
    設定請走 apply_settings()，那裡有合法值檢查。IFD 是稀疏的，所以只有指定
    的 tag 會被送出，其餘欄位不受影響。

    Raises:
        MovieSettingError: 型別不在允許清單內。
    """
    dt = PROBE_TYPES.get(type_name)
    if dt is None:
        raise MovieSettingError(
            f"探測不接受型別 {type_name!r}；可用：{', '.join(sorted(PROBE_TYPES))}")
    if dt in (DT.URational,):
        value = tuple(value)
    else:
        value = int(value)
    write_raw(cam, [(int(tag), dt, value)])


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
            "inferred_labels": INFERRED_LABELS.get(s.name),
        }
        for s in MOVIE_SETTINGS
    ]
