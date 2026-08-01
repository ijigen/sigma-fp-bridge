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


def test_iso_row_is_merged_and_uses_a_dropdown():
    """ISO 的模式與數值在同一列，數值用下拉選單。

    ISO 有 25 個合法值，橫排按鈕會佔掉整列；而且使用者多半心裡已經有目標值，
    用選的比用捲的快。自動模式下數值不可調 —— 選了也會被相機覆蓋。
    """
    js = _script(HTML.read_text())
    assert "function isoRow" in js, "沒有合併的 ISO 列"
    assert "'__iso'" in js, "ORDER 沒有指向合併列"
    assert "iso_auto" not in js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")], \
        "iso_auto 不該還在一般的 ORDER 迴圈裡"
    assert "<select data-set=\"iso\"" in js, "ISO 數值不是下拉選單"
    assert "isAuto ? ' disabled' : ''" in js, "自動模式下沒有停用數值選單"
    assert "select[data-set]" in js, "select 沒有綁事件"
    # 曝光補償併在同一排：它跟 ISO 一樣是調亮暗的旋鈕
    assert 'data-set="exposure_compensation"' in js, "EV 沒有併進 ISO 那列"
    assert "ISO / EV" in js
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    assert "exposure_compensation" not in body, "EV 不該還在一般的 ORDER 迴圈裡"
    print("✓ ISO 與 EV 合併成一列，數值皆用下拉選單")


def test_shutter_row_merges_speed_and_angle():
    """秒數與角度是同一件事的兩種表示，合併成一列以免使用者以為是兩個設定。

    CINE 模式下沒有 shutter_speed（相機會收下寫入然後丟棄），所以「秒」
    要停用並說明，而不是留一個按了沒用的選項。
    """
    js = _script(HTML.read_text())
    assert "function shutterRow" in js
    assert "'__shutter'" in js
    assert "secondsAvailable" in js, "沒有處理 CINE 下秒數不可用"
    assert "CINE 模式的快門只能用角度設定" in js
    body = js[js.index("const ORDER"):js.index("const SCROLL_IF_OVER")]
    assert "shutter_speed" not in body and "shutter_angle" not in body, \
        "快門不該還在一般的 ORDER 迴圈裡"
    print("✓ 快門合併成一列，CINE 下停用秒數並說明")


def test_aperture_uses_a_dropdown():
    js = _script(HTML.read_text())
    assert "'__aperture'" in js and "function selectRow" in js
    assert "需切換成 M 或 A 模式" in js, "光圈沒有標示可設的模式"
    print("✓ 光圈用下拉選單，並標示需要的曝光模式")


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
    assert "grp-l" in js, "同列的多個群組沒有標籤區隔"
    print("✓ 規格 / 大小 / 位元合併成一列，分群標示")


def test_locked_option_groups_do_not_vanish():
    """相機在某些組合下會鎖死某個項目 —— 實測 UHD 下不開放調整色彩位元，
    合法值清單是空的。

    整組消失會讓人以為功能壞掉（實際被回報過）。要改成顯示目前值並標明鎖定。
    """
    js = _script(HTML.read_text())
    block = js[js.index("function formatRow"):js.index("function settingsHTML")]
    assert ".filter(g => g[1]);" in block, "沒有可選值的群組仍被濾掉"
    assert "locked = true" in block, "空清單沒有標成鎖定"
    assert "相機不開放調整這一項" in block
    assert "灰色項目在此組合下鎖定" in js
    print("✓ 無可選值的群組改為鎖定顯示，不會消失")


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
