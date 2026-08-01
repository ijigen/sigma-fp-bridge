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
