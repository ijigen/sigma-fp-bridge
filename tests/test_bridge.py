#!/usr/bin/env python3
"""CameraWorker / FrameHub / HTTP 端點的行為測試，用假相機跑。

這些測試存在的理由：相機存取的序列化與優先權是沒辦法用肉眼看出對錯的，
而開發機上不一定有 Sigma fp。這裡把重構真正在乎的性質釘死：

  - 控焦指令會插到排隊中的影格請求前面
  - 連發的位置設定會被合併，只有最後一個真的送上 USB
  - 多個 MJPEG client 共用同一組影格（不是各抓各的）
  - 等馬達停止時 live view 不會被餓死
  - 斷線的 MJPEG client 會被回收

執行：python3 tests/test_bridge.py     （需要 aiohttp，requirements.txt 已含）
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_camera

CAM = fake_camera.install()

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

import mac_bridge_server as B  # noqa: E402


def reset():
    CAM.reset()
    B.state.camera = CAM
    B.state.camera_connected = True
    B.state.mjpeg_clients.clear()
    B.state.ws_clients.clear()
    B.state.calibration = []
    # 預設不啟用夾值，想測範圍的測試自己呼叫 refresh_focus_range()
    B.state.focus_range = None
    B.state.last_lens_focal_mm = None


@contextlib.asynccontextmanager
async def running_worker():
    """換上一個乾淨的 worker，離開時收乾淨。"""
    w = B.CameraWorker()
    w.start()
    previous = B.worker
    B.worker = w
    try:
        yield w
    finally:
        B.worker = previous
        await w.stop()


# ── CameraWorker ────────────────────────────────────────────────────────


async def test_control_preempts_liveview():
    reset()
    async with running_worker() as w:
        order = []

        def make(label):
            def fn():
                order.append(label)
                time.sleep(0.02)
            return fn

        # 同步塞完再 await：worker 還沒開始處理，佇列裡就已經有全部 6 個
        futures = [w.submit(make(f"lv{i}"), priority=B.Priority.LIVEVIEW) for i in range(5)]
        futures.append(w.submit(make("control"), priority=B.Priority.CONTROL))
        await asyncio.gather(*futures)

    assert order[0] == "control", order
    print("✓ 控焦指令插到 5 個排隊影格前面")


async def test_set_position_coalesces():
    reset()
    async with running_worker():
        # 模擬拖 slider / iOS 高頻餵目標值
        results = await asyncio.gather(
            *[B.cam_set_position(p) for p in range(100, 1100, 100)]
        )
    assert CAM.set_log == [1000], CAM.set_log
    assert [r.applied for r in results] == [False] * 9 + [True], results
    print("✓ 連發 10 次位置設定只送 1 次 USB")


async def test_state_dir_follows_sudo_user():
    """sudo 下校準表要存進真正使用者的家目錄，不是 /var/root。

    bridge 平常都用 sudo 跑（macOS 要 root 才搶得到相機），如果照著
    Path.home() 走，校準資料會落在 root 家裡而使用者原本的那份看不見。
    """
    import os
    import pwd

    me = pwd.getpwuid(os.getuid())
    saved = os.environ.get("SUDO_USER")
    os.environ["SUDO_USER"] = me.pw_name
    try:
        resolved = B._resolve_state_dir()
    finally:
        if saved is None:
            os.environ.pop("SUDO_USER", None)
        else:
            os.environ["SUDO_USER"] = saved

    assert resolved == Path(me.pw_dir) / ".sigma_fp_bridge", resolved
    assert "/var/root" not in str(resolved), resolved
    print(f"✓ sudo 下校準目錄指向使用者家目錄 ({resolved})")


async def test_focus_range_read_and_clamped():
    """相機回報範圍時，超出範圍的位置要被修到邊界而不是原樣送出去。"""
    reset()
    async with running_worker():
        await B.refresh_focus_range()
        assert B.state.focus_range == (5974, 11116), B.state.focus_range

        low = await B.cam_set_position(100)      # 遠低於下限
        assert low.clamped and low.position == 5974, low
        assert CAM.set_log[-1] == 5974, CAM.set_log

        high = await B.cam_set_position(99999)   # 遠高於上限
        assert high.clamped and high.position == 11116, high

        ok = await B.cam_set_position(8000)      # 範圍內
        assert not ok.clamped and ok.position == 8000, ok
    print("✓ 焦點範圍讀取 + 超界修正")


async def test_no_range_means_no_clamping():
    """相機不回報範圍時不能亂夾 —— 寧可原樣送出去讓相機自己判斷。"""
    reset()
    CAM.focus_range = None
    async with running_worker():
        B.state.focus_range = None
        await B.refresh_focus_range()
        assert B.state.focus_range is None
        result = await B.cam_set_position(123456)
        assert not result.clamped and result.position == 123456, result
    CAM.focus_range = (5974, 11116)
    print("✓ 沒有範圍時不夾值")


async def test_range_refreshes_when_focal_length_changes():
    """變焦或換鏡頭會改變合法範圍，焦距一變就要重讀。"""
    reset()
    async with running_worker():
        await B.refresh_focus_range()
        poll = asyncio.create_task(B.state_polling_loop())
        try:
            # 模擬轉了變焦環 / 換了鏡頭
            CAM.focal_length = 105
            CAM.focus_range = (2000, 4000)
            deadline = time.monotonic() + 5
            while B.state.focus_range != (2000, 4000) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        finally:
            poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll
    assert B.state.focus_range == (2000, 4000), B.state.focus_range
    print("✓ 焦距改變時自動重讀範圍")


async def test_stale_frame_request_dropped():
    reset()
    async with running_worker() as w:
        slow = w.submit(lambda: time.sleep(0.15), priority=B.Priority.CONTROL)
        stale = w.submit(lambda: "frame", priority=B.Priority.LIVEVIEW, ttl=0.05)
        await slow
        result = await stale
    assert isinstance(result, B.JobSkipped) and result.reason == "expired", result
    print("✓ 排隊過久的影格請求被丟掉")


async def test_fails_fast_without_camera():
    reset()
    B.state.camera = None
    try:
        async with running_worker() as w:
            await w.submit(lambda: "nope")
    except B.CameraUnavailable:
        print("✓ 沒相機時 fail-fast")
    else:
        raise AssertionError("應該要丟 CameraUnavailable")
    finally:
        B.state.camera = CAM


async def test_liveview_survives_wait_idle():
    """重構針對的核心回歸：等馬達停止期間 live view 不能被凍住。"""
    reset()
    CAM.op_delay = 0.005
    CAM.idle_after_reads = 8  # 讀第 8 次才回報 Idle
    B.state.mjpeg_clients.add(object())  # 讓 liveview_loop 願意工作
    async with running_worker():
        producer = asyncio.create_task(B.liveview_loop())
        try:
            reached_idle = await B.cam_wait_idle(timeout_s=2.0, poll_interval_s=0.02)
        finally:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer
    CAM.op_delay = 0.0
    B.state.mjpeg_clients.clear()
    assert reached_idle, "應該要等到 Idle"
    assert CAM.count("frame") > 0, "等待期間一張影格都沒抓到 —— live view 被餓死了"
    print(f"✓ 等馬達期間仍抓到 {CAM.count('frame')} 張影格")


# ── FrameHub ────────────────────────────────────────────────────────────


async def test_slow_client_skips_frames():
    hub = B.FrameHub()
    fast, slow = [], []

    async def consume(out, delay, n=3):
        seq = hub.seq
        for _ in range(n):
            seq, frame = await hub.wait_for_next(seq)
            out.append(int(frame.decode()[1:]))
            await asyncio.sleep(delay)

    tasks = [asyncio.create_task(consume(fast, 0)),
             asyncio.create_task(consume(slow, 0.06))]
    await asyncio.sleep(0.01)
    for i in range(12):
        hub.publish(f"f{i}".encode())
        await asyncio.sleep(0.01)
    await asyncio.wait_for(asyncio.gather(*tasks), 5)

    assert all(b > a for a, b in zip(slow, slow[1:])), slow
    assert any(b - a > 1 for a, b in zip(slow, slow[1:])), f"慢 client 沒跳格: {slow}"
    print(f"✓ 慢 client 跳格取最新 {slow}（快 client {fast}）")


# ── 端對端 ───────────────────────────────────────────────────────────────


async def test_http_endpoints():
    reset()
    B.worker.start()
    assert await B.try_connect_camera()
    poll = asyncio.create_task(B.state_polling_loop())
    live = asyncio.create_task(B.liveview_loop())

    runner = web.AppRunner(B.make_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"

    try:
        async with aiohttp.ClientSession() as s:
            status = await (await s.get(f"{base}/api/status")).json()
            assert status["connected"] is True, status
            # 連線時就該把範圍讀進來並公開給 client
            assert status["focus_range"] == [5974, 11116], status

            body = await (await s.post(f"{base}/api/focus", json={"position": 8000})).json()
            assert body["applied"] and not body["clamped"], body
            assert CAM.position == 8000, CAM.position

            body = await (await s.post(f"{base}/api/focus", json={"position": 1})).json()
            assert body["clamped"] and body["position"] == 5974, body

            await s.post(f"{base}/api/calibration", json={"table": [[1.0, 100], [5.0, 900]]})
            body = await (await s.post(f"{base}/api/distance", json={"distance": 3.0})).json()
            assert body["ok"], body
            print("✓ REST：status / focus / distance / calibration")

            # 兩個 MJPEG client 必須共用影格，而不是各自去抓
            before = CAM.count("frame")

            async def read_frames(n=4):
                got = []
                async with s.get(f"{base}/liveview.mjpeg") as resp:
                    assert resp.status == 200
                    assert "multipart/x-mixed-replace" in resp.headers["Content-Type"]
                    buf = b""
                    while len(got) < n:
                        chunk = await resp.content.read(4096)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\xff\xd9" in buf:
                            end = buf.index(b"\xff\xd9") + 2
                            segment, buf = buf[:end], buf[end:]
                            got.append(segment[segment.index(b"\xff\xd8"):])
                return got

            a, b = await asyncio.wait_for(asyncio.gather(read_frames(), read_frames()), 15)
            pulled = CAM.count("frame") - before
            assert pulled < len(a) + len(b), f"影格沒共用：抓了 {pulled} 次給 {len(a) + len(b)} 張"
            print(f"✓ MJPEG：2 個 client 拿到 {len(a)}+{len(b)} 張，相機只抓了 {pulled} 次")

            # 斷線的 client 必須被回收，否則相機會一直為幽靈抓影格
            deadline = time.monotonic() + 5
            while B.state.mjpeg_clients and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            assert not B.state.mjpeg_clients, f"還剩 {len(B.state.mjpeg_clients)} 個"
            print("✓ 斷線的 MJPEG client 已回收")

            async with s.ws_connect(f"{base}/ws") as ws:
                hello = json.loads((await ws.receive()).data)
                assert hello["type"] == "hello", hello
                # UI 一連上就要知道範圍，不必等第一次狀態廣播
                assert hello["focus_range"] == [5974, 11116], hello
                await ws.send_str(json.dumps({"cmd": "set_position", "position": 9000, "id": 9}))
                deadline = time.monotonic() + 5
                ack = None
                while time.monotonic() < deadline:
                    msg = json.loads((await ws.receive()).data)
                    if msg.get("id") == 9:
                        ack = msg
                        break
                assert ack and ack["type"] == "ack", ack
                assert CAM.position == 9000, CAM.position
                print("✓ WebSocket set_position + hello 帶焦點範圍")

            B.state.camera = None
            resp = await s.get(f"{base}/api/focus")
            assert resp.status == 503, (resp.status, await resp.text())
            B.state.camera = CAM
            print("✓ 相機掉線時回 503 而不是 500")
    finally:
        for t in (poll, live):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await B.worker.stop()
        await runner.cleanup()


async def main():
    import logging
    logging.basicConfig(level=logging.ERROR)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            await fn()
    print("\ntest_bridge 全部通過")


if __name__ == "__main__":
    asyncio.run(main())
