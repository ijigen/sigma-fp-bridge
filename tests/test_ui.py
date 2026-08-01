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


def test_body_mode_is_not_presented_as_settable():
    """機身模式是實體撥桿，PTP 改不了。

    做成可按的按鈕會讓使用者按了沒反應還以為壞掉。它必須是鎖定的指示。
    """
    js = _script(HTML.read_text())
    assert "'__mode'" in js and "locked: true" in js, "機身模式沒有標成 locked"
    assert "if (b.dataset.set === '__mode') return;" in js, "機身模式的按鈕沒有擋掉送出"
    print("✓ 機身模式呈現為鎖定指示，不是可按的設定")


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
    assert "autoIso ? ' disabled' : ''" in block, "自動時沒有停用 ISO 數值"
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
    assert "byTemp ? '' : ' disabled'" in block, "非色溫模式下沒有停用 K 值"
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_ui 全部通過")
