#!/usr/bin/env python3
"""瀏覽器 UI 的靜態檢查。不需要相機，也不需要瀏覽器。

UI 壞掉通常要等到打開網頁才會發現，而這個專案的 UI 是操作相機的唯一介面。
這裡至少把「語法壞了」「呼叫了不存在的函式」「操作不存在的元素」擋下來。

執行：python3 tests/test_ui.py
"""
import re
import sys
from pathlib import Path

HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _script(html: str) -> str:
    return re.search(r"<script>(.*?)</script>", html, re.S).group(1)



def _run_js(html, fn_pattern, body):
    """把一個函式從網頁裡挖出來，用 macOS 內建的 JavaScriptCore 跑。

    回傳字串結果；沒有引擎（非 macOS）時回 None 讓呼叫端跳過。
    """
    import subprocess
    m = re.search("^" + fn_pattern, html, re.S | re.M)
    assert m, f"找不到符合 {fn_pattern} 的函式"
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", m.group(0) + body],
                           capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    assert r.returncode == 0, f"JS 執行失敗：{r.stderr.strip()[:200]}"
    return r.stdout.strip()

def test_javascript_parses():
    try:
        import esprima
    except ImportError:
        print("- 跳過語法檢查（pip install esprima 可啟用）")
        return
    esprima.parseScript(_script(HTML.read_text()))
    print("✓ JavaScript 語法正確")


def test_every_handler_is_defined():
    """HTML 的 onclick 呼叫的函式必須存在，否則按了沒反應。"""
    html = HTML.read_text()
    js = _script(html)
    called = set(re.findall(r"on\w+=\"(\w+)\(", html))
    defined = set(re.findall(r"(?:async\s+)?function\s+(\w+)", js))
    missing = called - defined
    assert not missing, f"HTML 呼叫了未定義的函式：{missing}"
    print(f"✓ {len(called)} 個事件處理函式都有定義")


def test_every_element_id_exists():
    """JS 取用的 id 必須真的在 HTML 裡，否則會 null pointer。"""
    html = HTML.read_text()
    js = _script(html)
    ids = set(re.findall(r"id=[\"\'](\w[\w-]*)[\"\']", html))
    used = set(re.findall(r"\$\(['\"]([\w-]+)['\"]\)", js))
    missing = used - ids
    assert not missing, f"JS 取用了不存在的元素 id：{missing}"
    print(f"✓ JS 取用的 {len(used)} 個元素 id 都存在")


def test_body_mode_is_settable_when_the_camera_reports_it():
    """機身模式實測可寫（DataGroupMovie tag 1），寫入後螢幕真的切換。

    先前這裡斷言它必須是唯讀 —— 那是根據我對 SDK 那句「與撥桿狀態同步」
    的誤讀。相機沒回報時才退回唯讀指示。
    """
    js = _script(HTML.read_text())
    assert "byName.capture_mode" in js, "沒有用 capture_mode 當可寫控制"
    assert "'capture_mode', 'Shooting Mode'" in js
    # 相機沒回報時仍要有唯讀的後備
    assert "'__mode'" in js and "locked: true" in js, "缺少唯讀後備"
    assert "if (b.dataset.set === '__mode') return;" in js, "後備的按鈕沒擋掉送出"
    print("✓ 機身模式可切換，相機未回報時退回唯讀指示")


def test_manual_only_settings_are_marked():
    """P/A/S 模式下寫快門或光圈會被自動曝光覆蓋，按鈕要先停用並說明。"""
    js = _script(HTML.read_text())
    assert "NEEDS_MANUAL" in js
    for name in ("shutter_speed", "shutter_angle", "aperture"):
        assert name in js[js.index("NEEDS_MANUAL"):js.index("NEEDS_MANUAL") + 200], name
    assert "'needs M mode'" in js
    print("✓ 只有 M 模式可設的項目會停用並說明")


def test_inferred_labels_are_marked_uncertain():
    """推測而非實測的標籤要標記，不能跟確認過的混為一談。"""
    js = _script(HTML.read_text())
    assert "inferred_labels" in js and "+ '?'" in js, "推測標籤沒有標上問號"
    print("✓ 推測的標籤會標記為未確認")


def test_iso_sits_inside_the_exposure_row_after_the_mode():
    """ISO 與 EV 併進曝光那一列，緊接在模式後面。

    模式決定相機接不接受手動 ISO，兩者相鄰才看得出關係。數值用下拉是因為
    ISO 有 25 個合法值，橫排按鈕會佔掉整列；自動時停用，選了也會被覆蓋。
    """
    js = _script(HTML.read_text())
    block = js[js.index("function exposureRow"):js.index("function wbRow")]
    assert "iso_auto" in block and "data-set=\"iso\"" in block, "ISO 沒併進曝光列"
    assert 'data-set="exposure_compensation"' in block, "EV 沒併進曝光列"
    assert "lockedAttr(autoIso)" in block, "自動時沒有停用 ISO 數值"
    # ISO 群組要排在模式群組後面、光圈之前
    assert block.index('data-set="exposure_mode"') < block.index('data-set="iso_auto"')
    assert block.index('data-set="iso_auto"') < block.index('data-set="aperture"')
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    for t in ("'iso'", "'iso_auto'", "'exposure_compensation'", "'__iso'"):
        assert t not in body, f"{t} 不該還在一般的 ORDER 迴圈裡"
    assert "select[data-set]" in js, "select 沒有綁事件"
    print("✓ ISO / EV 併入曝光列，緊接模式之後")


def test_exposure_row_merges_mode_aperture_and_shutter():
    """曝光模式決定光圈與快門能不能手動設，三者放同一列才看得出關係。

    P 下兩個都由相機接管、A 只放光圈、S 只放快門、M 才全放。
    CINE 模式沒有 shutter_speed，所以「秒」要停用並說明。
    """
    js = _script(HTML.read_text())
    assert "function exposureRow" in js and "'__exposure'" in js
    block = js[js.index("function exposureRow"):js.index("function wbRow")]
    for token in ("exposure_mode", "aperture", "shutter_speed", "shutter_angle"):
        assert token in block, token
    assert "apOK" in block and "shOK" in block, "沒有依模式決定可不可設"
    assert "in CINE the shutter is set by angle only" in block
    assert "the camera decides" in block, "被相機接管的項目沒有說明"
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    for token in ("'exposure_mode'", "'aperture'", "'shutter_speed'", "'shutter_angle'"):
        assert token not in body, f"{token} 不該還在一般的 ORDER 迴圈裡"
    print("✓ 曝光模式 / 光圈 / 快門合併成一列，依模式決定可設性")


def test_exposure_row_comes_after_format():
    """先決定要拍什麼規格，再決定怎麼曝光。"""
    js = _script(HTML.read_text())
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    assert body.index("'__format'") < body.index("'__exposure'")
    print("✓ 曝光列排在影像規格之後")


def test_derived_shutter_angle_not_offered_in_movie_mode():
    """錄影模式有真正的 DataGroupMovie tag 7，衍生的那個會打架。"""
    src = (Path(__file__).resolve().parent.parent / "camera_settings.py").read_text()
    assert 'if mode == "movie":' in src and "return out" in src
    print("✓ 錄影模式不列衍生的快門角度")


def test_exposure_mode_shows_only_pasm():
    """相機回報 10 個曝光模式，但實際會用的就 P/A/S/M。

    目前值若不在名單內仍要列出，否則使用者看不到自己現在在哪一檔。
    """
    js = _script(HTML.read_text())
    assert "CHOICE_WHITELIST" in js
    block = js[js.index("CHOICE_WHITELIST"):js.index("NEEDS_MANUAL")]
    for m in ("ProgramAuto", "AperturePriority", "ShutterPriority", "Manual"):
        assert m in block, m
    assert "C1" not in block and "Star" not in block
    assert "String(v) === String(cur)" in js, "目前值不在名單時沒有保留"
    print("✓ 曝光模式只列 P/A/S/M（目前值不在名單時仍保留）")


def test_shutter_row_never_disappears():
    """快門列在四種組合下都必須有東西可顯示。

    實際壞過：visibleSettings() 留著「單位不是角度就濾掉 shutter_angle」的
    舊邏輯，而 CINE 模式沒有 shutter_speed，於是預設單位下兩個都被濾掉，
    整列憑空消失。合併成一列之後，該顯示哪一個是 shutterRow 的職責。
    """
    js = _script(HTML.read_text())
    body = js[js.index("function visibleSettings"):js.index("function labelFor")]
    assert "shutter_angle" not in body and "shutter_speed" not in body, \
        "visibleSettings() 不該再過濾快門"

    for mode, available in [("CINE", {"shutter_angle"}),
                            ("STILL", {"shutter_speed", "shutter_angle"})]:
        for unit in ("time", "angle"):
            has_speed = "shutter_speed" in available
            use_angle = unit == "angle" or not has_speed
            active = "shutter_angle" if use_angle else "shutter_speed"
            assert active in available, f"{mode} + {unit} 會顯示不存在的 {active}"
    print("✓ 快門列在 CINE / STILL × 秒 / 角度 都有內容")


def test_format_size_and_depth_share_a_row():
    """規格 / 大小 / 色彩位元是同一個決策的三個面向，而且相機讓它們互相牽動。

    分成三列看不出這層關係 —— 改規格會改變另外兩個可選什麼。
    """
    js = _script(HTML.read_text())
    assert "function formatRow" in js and "'__format'" in js
    for name in ("record_format", "image_quality", "movie_resolution",
                 "resolution", "cinema_dng_quality", "dng_quality"):
        assert name in js[js.index("function formatRow"):js.index("function settingsHTML")], name
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    # 用帶引號的形式比對：mov_image_quality 是刻意留著的獨立列，
    # 而它的名字包含 image_quality，裸比對會誤判。
    for name in ("record_format", "image_quality", "movie_resolution",
                 "cinema_dng_quality", "dng_quality"):
        assert f"'{name}'" not in body, f"{name} 不該還在一般的 ORDER 迴圈裡"
    assert "frame_rate" in js[js.index("function formatRow"):js.index("function settingsHTML")], \
        "幀率沒有併進同一列"
    assert "'frame_rate'" not in body, "frame_rate 不該還在一般的 ORDER 迴圈裡"
    assert ".grp + .grp" in HTML.read_text(), "群組之間沒有分隔線"
    print("✓ 規格 / 大小 / 位元 / 幀率合併成一列，以分隔線區分")


def test_locked_option_groups_do_not_vanish():
    """相機在某些組合下會鎖死某個項目 —— 實測 UHD 下不開放調整色彩位元，
    合法值清單是空的。

    整組消失會讓人以為功能壞掉（實際被回報過）。要改成顯示目前值並標明鎖定。
    """
    js = _script(HTML.read_text())
    block = js[js.index("function formatRow"):js.index("function settingsHTML")]
    assert ".filter(Boolean);" in block, "沒有可選值的群組仍被濾掉"
    assert "locked = true" in block, "空清單沒有標成鎖定"
    assert "does not offer this at the current format / size" in block
    assert "greyed out = locked at this combination" in js
    print("✓ 無可選值的群組改為鎖定顯示，不會消失")


def test_unidentified_values_say_so():
    """有些設定我們知道相機接受哪些值，卻不知道值代表什麼
    （例如 mov_image_quality 的 1 / 2）。

    讓使用者對著沒有意義的數字猜，跟控制項壞掉沒兩樣 —— 要直說未確認。
    """
    js = _script(HTML.read_text())
    assert "meaning of these values not confirmed" in js
    assert "const opaque" in js and "!s.labels" in js
    print("✓ 未確認意義的數值會標明")


def test_unsettable_unidentified_setting_is_hidden():
    """mov_image_quality 在所有測過的組合下都不可調，值的意義也沒有證據。

    留在畫面上就是一個按不動又看不懂的控制項。協定層的對應保留，
    API 仍可讀寫 —— 隱藏的只是 UI。
    """
    js = _script(HTML.read_text())
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    assert "'mov_image_quality'" not in body, "mov_image_quality 不該列在 UI"
    src = (Path(__file__).resolve().parent.parent / "movie_settings.py").read_text()
    assert '"mov_image_quality"' in src, "協定層的對應不該一起拿掉"
    print("✓ 不可調且未識別的設定不列在 UI，但協定對應保留")


def test_white_balance_and_colour_temp_share_a_row():
    """色溫只有在白平衡設成 ColorTemp 時才生效。

    分成兩列的話，使用者會在色溫那格輸入數字然後發現沒反應 —— 相機在
    其他白平衡預設下自己決定色溫。合併並在不適用時停用。
    """
    js = _script(HTML.read_text())
    assert "function wbRow" in js and "'__wb'" in js
    block = js[js.index("function wbRow"):js.index("function settingsHTML")]
    assert "byTemp" in block, "沒有判斷白平衡是否為色溫模式"
    assert "lockedAttr(!byTemp)" in block, "非色溫模式下沒有停用 K 值"
    assert "pick Color Temp. to set K" in block
    assert '<select data-set="white_balance">' in block, "白平衡不是下拉選單"
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    for token in ("'white_balance'", "'color_temp'"):
        assert token not in body, f"{token} 不該還在一般的 ORDER 迴圈裡"
    print("✓ 白平衡改下拉並與色溫合併，非色溫模式下 K 值停用")


def test_row_slices_use_the_next_function_defined():
    """這些測試靠切片檢查每個 row builder 的內容，切片邊界必須真的在它後面。

    寫錯過一次：邊界指向定義在前面的函式，切出空字串，斷言全部無聲通過。
    """
    js = _script(HTML.read_text())
    order = ["function formatRow", "function exposureRow",
             "function wbRow", "function settingsHTML"]
    positions = [js.index(x) for x in order]
    assert positions == sorted(positions), f"函式定義順序變了：{order}"
    print("✓ row builder 的定義順序符合測試切片假設")


def test_all_controls_coerce_values_the_same_way():
    """按鈕、下拉、數字輸入送出前都要依 schema 決定型別。

    白平衡壞過：下拉的處理器無條件 parseFloat，而白平衡的值是字串
    （Auto / ColorTemp），變成 NaN、序列化成 null —— 選了完全沒反應。
    三種控制項各寫各的轉換，只要有一邊漏掉就會重演。
    """
    js = _script(HTML.read_text())
    assert "function coerceValue" in js, "沒有共用的型別轉換"
    # 送出路徑不得再出現裸的 parseFloat
    import re as _re
    assert not _re.search(r"settings: \{\[.*?\]: parseFloat", js), \
        "還有控制項直接 parseFloat 就送出"
    assert js.count("coerceValue") >= 4, "不是所有控制項都走共用轉換"
    print("✓ 按鈕 / 下拉 / 輸入框共用型別轉換")


def test_display_matching_is_separate_from_correctness():
    """相機回報的值不一定精確等於選項（實測選 29.97 相機存 30）。

    完全不亮會讓整列看起來沒選中，所以顯示用的比對放寬 —— 但只用於顯示，
    而且要看得出不是精確命中。伺服器端的回讀驗證仍是嚴格比對，
    值沒寫進去照樣要報，不能因為畫面好看就假裝成功。
    """
    js = _script(HTML.read_text())
    assert "DISPLAY_EPSILON" in js and "function matchState" in js
    assert "'on approx'" in js, "接近命中沒有獨立的樣式"
    assert ".o.on.approx" in HTML.read_text(), "缺少接近命中的樣式定義"
    assert "camera reports" in js, "接近命中沒有標出實際值"

    # 伺服器端的容差不得被一起放寬
    src = (Path(__file__).resolve().parent.parent / "camera_settings.py").read_text()
    assert "VALUE_EPSILON = 1e-6" in src, "伺服器端的回讀容差被放寬了"
    print("✓ 顯示比對放寬、正確性檢查維持嚴格")


def test_every_dropdown_uses_the_shared_option_builder():
    """五個下拉共用同一套選中判定，避免只有一部分吃到放寬的比對。"""
    js = _script(HTML.read_text())
    assert "function optionsFor" in js
    assert "String(c) === String(v) ? ' selected'" not in js, "還有下拉自己判斷選中"
    assert js.count("optionsFor(") >= 5, "不是所有下拉都走共用產生器"
    print("✓ 所有下拉共用選中判定")


def test_selected_buttons_keep_their_colour_on_hover():
    """已選的按鈕滑入要變淺藍，不能被未選狀態的灰色蓋掉。

    .o:hover:not(:disabled) 的特異性 (0,3,0) 高於 .o.on 的 (0,2,0)，
    所以光靠宣告順序不夠 —— 已選狀態需要自己的 hover 規則。
    """
    css = HTML.read_text()
    assert ".o:hover:not(:disabled):not(.on)" in css, "未選的 hover 沒有排除已選狀態"
    assert ".o.on:hover:not(:disabled)" in css, "已選狀態沒有自己的 hover 樣式"
    assert ".o.on.approx:hover:not(:disabled)" in css, "接近命中沒有 hover 樣式"
    assert ".o.locked.on:hover" in css, "鎖定的按鈕滑入不該變色"
    print("✓ 已選按鈕滑入變淺藍而非灰")


def test_mode_based_disabling_survives_the_10hz_redraw():
    """A 模式下快門要反灰、S 模式下光圈要反灰，而且不能被重繪打開。

    render() 每 100ms 會重設所有控制項的 disabled。先前它讀的是一個從未
    被設定過的屬性（lockedByMode），所以每次重繪都把依模式停用的狀態清掉 ——
    邏輯是對的，畫面上卻永遠是可按的。
    """
    js = _script(HTML.read_text())
    assert "function lockedAttr" in js, "沒有統一的停用標記"
    assert "' disabled'" not in js, "還有地方直接寫 disabled 而不標記原因"
    assert "el.dataset.locked === '1'" in js, "render() 沒有尊重停用標記"
    assert "lockedByMode" not in js, "還在讀那個從未設定過的屬性"

    # A 模式放光圈擋快門、S 模式反之
    block = js[js.index("function exposureRow"):js.index("function wbRow")]
    assert "cur === 'AperturePriority'" in block and "cur === 'ShutterPriority'" in block
    assert "lockedAttr(!apOK)" in block and "lockedAttr(!shOK)" in block
    print("✓ 依模式的反灰狀態不會被 10Hz 重繪清掉")


def test_state_updates_do_not_rebuild_dom():
    """狀態廣播是 10Hz。無條件寫 innerHTML 會讓節點每 100ms 被銷毀重建 ——
    版面抖動、捲軸亂飄，按鈕在按下的瞬間被換掉所以按不到。實測踩過。

    render() 這條路徑上只能透過會先比對的 helper 碰 DOM。
    """
    js = _script(HTML.read_text())
    body = js[js.index("function render()"):js.index("function renderNotices()")]
    assert ".innerHTML" not in body, "render() 裡有直接寫 innerHTML"
    assert ".disabled =" not in body, "render() 裡有直接設 disabled，應該用 setDisabled"
    for helper in ("setHTML", "setText", "setDisabled", "setShown"):
        assert f"function {helper}" in js, f"缺少 {helper} helper"
    assert "if (el.__last !== html)" in js, "setHTML 沒有先比對就寫入"
    print("✓ render() 只透過會先比對的 helper 碰 DOM")


def test_transient_notice_survives_rerender():
    """暫時訊息若直接插進 DOM，會被下一次 10Hz 重繪抹掉（原本就是這樣）。"""
    js = _script(HTML.read_text())
    assert "insertAdjacentHTML" not in js, "flash() 不該直接插進 DOM"
    assert "transient" in js and "transient.until" in js
    print("✓ 暫時訊息納入重繪計算，不會被抹掉")


def test_stop_button_stays_clickable_while_recording():
    """錄影中若把錄影鈕一起停用，就再也停不下來了。"""
    js = _script(HTML.read_text())
    assert "setDisabled($('btn-rec'), !live)" in js, "錄影鈕的停用條件不該包含 recording"
    print("✓ 錄影中仍可按停止")


def test_every_test_file_runs_all_of_its_tests():
    """測試定義在 __main__ 區塊之後就不會被執行，而且不會有任何抱怨。

    踩過兩次：加在檔案末尾的測試看起來過了，其實一次都沒跑 —— 變異測試
    全過才發現。這裡直接檢查每個測試檔的執行區塊是不是真的在最後面。
    """
    import re
    # 行首比對 —— 這個測試自己的原始碼裡就有那串字，縮排過的不算
    runner = re.compile(r"^if __name__ ==", re.M)
    root = Path(__file__).resolve().parent
    for path in sorted(root.glob("test_*.py")):
        text = path.read_text()
        starts = [m.start() for m in runner.finditer(text)]
        if not starts:
            continue
        assert len(starts) == 1, f"{path.name} 有 {len(starts)} 個執行區塊"
        defined_after = re.findall(r"^def (test_\w+)", text[starts[0]:], re.M)
        assert not defined_after, (
            f"{path.name} 有 {len(defined_after)} 個測試定義在執行區塊之後，"
            f"永遠不會被執行：{defined_after}")
    print("✓ 每個測試檔的測試都在執行區塊之前定義")


def test_lens_corrections_share_one_row_and_null_is_not_an_option():
    """四項鏡頭校正是同一類東西，選項也一樣 —— 各佔一列太佔版面。

    另外 Null 是「相機沒有回報」的佔位。把它畫成一個叫「—」的按鈕，等於邀請
    使用者去選一個不存在的設定。
    """
    html = HTML.read_text()
    assert "function comboRow(" in html, "沒有合併列的產生器"
    # 檢查 ORDER 裡的那個中文標題，不是欄位名 —— 欄位名在 LOC_PARTS 裡也有
    for title in ("'Distortion Correction'", "'Chromatic Aberration Correction'",
                  "'Diffraction Correction'", "'Vignetting Correction'"):
        assert title not in html, f"{title} 還單獨佔一列"
    for name in ("loc_distortion", "loc_chromatic_aberration",
                 "loc_diffraction", "loc_vignetting"):
        assert name in html, f"{name} 整個不見了"
    assert re.search(r"'__loc'.*?comboRow\(byName, 'Lens Optics Compensation', LOC_PARTS\)", html, re.S), \
        "合併列沒有接進 ORDER"

    assert "function usefulChoices(" in html, "沒有濾掉 Null 的地方"
    assert "let choices = usefulChoices(s)" in html, "一般選項列沒有用它"
    print("✓ 鏡頭校正併成一列，Null 不會變成可選項")


def test_focus_panel_exists_and_can_return_to_autofocus():
    """對焦面板：模式、臉眼偵測、對焦區域、對焦點。

    模式那一項是重點。set_focus_position 每次都強制寫 MF（不然相機會搶回
    焦點），在這個面板出現之前沒有任何地方寫得回去 —— 拉一次滑桿就永遠
    停在手動對焦。
    """
    html = HTML.read_text()
    assert 'id="focus-controls"' in html, "沒有對焦控制的容器"
    assert "function renderFocusControls(" in html, "沒有對焦面板的渲染"
    assert "/api/focus/mode" in html, "沒有接上切換對焦模式的端點"
    assert "/api/focus/bounds" in html, "沒有讀對焦座標範圍"
    for key in ("focus_mode", "face_eye_af", "focus_area", "focus_point"):
        assert key in html, f"面板沒有處理 {key}"
    # 對焦點沒有手動輸入 —— 座標系（682×1024，3:2）跟畫面（16:9）對不上，
    # 填數字等於盲填，在預覽上點直觀得多。
    assert "fp-centre" not in html and "fp-y" not in html, "還留著手動座標輸入"
    # 對焦位置也不留手動輸入 —— 滑桿即時送出之後，數字框與 ±100 只是重複
    for gone in ("pos-input", "btn-setpos", "stepPos"):
        assert gone not in html, f"還留著 {gone}"
    # 實測：MF 下相機忽略對焦區域與臉眼偵測的寫入。按了沒反應比按不下去
    # 更讓人困惑，所以要標成不可用而不是放著。
    assert "'no effect in MF'" in html, "MF 下沒有標示對焦對象不可用"
    # 單點與臉眼互斥，合併成一個控制項；多點會把對焦點鎖在正中央，不列
    import re
    assert re.search(r"\bconst TARGETS\s*=", html), "單點與臉眼沒有合併"
    assert "MultiAutoFocusPoints" not in html.split("const TARGETS")[1][:600], \
        "合併後的控制項還列出多點"
    assert "st.focus_mode === 'MF'" in html, "沒有依對焦模式判斷"
    # 三項併成一列 —— 各佔一列會把對焦面板撐得比 live view 還高
    assert "function focusGroup(" in html, "對焦三項沒有併成一列"
    assert "function focusOptRow(" not in html, "還留著逐項一列的舊實作"
    # 舊的唯讀顯示要拿掉，不然模式會同時出現在兩個地方
    assert "$('f-mode')" not in html, "還留著唯讀的對焦模式顯示"
    print("✓ 對焦面板可切換模式 / 臉眼偵測 / 區域 / 對焦點")


def test_tap_to_focus_follows_the_af_point_subject():
    """點選對焦不是獨立開關 —— 它就是「對焦對象 = AF Point」這件事。

    多一個開關等於要求使用者把兩件同一回事的設定對齊，忘了對齊就會覺得
    點了沒反應。交給臉／眼偵測時對焦點由相機決定，MF 下相機根本不對焦。

    開啟時畫面上要標出相機實際採用的位置（它會吸附到格點），不然使用者
    分不出「點歪了」和「相機吸附了」。
    """
    html = HTML.read_text()
    assert 'id="btn-tapfocus"' not in html, "還留著獨立開關"
    # 畫不畫對焦框：看對焦點現在是不是生效中
    assert "function afPointActive(" in html, "沒有判斷對焦點是否生效"
    assert re.search(r"afPointActive\(\)[\s\S]{0,160}face_eye_af", html), \
        "沒有依對焦對象判斷"
    assert re.search(r"st\.focus_mode !== 'MF'", html), "MF 下沒有關掉"
    # 能不能點：只要相機在就能點。指定座標就是在說「對這裡」，相機端會把
    # 臉／眼偵測關掉、區域切成單點、MF 切回 AF-S。
    assert "function canTapFocus(" in html, "沒有獨立的可點判斷"
    assert re.search(r"if \(!canTapFocus\(\) \|\| !focusBounds\) return;", html), \
        "點擊還綁在對焦對象上"
    assert "function onLiveviewClick(" in html, "預覽沒有點擊處理"
    assert 'id="lv-marker"' in html, "沒有標示對焦點的 marker"
    assert "getBoundingClientRect" in html, "沒有把點擊換算成畫面比例"
    # marker 的 CSS 是 display:none，用 setShown 顯示時設 '' 會退回那條規則 ——
    # 於是永遠畫不出來。要明確設成 block。
    assert "marker.style.display" in html, "marker 用了會被 CSS 蓋掉的顯示方式"
    # 框要照相機給的實際尺寸畫。選了「小框」卻看到一樣大的方塊，
    # 那個控制項就等於沒有意義。
    assert "point_sizes" in html, "沒有用相機給的對焦框尺寸"
    # 要依比例算，不是固定像素 —— 用子字串比對「有沒有 marker.style.width」
    # 抓不到「改成 34px」這種變異，實測過。
    assert re.search(r"marker\.style\.width\s*=\s*w\s*\+", html), \
        "marker 沒有依實際框大小按比例繪製"
    # 基準要是完整座標系，不是有效區域。有效區域是「對焦點中心能去到的範圍」，
    # 拿它當基準會讓框大兩成、位置也對不上畫面。
    assert re.search(r"size\[1\]\s*/\s*Math\.max\(1,\s*b\.width\)", html), \
        "框大小用了錯的基準"
    # 座標系 3:2、畫面 16:9 —— 垂直要用畫面實際看得到的高度換算，
    # 否則框會變形也會偏大。
    assert "function visibleSpan(" in html, "沒有處理畫面與座標系的長寬比差異"
    assert re.search(r"h\s*=\s*\(size\[0\]\s*/\s*Math\.max\(1,\s*view\.height\)", html), \
        "框高沒有用可見高度換算"
    # 映射要用相機宣告的有效區域，不是憑空假設
    assert "b.top" in html and "b.left" in html, "沒有用相機宣告的有效區域做映射"
    print("✓ 點選對焦有開關，預設關閉，並標出相機實際位置")


def test_the_protocol_probe_ui_is_gone():
    """測試片段那一區是反推協定時用的，不是操作介面。

    它的說明在講「movie file info 不可信、要看 capture status 與 image DB
    tail」—— 那是給當時的我看的筆記，對使用相機的人沒有意義。錄影與下載
    都能用了之後，它只剩雜訊。端點留著（/api/record/clip 仍可用於研究）。
    """
    html = HTML.read_text()
    for gone in ("btn-clip", "clip-secs", "recordClip", "rec-status"):
        assert gone not in html, f"還留著 {gone}"
    print("✓ 協定探測用的 UI 已移除")


def test_unsettable_settings_render_disabled():
    html = HTML.read_text()
    assert re.search(r"const unavail = s\.writable === false", html), "沒有讀 writable"
    assert re.search(r"disabled:\s*needsM \|\| unavail", html), "unavail 沒接到 disabled"
    assert re.search(r"s\.writable === false \? ' disabled data-locked", html), \
        "鏡頭校正整合列沒吃 writable"
    print("✓ 相機宣告不可設定的項目在網頁上反灰")


def test_crop_and_stabilisation_share_one_row_after_the_format_row():
    """兩者都在裁切感光範圍，而且會互相影響 —— CinemaDNG 下相機會關掉
    電子防手震。分開兩列看不出這層關係，放遠了更看不出來。
    """
    html = HTML.read_text()
    order = html[html.index("const ORDER = ["):]
    order = order[:order.index("];")]
    rows = re.findall(r"\['(\w+|__\w+)'", order)
    assert rows.index("__crop") == rows.index("__format") + 1, \
        f"裁切列沒有緊接在影像規格之後：{rows[:4]}"
    for name in ("dc_crop_mode", "electronic_stabilization"):
        assert f"['{name}'" not in order, f"{name} 還單獨佔一列"
        assert name in html, f"{name} 整個不見了"
    assert re.search(r"'__crop'.*?comboRow\(byName, title, CROP_PARTS\)", html, re.S), \
        "裁切列沒有接進 ORDER"
    print("✓ DC 裁切與電子防手震併成一列，緊接影像規格")


def test_tap_focus_clamps_to_the_range_the_camera_really_accepts():
    """宣告的有效區 613 是**框**能落在哪，座標卻是框的中心 —— 中心的可達
    範圍要再向內縮半個框。實測三種框大小的四個邊界全部符合這條規則。

    只夾到有效區的話，邊緣的點寫進去會被相機往內拉，看起來就像對焦點全擠
    在畫面中間、外圈點不到。
    """
    html = HTML.read_text()
    assert re.search(r"const lim = reachable\(b, st\.point_size", html), \
        "點擊沒有用可達範圍，或沒有依當下的框大小算"
    assert re.search(r"Math\.max\(lim\.top", html) and \
           re.search(r"Math\.max\(lim\.left", html), "點擊沒有夾進可達範圍"

    # 真的跑一遍，拿實測值當答案 —— 檢查字串長相的話，把 /2 改成 /4
    # 也會照樣通過。這四組數字是對著相機一個邊界一個邊界寫進去讀回來的。
    got = _run_js(html, r"function reachable\(b, sizeIndex\) \{.*?^\}", """
        var b = {height: 682, width: 1024, top: 85, bottom: 597,
                 left: 96, right: 928,
                 point_sizes: [[128, 128], [64, 64], [32, 32]]};
        var out = [];
        for (var i = 0; i < 3; i++) {
            var r = reachable(b, i);
            out.push([r.top, r.bottom, r.left, r.right].join(','));
        }
        out.join(' | ')
    """)
    if got is None:
        print("- 跳過可達範圍的實跑（沒有 JavaScript 引擎）")
        return
    assert got == "149,533,160,864 | 117,565,128,896 | 101,581,112,912", \
        f"可達範圍算錯：{got}"
    print("✓ 點擊夾進「有效區縮半個框」的實際可達範圍（對相機實測值）")


def test_the_af_area_is_drawn_on_the_preview():
    html = HTML.read_text()
    assert 'id="lv-afarea"' in html, "沒有 AF 範圍的元素"
    assert re.search(r"area\.style\.display = 'block'", html), "沒有顯示出來"
    assert re.search(r"area\.style\.display = 'none'", html), "關掉點對焦時沒有收掉"
    print("✓ 相機的 AF 範圍畫在預覽上")


def test_the_focus_marker_is_centred_with_transform_not_margin():
    """margin-top 的百分比是相對**容器的寬度**算的，不是高度。

    對焦框的大小是用百分比設的（框的尺寸來自相機座標系），所以用負 margin
    置中時，垂直方向會多偏「寬高比」倍 —— 16:9 下約 1.78 倍。看到的就是框
    高出 AF 虛線框的上緣、卻碰不到下緣。
    """
    html = HTML.read_text()
    assert "transform:translate(-50%, -50%)" in html, "對焦框沒有用 transform 置中"
    assert not re.search(r"marker\.style\.margin", html), \
        "還在用負 margin 置中，垂直方向會偏"
    print("✓ 對焦框用 transform 置中（負 margin 的垂直百分比是錯的）")


def test_motor_state_and_focal_length_share_the_position_row():
    """兩者都是在描述位置那一列講的那顆鏡頭 —— 各佔一列是把同一件事拆開。"""
    html = HTML.read_text()
    # 找那兩列的標籤標記，不是字串本身 —— 註解裡提到它們是正常的
    for gone in (">Motor<", ">Focal Length<", "'f-state'", "'f-len'"):
        assert gone not in html, f"還留著 {gone}"
    assert 'id="f-lens"' in html, "沒有合併後的元素"
    body = html[html.index("setText($('f-pos')"):]
    body = body[:body.index("renderFocusControls()")]
    assert "focus_state" in body and "focal_length_mm" in body, \
        "馬達狀態或焦距沒有接進位置那一列"
    print("✓ 馬達狀態與焦距併入位置列")


def test_each_setting_row_folds():
    """設定調好之後就很少再動，但每一列的按鈕都常駐著，一個面板要捲很久。

    收起時只留標題和目前值，展開時只留控制項 —— 控制項自己就說明了它是
    什麼，標題再佔一行是多的。
    """
    html = HTML.read_text()
    assert "const openRows = new Set()" in html, "沒有記住展開狀態"
    assert "function applyFolds(" in html and "applyFolds(box);" in html, \
        "折疊沒有接進重繪"
    assert ".opt.folded .opts { display: none; }" in html, "收起時沒有藏掉控制項"
    assert re.search(r"\.opt:not\(\.folded\) \.opt-head \{ display: none", html), \
        "展開時沒有藏掉標題行"
    # 狀態要存在重繪活得下來的地方 —— 每 100ms 整個 DOM 會被換掉
    assert "openRows.has(key)" in html and "openRows.delete(key)" in html
    no_comments = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
    assert ":has(" not in no_comments, "用了 :has()，瀏覽器支援不保證"
    print("✓ 每個設定群組可折疊，狀態撐得過重繪")


def test_frame_size_is_locked_unless_the_subject_is_the_af_point():
    """對焦框大小只在 AF Point 下有意義 —— 交給臉／眼偵測時，框的位置和
    大小都是相機自己決定的，MF 下相機根本不對焦。
    """
    html = HTML.read_text()
    assert re.search(r"const sizeLocked = isMF \|\| activeTarget !== 'point'", html), \
        "沒有依對焦對象鎖定框大小"
    block = html[html.index("const sizeLocked"):]
    block = block[:block.index("</span>' : '')")]
    assert "sizeLocked ? ' disabled data-locked=" in block, "鎖定沒有接到按鈕上"


def test_position_focus_mode_and_frame_size_share_one_row():
    """三者都是在講同一顆鏡頭要對哪裡、怎麼對 —— 分三列只是把一件事攤開。"""
    html = HTML.read_text()
    assert '<span class="label">Position</span>' not in html, "還留著獨立的 Position 一列"
    assert '<span class="opt-name">AF Point</span>' not in html, "還留著獨立的 AF Point 一列"
    body = html[html.index("const html = '<div class=\"opt\"><div class=\"opt-head\">' +"):]
    body = body[:body.index("</div></div>';")]
    for token in ("f-pos", "Focus Mode", "Subject", "Frame Size"):
        assert token in body, f"{token} 沒有併進那一列"
    # 位置的數值不能進 html 字串 —— 那會讓這一列每 100ms 重建
    assert "st.focus_position" not in body, "位置的數值被寫進了 html，會造成不斷重建"


def test_dynamic_ids_are_read_safely():
    """f-pos 這些現在由對焦面板產生 —— 面板還沒渲染時它們不存在。"""
    html = HTML.read_text()
    assert re.search(r"function setText\(el, v\) \{ if \(!el\) return;", html), \
        "setText 沒有防呆"
    assert "$('f-range-hint').textContent" not in html, \
        "還在直接取用可能不存在的元素"
    # 面板重建後範圍提示要重新填，只填一次的話重建後就空著
    assert re.search(r"setText\(\$\('f-range-hint'\), focusRange", html), \
        "範圍提示沒有每次重繪都填"


def test_the_slider_follows_the_camera_unless_it_is_being_held():
    """滑桿要跟著自動對焦跑，除非使用者正按著它。

    先前用的是 busy()：任何指令都會擋住 4 秒。切個對焦模式、點一下對焦點，
    滑桿就有 4 秒不動 —— 而 AF 正是在那幾秒裡把鏡頭轉到定位的。
    """
    html = HTML.read_text()
    assert re.search(r"st\.focus_position != null && !sliderHeld", html), \
        "滑桿的跟隨條件還綁在 busy() 上"
    assert "markBusy()" not in html[html.index("function onSliderInput"):
                                    html.index("function onSliderCommit")], \
        "拖曳還在用全域 busy 旗標"
    assert "function releaseSlider(" in html, "放開後沒有恢復跟隨"


def test_af_c_is_locked_in_cine():
    """CINE 下相機直接拒收 AF-C：寫進去讀回來是 AF-S（兩種機身模式都量過）。"""
    html = HTML.read_text()
    assert re.search(r"c === 'AF_C' && st\.camera_mode === 'movie'", html), \
        "CINE 下沒有鎖住 AF-C"
    assert "typeof locked === 'function'" in html, "focusGroup 不支援逐項鎖定"


def test_the_focus_range_sits_after_the_title():
    html = HTML.read_text()
    head = html[html.index("'<span class=\"opt-name\">Focus</span>' +"):]
    head = head[:head.index("opt-cur")]
    assert "f-range-hint" in head, "範圍沒有接在 Focus 標題後面"


def test_the_tap_to_focus_hint_is_gone():
    """虛線框和 crosshair 游標已經說明了可以點，那行提示每次都在講同一句話。"""
    html = HTML.read_text()
    for gone in ("tapfocus-hint", "lv-tools", "the camera snaps to its grid"):
        assert gone not in html, f"還留著 {gone}"


def test_notes_live_with_the_controls_not_in_the_title():
    """提示要跟著控制項一起出現。

    留在標題行的話，收起狀態下每一列後面都拖著一長串說明 —— 那正是收合
    要省掉的東西，而且那些話只有在你真的要改那一項時才有用。
    """
    html = HTML.read_text()
    assert re.search(r"row\.querySelector\('\.opt-head > \.opt-note'\)", html), \
        "沒有把提示從標題行取出來"
    # 連條件一起釘住 —— 只檢查「有沒有 appendChild」的話，把條件改成 false
    # 也會照樣通過（實測過）。
    assert re.search(
        r"if \(note && opts\) \{\s*"
        r"if \(note\.textContent\.trim\(\)\) opts\.appendChild\(note\);\s*"
        r"else note\.remove\(\);", html), "提示沒有在有內容時移進控制項區"
    assert ".opts > .opt-note" in html, "移進去的提示沒有樣式"
    assert "classList.toggle('bare'" not in html, "還留著舊的 bare 標記"


def test_the_whole_title_line_opens_a_collapsed_row():
    """收起狀態下那一行就是這一列的全部內容 —— 要求對準左邊那顆小按鈕沒有
    道理。展開時標題是藏起來的，所以不會誤觸。

    按鈕在兩個狀態下做的事相反，形狀也不一樣：收起是箭頭，展開是叉。
    """
    html = HTML.read_text()
    assert re.search(r"const toggle = \(\) => \{", html), "沒有共用的開關"
    assert re.search(r"if \(head\) head\.onclick = toggle;", html), \
        "標題行不能點開"
    assert "btn.onclick = toggle;" in html, "按鈕沒有接上同一個開關"
    assert re.search(r"btn\.textContent = open \? '\\u00d7' : '\\u203a';", html), \
        "展開後左邊不是叉"
    assert ".opt.folded .opt-head { cursor: pointer; }" in html, \
        "收起的標題沒有可點的游標"


def test_recording_does_not_push_the_layout_around():
    """錄影中的橫幅會在開始 / 停止的瞬間把底下整片版面往下推 —— 而那正是
    你盯著畫面的時候。頂端的 REC 徽章和被停用的控制項說的是同一件事，而且
    不改變版面高度。
    """
    html = HTML.read_text()
    assert "changing exposure mid-take" not in html, "錄影橫幅還在"
    assert "notice('info', 'Recording'" not in html, "錄影橫幅還在"
    # 講同一件事的另外兩個管道要留著
    assert 'class="badge rec"' in html, "頂端沒有 REC 徽章"
    assert re.search(r"setDisabled\(", html), "錄影中沒有停用控制項的機制"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_ui 全部通過")
