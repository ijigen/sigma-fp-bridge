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
