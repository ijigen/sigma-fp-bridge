#!/usr/bin/env python3
"""相機 session 生命週期。不需要相機，但需要裝好 sigma-ptpy。

執行：python3 tests/test_session.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import sigma_fp_focus
except ImportError as e:
    print(f"- 跳過：沒裝 sigma-ptpy（{e}）")
    sys.exit(0)


class FakeSessionCM:
    def __init__(self):
        self.exited = False

    def __exit__(self, *args):
        self.exited = True


class FakeCam:
    def __init__(self, close_application_raises=False):
        self.close_application_called = False
        self._raises = close_application_raises
        self._bridge_session_cm = FakeSessionCM()

    def close_application(self):
        self.close_application_called = True
        if self._raises:
            raise RuntimeError("韌體不吃這個指令")


def test_close_camera_sends_close_application():
    """一定要送 CloseApplication，否則機身按鍵會一直鎖著。

    config_api() 讓相機進入 API 模式後，SDK 文件明說「API does not accept
    any operation other than the power-off operation」—— 只關 PTP session
    是不夠的，相機不會退出 API 模式，使用者原本的設定也不會還原。
    這是實際踩過的 bug，別讓它回來。
    """
    cam = FakeCam()
    sigma_fp_focus.close_camera(cam)
    assert cam.close_application_called, "沒有送 CloseApplication —— 相機會鎖住"
    assert cam._bridge_session_cm.exited, "PTP session 沒關"
    print("✓ close_camera() 會送 CloseApplication 並關掉 session")


def test_session_still_closed_if_close_application_fails():
    """CloseApplication 的 payload 在 sigma-ptpy 裡註明 undocumented，
    有些韌體可能不吃。失敗也不能擋住 session 關閉。"""
    cam = FakeCam(close_application_raises=True)
    sigma_fp_focus.close_camera(cam)
    assert cam._bridge_session_cm.exited, "CloseApplication 失敗就不關 session 了"
    print("✓ CloseApplication 失敗時仍會關掉 session")


def test_close_camera_without_session_does_not_raise():
    """dump 工具那類短命連線可能沒掛 session context manager。"""
    class Bare:
        def __init__(self):
            self.called = False

        def close_application(self):
            self.called = True

    cam = Bare()
    sigma_fp_focus.close_camera(cam)
    assert cam.called
    print("✓ 沒有 session context manager 時不會爆炸")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\ntest_session 全部通過")
