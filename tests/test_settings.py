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


def test_capabilities_reject_out_of_range_iso():
    """實機 ISO 只到 25600，但 APEX 換算表到 102400。

    不擋的話，UI 會列出 51200 / 102400，使用者選了送出去被相機默默拒絕，
    畫面上只看到「設了沒反應」。這正是實測踩到的狀況。
    """
    cam = fake_camera.FakeCamera()
    caps = {"iso": {"min": 100, "max": 25600}}
    for bad in (51200, 102400, 50, 6):
        try:
            CS.apply_settings(cam, {"iso": bad}, caps)
        except CS.SettingError as e:
            assert "25600" in str(e), e
        else:
            raise AssertionError(f"ISO {bad} 應該被擋下")
    assert cam.set_group_log == [], "被擋下的值不該送到相機"
    CS.apply_settings(cam, {"iso": 6400}, caps)   # 範圍內要放行
    assert cam.groups[1]["ISOSpeed"] == 80
    print("✓ 超出相機 ISO 範圍的值被擋下，範圍內正常放行")


def test_describe_filters_choices_by_capabilities():
    caps = {"iso": {"min": 100, "max": 25600},
            "exposure_compensation": {"min": -5.0, "max": 5.0}}
    schema = {d["name"]: d for d in CS.describe(caps)}
    iso = schema["iso"]["choices"]
    assert min(iso) >= 100 and max(iso) <= 25600, (min(iso), max(iso))
    assert 102400 not in iso and 6 not in iso
    ec = schema["exposure_compensation"]["choices"]
    assert min(ec) >= -5.0 and max(ec) <= 5.0
    unfiltered = {d["name"]: d for d in CS.describe()}
    assert 102400 in unfiltered["iso"]["choices"], "沒給 capabilities 時不該過濾"
    print(f"✓ UI 選項依相機能力過濾（ISO {len(iso)} 項，未過濾為 {len(unfiltered['iso']['choices'])} 項）")


def test_shutter_angle_conversion():
    """180° 表示曝光佔一個影格的一半，換到任何幀率都該保持這個關係。"""
    assert CS.angle_to_seconds(180, 24) == 1 / 48
    assert CS.angle_to_seconds(360, 24) == 1 / 24
    assert CS.seconds_to_angle(1 / 48, 24) == 180.0
    assert CS.seconds_to_angle(1 / 50, 25) == 180.0
    print("✓ 快門角度 ↔ 秒數換算")


def test_shutter_angle_reports_what_it_actually_got():
    """快門是離散的，180° @ 24fps 做不出來（1/48 不在 1/3 級表裡）。

    這種時候必須回報真正拿到的角度，不能假裝設成了 180 —— 使用者要能
    決定是否接受這個誤差。
    """
    angle, seconds = CS.nearest_shutter_angle(180, 24)
    assert seconds == 1 / 50, seconds
    assert angle == 172.8, angle
    # 25fps 剛好對得上
    angle25, seconds25 = CS.nearest_shutter_angle(180, 25)
    assert seconds25 == 1 / 50 and angle25 == 180.0
    print(f"✓ 誠實回報實際角度（180°@24fps → {angle}°，180°@25fps → {angle25}°）")


def test_shutter_angle_applies_through_shutter_speed():
    cam = fake_camera.FakeCamera()
    applied = CS.apply_settings(cam, {"shutter_angle": 180}, frame_rate=25)
    assert applied["shutter_angle"] == 180.0, applied
    assert cam.groups[1]["ShutterSpeed"] == CS.SHUTTER.encode_uint8(1 / 50)
    got = CS.read_settings(cam, frame_rate=25)
    assert got["shutter_angle"] == 180.0, got
    print("✓ 快門角度經由 shutter_speed 欄位套用並讀回")


def test_shutter_angle_needs_frame_rate():
    cam = fake_camera.FakeCamera()
    try:
        CS.apply_settings(cam, {"shutter_angle": 180})
    except CS.SettingError as e:
        assert "needs a frame rate" in str(e)
        assert cam.set_group_log == []
        print("✓ 沒有幀率時拒絕設定快門角度")
    else:
        raise AssertionError("應該要丟 SettingError")


def test_shutter_angle_and_speed_conflict_rejected():
    """兩者是同一件事，同時給會互相矛盾。"""
    cam = fake_camera.FakeCamera()
    try:
        CS.apply_settings(cam, {"shutter_angle": 180, "shutter_speed": 1 / 100},
                          frame_rate=24)
    except CS.SettingError as e:
        assert "same setting" in str(e)
        assert cam.set_group_log == []
        print("✓ 同時指定角度與秒數被擋下")
    else:
        raise AssertionError("應該要丟 SettingError")


def test_setting_iso_turns_off_iso_auto():
    """實測發現的：ISO Auto 開著時寫 ISO 完全沒作用。

    所以設 iso 必須同時關掉 iso_auto —— 跟 set_focus_position() 得在同一次
    寫入裡關掉 AFLock / PreConstAF 是同一個道理。兩者同屬 DataGroup1，
    會併成一次寫入。
    """
    from sigma_ptpy import enum as E
    cam = fake_camera.FakeCamera()
    cam.groups[1]["ISOAuto"] = E.ISOAuto.Auto
    CS.apply_settings(cam, {"iso": 800})
    assert cam.groups[1]["ISOAuto"] == E.ISOAuto.Manual, "沒有自動關掉 ISO Auto"
    assert cam.groups[1]["ISOSpeed"] == CS.ISO.encode_uint8(800)
    assert len(cam.set_group_log) == 1, "應該只有一次寫入（同一個 DataGroup）"
    print("✓ 設 ISO 會一併關掉 ISO Auto（單次寫入）")


def test_explicit_iso_auto_is_respected():
    """使用者明講要 Auto 就不要雞婆覆蓋。"""
    from sigma_ptpy import enum as E
    cam = fake_camera.FakeCamera()
    CS.apply_settings(cam, {"iso": 800, "iso_auto": "Auto"})
    assert cam.groups[1]["ISOAuto"] == E.ISOAuto.Auto
    print("✓ 明確指定 iso_auto 時不覆蓋")


def test_mode_specific_settings_rejected_with_alternative():
    """CINE 模式下寫 shutter_speed 相機會默默丟掉 —— 直接擋下並指路。"""
    cam = fake_camera.FakeCamera()
    try:
        CS.apply_settings(cam, {"shutter_speed": 1 / 500}, mode="movie")
    except CS.SettingError as e:
        assert "shutter_angle" in str(e), e
        assert cam.set_group_log == [], "被擋下的值不該送到相機"
        print("✓ 錄影模式下擋掉 shutter_speed 並指向 shutter_angle")
    else:
        raise AssertionError("應該要丟 SettingError")


def test_mode_none_does_not_gate():
    """還不知道模式時不要亂擋 —— 寧可讓相機自己判斷。"""
    cam = fake_camera.FakeCamera()
    CS.apply_settings(cam, {"shutter_speed": 1 / 500}, mode=None)
    assert cam.set_group_log, "模式未知時不該擋"
    print("✓ 模式未知時不做限制")


def test_describe_hides_settings_for_other_mode():
    stills = {d["name"] for d in CS.describe(mode="stills")}
    movie = {d["name"] for d in CS.describe(mode="movie")}
    assert "shutter_speed" in stills and "shutter_speed" not in movie
    assert "image_quality" in stills and "image_quality" not in movie
    assert "aperture" in stills and "aperture" in movie, "光圈兩邊都適用"
    print(f"✓ describe 依模式過濾（拍照 {len(stills)} 項 / 錄影 {len(movie)} 項）")


def test_readback_tolerance_is_tight_enough_for_frame_rates():
    """回讀比對的容差只該吸收浮點誤差，不該容忍相機給了別的值。

    實測踩過：寫 29.97 相機存成 30，差 0.1%，被 2% 的容差判定為相等，
    於是「寫入沒生效」完全不會被報出來 —— 而 29.97 與 30 在影片裡
    是 drop-frame 與否的差別。
    """
    assert not CS._roughly_equal(29.97, 30.0), "29.97 與 30 不該被當成相等"
    assert not CS._roughly_equal(59.94, 60.0)
    assert CS._roughly_equal(1 / 50, 0.02), "浮點表示誤差仍要吸收"
    assert CS._roughly_equal(6400, 6400)
    print("✓ 回讀容差夠緊，抓得出 29.97 被存成 30")


def test_stills_only_settings_are_marked():
    """實測在錄影模式下寫入無效的設定要標成 stills。"""
    for name in ("aspect_ratio", "color_space", "tone_effect"):
        assert CS.BY_NAME[name].applies_to == "stills", name
        assert "錄影模式下寫入無效" in CS.BY_NAME[name].note, name
    movie = {d["name"] for d in CS.describe(mode="movie")}
    for name in ("aspect_ratio", "color_space", "tone_effect"):
        assert name not in movie, f"{name} 不該出現在錄影模式"
    print("✓ 錄影模式下無效的拍照設定已標記並排除")


def test_shutter_speed_allowed_when_camera_is_in_speed_mode():
    """錄影模式下 shutter_speed 原本被擋，但相機切到速度模式時它就是有效的。

    模式閘門要能接受這個例外，否則使用者切了單位卻還是不能設快門。
    """
    cam = fake_camera.FakeCamera()
    try:
        CS.apply_settings(cam, {"shutter_speed": 1 / 500}, mode="movie")
    except CS.SettingError:
        pass
    else:
        raise AssertionError("角度模式下應該擋下")

    CS.apply_settings(cam, {"shutter_speed": 1 / 500}, mode="movie",
                      also_allowed={"shutter_speed"})
    assert cam.groups[1]["ShutterSpeed"] == CS.SHUTTER.encode_uint8(1 / 500)

    names = {d["name"] for d in CS.describe(mode="movie", also_allowed={"shutter_speed"})}
    assert "shutter_speed" in names, "速度模式下 schema 要列出 shutter_speed"
    print("✓ 相機切到速度模式時 shutter_speed 解除封鎖")


def test_newly_exposed_settings_round_trip():
    """DataGroup4/5 裡一直沒被接出來的設定。

    刻意不做能力驗證 —— CanSetInfo5 對這些欄位的編碼形狀不一致（EImageStab
    宣告 [0,1] 但實際值是 Off(2)），拿去當可選值清單會擋掉合法設定。靠寫入
    後讀回比對就好。
    """
    import sigma_ptpy.enum as E
    cam = fake_camera.FakeCamera()
    cases = {
        "electronic_stabilization": E.EImageStab.On,
        "dc_crop_mode": E.DCCropMode.On,
        "loc_distortion": E.LOCDistortion.Off,
        "loc_vignetting": E.LOCVignetting.Auto,
        "interval_timer_seconds": 30,
        "interval_timer_frames": 5,
    }
    for name, value in cases.items():
        setting = CS.BY_NAME[name]
        raw = value.value if hasattr(value, "value") else value
        cam.groups[setting.group][setting.field] = value
        got = CS.read_settings(cam).get(name)
        expected = value.name if hasattr(value, "name") else value
        assert got == expected, f"{name}: 讀回 {got!r}，預期 {expected!r}"
    print(f"✓ 新接出的 {len(cases)} 個設定讀得回來")


def test_read_only_status_is_not_writable():
    """放大倍率與間隔計時器剩餘量是相機自己算的，不能出現在可寫設定裡。"""
    names = {s.name for s in CS.SETTINGS}
    for name in ("lv_magnify_ratio", "interval_seconds_remain", "interval_frames_remain"):
        assert name not in names, f"{name} 不該是可寫設定"
        assert any(n == name for n, _, _ in CS.STATUS_FIELDS), \
            f"{name} 應該在唯讀狀態裡"
    print("✓ 唯讀狀態沒有混進可寫設定")


def test_capabilities_values_are_all_dicts():
    """read_capabilities 的契約：每個值都是帶 min/max 的 dict。

    實機炸過：我把對焦點的座標陣列（CanSetInfo5 612 / 613）也塞進這個字典，
    於是 bridge 裡 `v.get('min')` 那行對著 list 呼叫 .get，整個狀態讀取失敗，
    log 只留下一句 'list' object has no attribute 'get' —— 錯誤出現的地方
    離成因很遠。混了兩種值型別的字典就是這樣。

    直接替換 read_info5_raw，讓真實的 read_capabilities 跑在已知 payload 上；
    用假相機是測不到的（那條路徑靠 decode 時的側錄 patch）。
    """
    import sigma_fp_focus as F
    payload = fake_camera._sample_ifd([
        (215, 3, [1280, 3328, 256, 85]),     # ISO 範圍
        (612, 3, [682, 1024]),               # 對焦點整體區域
        (613, 3, [85, 597, 96, 928]),        # 對焦點有效區域
        (658, 3, [5974, 11116]),             # 焦點位置範圍
    ])
    original = F.read_info5_raw
    F.read_info5_raw = lambda cam: payload
    try:
        caps = F.read_capabilities(object())
    finally:
        F.read_info5_raw = original

    assert caps, "什麼都沒解出來，這個測試就是空轉的"
    bad = {k: type(v).__name__ for k, v in caps.items() if not isinstance(v, dict)}
    assert not bad, f"這些能力值不是 dict：{bad}"
    # bridge 會對每個值做 v.get('min')，這裡照做一次確認不會炸
    for name, limits in caps.items():
        limits.get("min"), limits.get("max")
    print(f"✓ {len(caps)} 個能力值都是 dict，v.get('min') 不會炸")


def test_the_cansetinfo5_fallback_notice_prints_once():
    """CINE 模式下每次讀設定都會撞到同一個 IndexError。

    備援路徑有效，訊息也正確，但每輪刷七八行相同內容會把真正的問題蓋掉 ——
    這次那個 'list' object has no attribute 'get' 就是埋在裡面。
    """
    import io
    import contextlib
    import sigma_fp_focus as F

    class Boom:
        def get_cam_can_set_info5(self):
            F._LAST_RAW["CanSetInfo5"] = b"\x08\x00\x00\x00\x00\x00\x00\x00"
            raise IndexError("list index out of range")

    F._CANSETINFO5_WARNED = False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        for _ in range(5):
            try:
                F.read_info5_raw(Boom())
            except Exception:
                pass
    hits = err.getvalue().count("sigma-ptpy 解 CanSetInfo5 失敗")
    assert hits == 1, f"提示印了 {hits} 次，應該只印一次"
    print("✓ CanSetInfo5 備援提示只印一次")


def test_a_declared_list_that_excludes_the_current_value_is_not_a_value_list():
    """實機炸過：DC 裁切的選項變成「關 / 自動 / -1」。

    CanSetInfo5 500 給 [1, 0, -1]，而 -1 不是合法的 enum 值；LOCVignetting
    給 [0, -1] 但目前生效的是 Off(2)，根本不在裡面。那些 tag 放的不是可選值。

    我先前就記下「這些欄位的編碼形狀不一致」，然後還是把它們當值列表用了。
    所以現在有兩道機械化的防線，而不是靠記得：清單不能有負數，而且目前
    生效的值必須在裡面。
    """
    import sigma_ptpy.enum as E
    # 只違反「不能有負數」這一條 —— 目前值確實在清單裡，所以另一道防線
    # 不會攔它。兩條要分開測，不然拿掉一條也不會有人發現。
    cur = E.DriveMode.SingleCapture
    entries = {e["name"]: e for e in CS.describe(
        choice_values={"drive_mode": [cur.value, -1]},
        current_values={"drive_mode": cur.name})}
    assert entries["drive_mode"]["choices"] == [m.name for m in E.DriveMode], \
        "含負數的清單被當成可選值了"

    # 目前值不在宣告清單裡 —— 那份清單不是值列表
    entries = {e["name"]: e for e in CS.describe(
        choice_values={"metering_mode": [1, 2]},
        current_values={"metering_mode": "Average"})}
    got = entries["metering_mode"]["choices"]
    assert got == [m.name for m in E.AEMeteringMode], f"沒有退回 enum：{got}"
    print("✓ 不是值列表的宣告會被擋下，退回 enum")


def test_unknown_but_declared_values_can_be_written():
    """畫得出來就要按得下去。

    色彩模式 13~16 是相機宣告、sigma-ptpy 不認得的。UI 把它們顯示成數字，
    而寫入路徑原本只收 enum 名稱 —— 於是選項看得到、點下去卻是 SettingError。
    """
    s = CS.BY_NAME["color_mode"]
    assert CS.encode_value(s, "Standard").name == "Standard"
    assert CS.encode_value(s, "13") == 13, "數字字串收不下"
    assert CS.encode_value(s, 13) == 13, "整數收不下"
    try:
        CS.encode_value(s, "nope")
    except CS.SettingError:
        pass
    else:
        raise AssertionError("亂寫的名稱應該要被擋")
    print("✓ enum 不認得但相機宣告的值寫得進去")


def test_choices_follow_what_the_camera_declares():
    """選項要以相機宣告的為準，enum 只負責提供名稱。

    使用者回報色彩模式少了一個 Off。查下去是這台相機宣告 16 個色彩模式，
    而 sigma-ptpy 的 enum 只認得 12 個 —— UI 的選項全部來自 enum，所以那四
    個看不到也選不了。函式庫跟不上韌體是常態，以相機說的為準才不會漏。

    那 4 個值後來實測認出來了（見 FpColorMode），但這條規則不能因此鬆掉：
    下一次韌體再加一個，一樣要以相機宣告的為準。
    """
    declared = [15, 14, 13, 12, 8, 7, 6, 10, 9, 5, 4, 3, 2, 1, 11, 16]
    entries = {e["name"]: e for e in
               CS.describe(choice_values={"color_mode": declared},
                           current_values={"color_mode": "Standard"})}
    choices = entries["color_mode"]["choices"]
    assert len(choices) == len(declared), f"{len(choices)} 個選項，相機說有 {len(declared)}"
    assert "Cinema" in choices and "TealAndOrange" in choices, choices

    # 相機宣告一個連自訂 enum 也不認得的值時，仍要顯示成數字讓人選得到 ——
    # 不認得不代表不能用。
    plus = {e["name"]: e for e in
            CS.describe(choice_values={"color_mode": declared + [17]},
                        current_values={"color_mode": "Standard"})}["color_mode"]["choices"]
    assert "17" in plus, f"未知的 17 沒出現在選項裡：{plus}"

    # 沒有相機資料、或驗證不了目前值時退回 enum，不能整個變空
    fallback = {e["name"]: e for e in CS.describe()}["color_mode"]["choices"]
    assert fallback == [m.name for m in CS.FpColorMode], fallback
    unverifiable = {e["name"]: e for e in
                    CS.describe(choice_values={"color_mode": declared})}["color_mode"]["choices"]
    assert unverifiable == [m.name for m in CS.FpColorMode], "驗不了目前值卻照用了"
    print(f"✓ 選項跟著相機走（{len(choices)} 個，未知值顯示成數字）")


def test_an_empty_capability_list_marks_the_setting_unsettable():
    # 相機在 CinemaDNG 下把 EImageStab（tag 810）的清單清空 —— fp 的電子
    # 防手震不支援 CinemaDNG。清單的內容編碼不可信，但空不空是可信的。
    by = {d["name"]: d for d in CS.describe(
        choice_values={"electronic_stabilization": [], "dc_crop_mode": [1, 0, -1]})}
    assert by["electronic_stabilization"]["writable"] is False
    assert "不開放" in by["electronic_stabilization"]["note"]
    # 有內容的照舊可設 —— 否則「全部反灰」的實作也會讓這個測試過。
    assert by["dc_crop_mode"]["writable"] is True
    print("✓ 空的能力清單 = 相機宣告當下不可設定")


def test_a_missing_capability_entry_leaves_the_setting_settable():
    # 「相機沒提到這個 tag」跟「提到但是空的」是兩回事。混為一談的話，
    # CanSetInfo5 讀不到時整個面板都會變成唯讀。
    by = {d["name"]: d for d in CS.describe(choice_values={})}
    assert by["electronic_stabilization"]["writable"] is True
    print("✓ 沒宣告 ≠ 不可設定")


def test_fp_colour_modes_do_not_come_from_the_sd_quattro_table():
    """sigma-ptpy 的 ColorMode 是 SD Quattro 時代的表：12 個值，第一個叫
    Sepia。fp 沒有 Sepia 模式（那是 Monochrome 底下的色調），官方清單是
    16 個，跟相機宣告的 16 個值數量相符。

    值 1 和 13–16 是實測定的：每個值寫進去、抓一張即時預覽影格回來看。
    """
    import sigma_ptpy.enum as E
    assert len(CS.FpColorMode) == 16, "fp 有 16 個色彩模式"
    assert CS.FpColorMode(1).name == "WarmGold", "值 1 在 fp 上不是 Sepia"
    assert not hasattr(CS.FpColorMode, "Sepia"), "fp 沒有 Sepia 模式"
    for name in ("TealAndOrange", "Off", "PowderBlue", "Duotone"):
        assert hasattr(CS.FpColorMode, name), f"少了 {name}"
    assert CS.BY_NAME["color_mode"].enum_cls is CS.FpColorMode, \
        "色彩模式又接回 sigma-ptpy 的表了"

    # 解碼要以我們的表為準。sigma-ptpy 解出來的 member 會帶著它自己的名字，
    # 先看 raw.name 的話值 1 會顯示成 Sepia。
    got = CS.decode_value(CS.BY_NAME["color_mode"], E.ColorMode(1))
    assert got == "WarmGold", f"解碼被 sigma-ptpy 的名字蓋掉了：{got}"
    print("✓ 色彩模式用 fp 自己的 16 個值，不是 SD Quattro 的表")


def test_a_setting_written_as_a_number_is_not_reported_as_rejected():
    """回讀的是名稱，寫進去的是數字 —— 不換算成同一種表示法就永遠不相等。

    實測：16 個色彩模式全部誤報成「相機沒有接受」，但相機其實吃了，畫面
    上的效果也變了。使用者看到的就是「顏色明明變了，log 卻說沒接受」。
    """
    cam = fake_camera.FakeCamera()
    cam.groups[3]["ColorMode"] = 13          # 相機現在就是這個值
    rejected = CS.verify_applied(cam, {"color_mode": 13})
    assert "color_mode" not in rejected, f"寫成功卻被報成拒絕：{rejected}"

    # 名稱寫入一樣不能誤報
    assert "color_mode" not in CS.verify_applied(cam, {"color_mode": "TealAndOrange"})

    # 真的沒吃還是要報出來 —— 否則「全部都算相符」也會通過這條測試
    cam.groups[3]["ColorMode"] = 3
    assert "color_mode" in CS.verify_applied(cam, {"color_mode": 13}), \
        "相機沒吃卻沒報出來"
    print("✓ 數字寫入 / 名稱回讀會換算後再比對")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_settings 全部通過")
