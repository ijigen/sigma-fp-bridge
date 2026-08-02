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
    assert "'capture_mode', '機身模式'" in js
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
    assert "需切換成 M 模式" in js
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
    assert "CINE 模式的快門只能用角度設定" in block
    assert "由相機決定" in block, "被相機接管的項目沒有說明"
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
    assert "相機不開放調整這一項" in block
    assert "灰色項目在此組合下鎖定" in js
    print("✓ 無可選值的群組改為鎖定顯示，不會消失")


def test_unidentified_values_say_so():
    """有些設定我們知道相機接受哪些值，卻不知道值代表什麼
    （例如 mov_image_quality 的 1 / 2）。

    讓使用者對著沒有意義的數字猜，跟控制項壞掉沒兩樣 —— 要直說未確認。
    """
    js = _script(HTML.read_text())
    assert "數值意義尚未確認" in js
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
    assert "選「色溫」才能指定 K 值" in block
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
    assert "相機實際值" in js, "接近命中沒有標出實際值"

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
    assert "function locRow(" in html, "沒有鏡頭校正的合併列"
    # 檢查 ORDER 裡的那個中文標題，不是欄位名 —— 欄位名在 LOC_PARTS 裡也有
    for title in ("'畸變校正'", "'色差校正'", "'繞射校正'", "'周邊光量'"):
        assert title not in html, f"{title} 還單獨佔一列"
    for name in ("loc_distortion", "loc_chromatic_aberration",
                 "loc_diffraction", "loc_vignetting"):
        assert name in html, f"{name} 整個不見了"
    assert "'__loc'" in html and "locRow(byName)" in html, "合併列沒有接進 ORDER"

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
    assert "fp-centre" in html, "沒有回中央的捷徑"
    # 舊的唯讀顯示要拿掉，不然模式會同時出現在兩個地方
    assert "$('f-mode')" not in html, "還留著唯讀的對焦模式顯示"
    print("✓ 對焦面板可切換模式 / 臉眼偵測 / 區域 / 對焦點")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_ui 全部通過")
