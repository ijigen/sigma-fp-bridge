#!/usr/bin/env python3
"""相機設定的編碼 / 解碼與批次套用。不需要相機。

執行：python3 tests/test_settings.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import camera_settings as CS
except ImportError as e:
    print(f"- 跳過：沒裝 sigma-ptpy（{e}）")
    sys.exit(0)

import fake_camera


def test_apex_roundtrip_matches_real_camera():
    """對照實機 dump 到的原始值：光圈 24 / ISO 80 / 快門 93。"""
    assert CS.decode_value(CS.BY_NAME["aperture"], 24) == 2.0
    assert CS.decode_value(CS.BY_NAME["iso"], 80) == 6400
    assert CS.decode_value(CS.BY_NAME["shutter_speed"], 93) == 1 / 25
    assert CS.encode_value(CS.BY_NAME["aperture"], 2.0) == 24
    assert CS.encode_value(CS.BY_NAME["iso"], 6400) == 80
    assert CS.encode_value(CS.BY_NAME["shutter_speed"], 1 / 25) == 93
    print("✓ APEX 編解碼對得上實機值（f/2.0, ISO 6400, 1/25s）")


def test_enum_accepts_name_and_number():
    from sigma_ptpy import enum as E
    s = CS.BY_NAME["exposure_mode"]
    assert CS.encode_value(s, "Manual") == E.ExposureMode.Manual
    assert CS.encode_value(s, 4) == E.ExposureMode.Manual
    assert CS.decode_value(s, E.ExposureMode.AperturePriority) == "AperturePriority"
    print("✓ enum 接受名稱與數值")


def test_bad_values_rejected_with_useful_message():
    try:
        CS.encode_value(CS.BY_NAME["exposure_mode"], "Bulb")
    except CS.SettingError as e:
        assert "Manual" in str(e), e  # 錯誤訊息要列出可用值
        print("✓ 不合法的 enum 值被擋下並列出可用值")
    else:
        raise AssertionError("應該要丟 SettingError")


def test_broken_upstream_aperture_code_rejected():
    """sigma-ptpy 的 Aperture3Converter 把 (101, 57) 打成 (191, 57)。

    191 超出光圈碼範圍，直接送出去相機看不懂。寧可明確拒絕也不要送垃圾。
    """
    try:
        CS.encode_value(CS.BY_NAME["aperture"], 57)
    except CS.SettingError as e:
        assert "191" in str(e), e
        print("✓ 擋掉 sigma-ptpy 換算表裡的壞光圈碼")
    else:
        raise AssertionError("f/57 應該被擋下")


def test_apply_groups_writes_by_datagroup():
    """同一個 DataGroup 的欄位要合併成一次寫入。"""
    cam = fake_camera.FakeCamera()
    CS.apply_settings(cam, {
        "aperture": 2.8,        # group 1
        "shutter_speed": 1/125,  # group 1
        "exposure_mode": "Manual",  # group 2
        "color_temp": 5600,      # group 5
    })
    groups_written = [n for n, _ in cam.set_group_log]
    assert sorted(groups_written) == [1, 2, 5], cam.set_group_log
    assert len(groups_written) == 3, "同組欄位應該合併成一次寫入"
    assert cam.groups[1]["Aperture"] == 32
    assert cam.groups[1]["ShutterSpeed"] == 112
    print(f"✓ 4 筆變更 → 3 次 USB 寫入（依 DataGroup 分組）")


def test_invalid_change_aborts_whole_batch():
    """任何一筆不合法就整批不送 —— 免得一半成功一半失敗。"""
    cam = fake_camera.FakeCamera()
    try:
        CS.apply_settings(cam, {"aperture": 2.8, "exposure_mode": "NotAMode"})
    except CS.SettingError:
        assert cam.set_group_log == [], f"不該有任何寫入：{cam.set_group_log}"
        print("✓ 一筆不合法就整批中止，相機完全沒被寫入")
    else:
        raise AssertionError("應該要丟 SettingError")


def test_unknown_setting_name_rejected():
    cam = fake_camera.FakeCamera()
    try:
        CS.apply_settings(cam, {"shutter_angle": 180})
    except CS.SettingError as e:
        assert "shutter_angle" in str(e)
        assert cam.set_group_log == []
        print("✓ 不認得的設定名稱被擋下")
    else:
        raise AssertionError("應該要丟 SettingError")


def test_read_settings_returns_human_values():
    cam = fake_camera.FakeCamera()
    got = CS.read_settings(cam)
    assert got["aperture"] == 2.0, got
    assert got["iso"] == 6400, got
    assert got["exposure_mode"] == "Manual", got
    assert got["color_temp"] == 5600, got
    print("✓ read_settings 回傳人看得懂的值")


def test_describe_gives_ui_choices():
    schema = {d["name"]: d for d in CS.describe()}
    assert "Manual" in schema["exposure_mode"]["choices"]
    assert 2.8 in schema["aperture"]["choices"]
    assert schema["color_temp"]["kind"] == "int"
    print(f"✓ describe() 提供 {len(schema)} 項設定的 UI 選項")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_settings 全部通過")
