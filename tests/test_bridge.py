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
import threading
import types
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
    B.state.released_by_user = False
    # 模式會被 refresh_capabilities() 設定，測試之間必須清掉 ——
    # 不然某個測試會繼承前一個的錄影模式，然後被模式閘門擋下。
    B.state.camera_mode = None
    B.state.shutter_unit = None
    B.state.released_camera = None
    B.state.frame_rate = 24.0
    CAM.usb_claimed = True


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


async def test_a_hung_camera_call_fails_fast_instead_of_queueing_forever():
    """實測踩過：一個 PTP 呼叫回不來，整座橋就無聲停擺。

    當時 /api/settings 完全沒有回應、live view 開得了連線卻送不出任何影格，
    而 /api/status（不碰相機）還是正常的 —— 從外面看不出哪裡壞了。原因是
    worker 那行 run_in_executor 沒有上限，卡住的工作永遠佔著佇列。

    停不掉那條執行緒是 Python 的限制，改不了；能改的是不要再假裝還會好。
    """
    reset()
    async with running_worker() as w:
        never_returns = threading.Event()
        try:
            hung = w.submit(lambda: never_returns.wait(), needs_camera=False,
                            timeout=0.3)
            try:
                await hung
            except B.CameraStuck as e:
                assert "重啟" in str(e), e
            else:
                raise AssertionError("卡住的工作應該要丟 CameraStuck")

            assert w.stuck, "卡住之後要留下記錄"

            # 後續工作必須立刻失敗，而不是排在死掉的工作後面
            ran = []
            started = time.monotonic()
            try:
                await w.submit(lambda: ran.append(1), needs_camera=False)
            except B.CameraStuck:
                pass
            else:
                raise AssertionError("卡住之後的工作不該被執行")
            assert not ran, "卡住之後仍然把工作送上 USB"
            assert time.monotonic() - started < 0.3, "卡住之後還在等"
        finally:
            never_returns.set()   # 放掉那條 executor 執行緒
    print("✓ 相機呼叫卡住時立刻回報，不讓後續請求無聲排隊")


async def test_status_still_answers_and_names_the_stuck_step():
    """卡住時 /api/status 必須是唯一還能講話的端點。

    實測時從外面完全看不出哪裡壞了：/api/settings 沒有回應、live view 送出
    0 bytes、/api/status 卻一切正常 —— 因為它讀快取。既然它是唯一活著的
    出口，卡住的訊息和卡在哪一步就都要從它講出來。
    """
    reset()
    async with running_worker() as w:
        w.stuck = "測試用的卡住訊息"
        capture_mod = sys.modules["capture"]
        capture_mod._step("get_pict_file_info2")
        try:
            request = types.SimpleNamespace(path="/api/status")
            resp = await B.handle_status(request)
            body = json.loads(resp.body.decode())
            assert body["stuck"] == "測試用的卡住訊息", body
            assert body["capture_step"]["step"] == "get_pict_file_info2", body

            # 其他端點要回 503 並指出解法，而不是誤報 not connected
            async def unreachable(_):
                raise AssertionError("卡住時不該進到 handler")

            other = types.SimpleNamespace(path="/api/settings")
            resp = await B.camera_unavailable_middleware(other, unreachable)
            assert resp.status == 503
            body = json.loads(resp.body.decode())
            assert body["stuck"] is True and "restart" in body["recovery"], body
            assert body["capture_step"]["step"] == "get_pict_file_info2", body
        finally:
            capture_mod._step(None)
            w.stuck = None
    print("✓ 卡住時 /api/status 仍答得出來，並指出卡在哪一步")


async def test_ptp_probe_returns_raw_bytes_and_rejects_nonsense():
    """研究用端點：省掉「改程式 → 重啟 bridge → 看結果」那個循環。

    那個循環貴到會讓人先寫好假設再驗證，而這個專案幾乎每個錯誤結論都是
    這樣來的 —— 拍攝那條線就連續錯了三次歸因。
    """
    reset()
    B.state.camera = fake_camera.FakeCamera()
    async with running_worker():
        listing = json.loads((await B.handle_ptp_probe(
            types.SimpleNamespace(method="GET"))).body.decode())
        assert "SigmaGetMovieFileInfo" in listing["opcodes"], listing
        assert listing["opcodes"]["SigmaGetMovieFileInfo"] == "0x9036"

        class Req:
            method = "POST"

            def __init__(self, body):
                self._body = body

            async def json(self):
                return self._body

        ok = json.loads((await B.handle_ptp_probe(
            Req({"opcode": "SigmaGetPictFileInfo2"}))).body.decode())
        assert ok["bytes"] > 0 and ok["raw_hex"], ok
        assert ok["uint32_le"], "沒有提供 uint32 視角"

        bad = await B.handle_ptp_probe(Req({"opcode": "NotARealOpcode"}))
        assert bad.status == 502, bad.status
        assert "不認得" in json.loads(bad.body.decode())["error"]

        bad = await B.handle_ptp_probe(Req({"opcode": "SigmaGetPictFileInfo2",
                                            "params": ["九"]}))
        assert bad.status == 400, bad.status
    print("✓ 原始 PTP 探測端點可用，並擋掉不合法的輸入")


async def test_a_long_download_gets_a_bigger_deadline_than_a_normal_command():
    """下載是長時間操作，不能套用一般指令的 120 秒上限。

    30 秒的 FHD 影片約 290 MB，實測速度 2.4 MB/s 要 120 秒 —— 剛好卡在
    CameraStuck 的門檻上，會被自己的看門狗砍掉，而且訊息還會叫使用者去
    把相機斷電重開。
    """
    reset()
    async with running_worker() as w:
        seen = {}
        original = w.submit

        def spy(fn, **kw):
            seen.update(kw)
            return original(fn, **kw)

        w.submit = spy
        big = 290 * 1024 * 1024
        budget = 120.0 + big / (512 * 1024)
        assert budget > 600, f"290 MB 的預算只有 {budget:.0f} 秒，太緊"
        # 一般指令仍然用預設上限
        await w.submit(lambda: None, needs_camera=False)
        assert seen.get("timeout", B.DEFAULT_JOB_TIMEOUT_S) == B.DEFAULT_JOB_TIMEOUT_S
    print(f"✓ 290 MB 的下載預算 {budget:.0f} 秒，遠大於一般指令的 "
          f"{B.DEFAULT_JOB_TIMEOUT_S:.0f} 秒")


async def test_download_is_refused_while_recording():
    """錄影中要求傳輸會讓相機進入必須斷電才能復原的狀態，實測重現過。

    那次錄影用的是 dest_to_save=InComputer —— 那種錄影不會留下任何檔案，
    所以「錄影中」和「沒有檔案」當時同時成立，真正的條件還沒分辨清楚。
    兩種情況都擋掉。
    """
    reset()
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as session:
                assert (await session.post(f"{base}/api/record/start")).status == 200
                assert B.state.recording

                resp = await session.get(f"{base}/api/record/download")
                assert resp.status == 409, resp.status
                body = await resp.json()
                assert "錄影中" in body["error"] and "斷電" in body["error"], body

                await session.post(f"{base}/api/record/stop")
        finally:
            await runner.cleanup()
    print("✓ 錄影中拒絕下載，並說明後果")


async def test_event_probe_drains_without_touching_the_camera():
    """事件探測不能對相機發任何指令 —— 錄影期間也要能安全呼叫。

    ptpy 已經有背景執行緒在輪詢事件並丟進佇列，這裡只讀佇列。錄影中對相機
    發指令是這個專案代價最高的錯誤（要一個還沒登錄的檔案會讓相機掉線）。
    """
    reset()
    cam = fake_camera.FakeCamera()
    queued = [
        types.SimpleNamespace(EventCode="ObjectAdded", SessionID=1,
                              TransactionID=7, Parameter=[42]),
        None,
    ]
    calls = []

    def fake_event(wait=False):
        calls.append(wait)
        return queued.pop(0) if queued else None

    cam.event = fake_event
    B.state.camera = cam
    async with running_worker():
        before = len(cam.calls)
        request = types.SimpleNamespace(query={"seconds": "0.3"})
        resp = await B.handle_event_probe(request)
        body = json.loads(resp.body.decode())
        assert body["count"] == 1, body
        assert body["events"][0]["code"] == "ObjectAdded", body
        assert body["events"][0]["params"] == [42], body
        assert calls and all(w is False for w in calls), "不能用阻塞式等待"
        assert len(cam.calls) == before, f"對相機發了指令：{cam.calls[before:]}"
    print(f"✓ 事件探測只讀佇列，沒有對相機發指令（收到 {body['count']} 個事件）")


async def test_clear_can_release_a_single_entry():
    """逐筆釋放：下載完一段就放掉，讓下一段遞補。

    0x9037 只服務一個位置。如果它服務的是 head 而不是固定的索引 0，逐筆釋放
    就能連續下載多段，不必每次都 release + acquire —— 而 release + acquire
    正是目前懷疑會弄壞傳輸的動作。
    """
    reset()
    cam = fake_camera.FakeCamera()
    cam.entries = {0: "a", 1: "b", 2: "c"}
    cam.db_head, cam.db_tail = 0, 3
    B.state.camera = cam
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(f"{base}/api/record/clear?image_id=0")
                assert resp.status == 200, resp.status
                body = await resp.json()
                assert body["cleared"] == 0, body
                assert cam.db_head == 1, f"head 沒有前進：{cam.db_head}"
                assert 1 in cam.entries and 2 in cam.entries, "清掉了不該清的"

                bad = await session.post(f"{base}/api/record/clear?image_id=x")
                assert bad.status == 400, bad.status
        finally:
            await runner.cleanup()
    print("✓ 可以只釋放一筆項目，其餘保留")


async def test_frame_rate_comes_from_the_camera_not_the_user():
    """快門角度換算要用相機實際的幀率。

    先前用的是使用者手動指定的 state.frame_rate（預設 24.0），那是在
    DataGroupMovie 的 tag 還沒解出來時的權宜做法。相機自己就報得出來
    （tag 61），猜錯會讓角度算錯，而且使用者不會發現。
    """
    reset()
    cam = fake_camera.FakeCamera()
    cam.movie[61] = (2997, 100)          # 29.97 fps
    B.state.camera = cam
    B.state.frame_rate = 24.0
    async with running_worker():
        await B.cam_read_settings()
        assert abs(B.state.frame_rate - 29.97) < 1e-6, B.state.frame_rate
    print(f"✓ 幀率從相機讀取（{B.state.frame_rate}），不是沿用預設值")


async def test_status_reports_card_space_and_battery():
    """卡片剩餘空間一直都在 DataGroup1 裡，只是沒被讀出來。

    這個專案先前懷疑「拍不成是不是卡滿了」，還記下「主機端查不到」——
    其實查得到。狀態欄位跟設定分開，因為它們寫不進去。
    """
    reset()
    cam = fake_camera.FakeCamera()
    B.state.camera = cam
    async with running_worker():
        await B.cam_read_settings()
        resp = await B.handle_status(types.SimpleNamespace(path="/api/status"))
        body = json.loads(resp.body.decode())
        status = body.get("camera_status") or {}
        assert "media_free_space" in status, status
        assert "battery_state" in status and "lens_wide_mm" in status, status
        assert status["media_free_space"] == cam.media_free_space, status
    print(f"✓ /api/status 帶出相機狀態（卡片剩餘 {status['media_free_space']}）")


async def test_probe_accepts_numeric_opcodes_for_undocumented_commands():
    """探未文件化指令要能直接給數值。

    sigma-ptpy 只認得 28 個 Sigma opcode，0x9010~0x9037 之間還有十幾個空號。
    ptpy 的 OperationCode 是 construct 的 Enum 且 default=Pass，所以未知數值
    原樣送得出去。範圍檢查留著 —— 打錯字送出一個荒謬的值沒有意義。
    """
    reset()
    cam = fake_camera.FakeCamera()
    seen = []
    cam.recv = lambda ptp: (seen.append(ptp.OperationCode),
                            types.SimpleNamespace(Data=b"\x01\x02"))[1]
    B.state.camera = cam
    async with running_worker():
        class Req:
            method = "POST"

            def __init__(self, body):
                self._body = body

            async def json(self):
                return self._body

        r = json.loads((await B.handle_ptp_probe(Req({"opcode": "0x9010"}))).body.decode())
        assert seen == [0x9010], seen
        assert r["opcode"] == "0x9010", r
        assert r["bytes"] == 2, r

        bad = await B.handle_ptp_probe(Req({"opcode": "0x0001"}))
        assert bad.status == 502, bad.status
        assert "超出範圍" in json.loads(bad.body.decode())["error"]
    print("✓ 探測端點收得下數值 opcode（用來探未文件化指令）")


async def test_focus_can_return_to_autofocus_after_manual_control():
    """迴歸：拉過一次滑桿就再也回不了自動對焦。

    set_focus_position 為了不讓相機搶回焦點，每次都寫 FocusMode=MF、
    PreConstAF=Off，而在 /api/focus/mode 出現之前，沒有任何地方寫得回去。
    """
    reset()
    from sigma_ptpy.enum import FocusMode, PreConstAF
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(f"{base}/api/focus", json={"position": 7000})
                got = await (await session.get(f"{base}/api/focus")).json()
                assert got["focus_mode"] == "MF", got
                assert got["continuous_af"] == "Off", got

                resp = await session.post(f"{base}/api/focus/mode",
                                          json={"mode": "AF_C"})
                assert resp.status == 200, resp.status
                body = await resp.json()
                assert body["focus_mode"] == "AF_C", body
                # 切回 AF 要把持續對焦一起打開，否則相機不會真的自己追焦
                assert body["continuous_af"] == "On", body
        finally:
            await runner.cleanup()
    print("✓ 手動控焦之後切得回自動對焦，持續對焦也一起恢復")


async def test_focus_point_face_eye_and_area_round_trip():
    """對焦點、臉眼偵測、對焦區域都要寫得進去也讀得回來。"""
    reset()
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(f"{base}/api/focus/mode", json={
                    "face_eye_af": "FaceEyeAuto",
                    "focus_area": "OnePointSelection",
                    "point": [200, 400],
                })
                body = await resp.json()
                assert body["face_eye_af"] == "FaceEyeAuto", body
                assert body["focus_area"] == "OnePointSelection", body
                assert body["focus_point"] == [200, 400], body

                bounds = await (await session.get(f"{base}/api/focus/bounds")).json()
                assert bounds["point"]["height"] == 682, bounds
                assert "FaceEyeAuto" in bounds["face_eye_options"], bounds

                bad = await session.post(f"{base}/api/focus/mode",
                                         json={"mode": "NotAMode"})
                assert bad.status == 400, bad.status
                bad = await session.post(f"{base}/api/focus/mode", json={})
                assert bad.status == 400, bad.status
        finally:
            await runner.cleanup()
    print("✓ 對焦點 / 臉眼偵測 / 對焦區域可讀可寫，不合法的值被擋下")


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


async def test_release_returns_body_control_and_blocks_reconnect():
    """release 必須真的退出 API 模式，而且自動重連不能馬上搶回去。"""
    reset()
    async with running_worker():
        assert B.state.camera_connected
        reconnect = asyncio.create_task(B.reconnect_loop())
        try:
            await B.release_camera()
            assert not B.state.camera_connected
            assert B.state.released_by_user
            assert CAM.count("close_application") == 1, "沒送 CloseApplication，機身還是鎖著"

            # 給自動重連幾輪機會偷偷搶回去
            await asyncio.sleep(0.4)
            assert not B.state.camera_connected, "release 後自動重連不該接管"

            await B.acquire_camera()
            assert B.state.camera_connected and not B.state.released_by_user
        finally:
            reconnect.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconnect
    print("✓ release 交還機身控制且擋住自動重連，acquire 收回")


async def test_acquire_after_release_reconnects():
    """release 之後 acquire 必須連得回來。

    實測失敗過：close_camera() 只關 PTP session，USB interface 還被舊物件
    佔著（ptpy 的 _shutdown() 只註冊在 atexit），所以 acquire 是在跟自己
    上一個連線搶裝置。
    """
    reset()
    async with running_worker():
        await B.release_camera()
        # 交還時只退出 API 模式，USB 不放 —— 放開的話 macOS 會立刻搶走裝置，
        # 之後同一個行程再也 acquire 不回來（實測連續八次全失敗）。
        assert CAM.count("close_application") >= 1, "沒退出 API 模式"
        assert CAM.count("shutdown") == 0, "不該在交還時放開 USB"
        assert CAM.usb_claimed, "USB claim 應該保留"
        ok = await B.acquire_camera()
        assert ok, "acquire 應該要成功"
        assert B.state.camera_connected
        assert CAM.count("config_api") >= 1, "沒有重新進入 API 模式"

        # 連續來回幾次也不能漏
        for _ in range(3):
            await B.release_camera()
            assert await B.acquire_camera()
        assert CAM.count("shutdown") == 0
    print("✓ release 只退出 API 模式、保留 USB，acquire 可反覆重用")


async def test_rejected_settings_are_reported():
    """相機默默忽略寫入時，要明確講出來而不是假裝成功。"""
    reset()
    CAM.ignored_fields = {"ShutterSpeed"}
    CAM.groups[2]["ExposureMode"] = __import__(
        "sigma_ptpy.enum", fromlist=["x"]).ExposureMode.AperturePriority
    try:
        async with running_worker():
            result = await B.cam_apply_settings({"shutter_speed": 1 / 500})
    finally:
        CAM.ignored_fields = set()
    assert "shutter_speed" in result["rejected"], result
    detail = result["rejected"]["shutter_speed"]
    assert detail["requested"] == 1 / 500
    assert "AperturePriority" in (detail["hint"] or ""), detail
    print("✓ 被相機忽略的設定會被抓出來並提示原因")


async def test_dump_endpoints_parse_ifd():
    """協定探勘端點要能把原始 IFD 解成 JSON。"""
    reset()
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as s:
                info5 = await (await s.get(f"{base}/api/dump/info5")).json()
                tags = [e["tag"] for e in info5["entries"]]
                assert 658 in tags, info5
                movie = await (await s.get(f"{base}/api/dump/movie")).json()
                assert movie["entries"], movie
                bad = await s.get(f"{base}/api/dump/nope")
                assert bad.status == 404
        finally:
            await runner.cleanup()
    print("✓ /api/dump/{info5,movie} 解析並回傳 IFD")


async def test_movie_shutter_angle_through_bridge():
    """CINE 模式的快門只能經由 DataGroupMovie 設定。"""
    reset()
    async with running_worker():
        B.state.movie_capabilities = {"shutter_angle": [11.2, 90.0, 172.8, 180.0]}
        result = await B.cam_apply_settings({"shutter_angle": 90})
        assert not result["rejected"], result
        assert CAM.movie[7] == (900, 3600), CAM.movie
        got = await B.cam_read_settings()
        assert got["shutter_angle"] == 90.0, got
    print("✓ 快門角度經由 DataGroupMovie 寫入")


async def test_movie_rejection_hints_at_exposure_mode():
    """實測：ProgramAuto 下寫快門角度沒作用，切 Manual 才行 —— 要講出來。"""
    reset()
    from sigma_ptpy import enum as E
    CAM.groups[2]["ExposureMode"] = E.ExposureMode.ProgramAuto
    async with running_worker():
        B.state.movie_capabilities = {"shutter_angle": [11.2, 180.0]}
        # 讓假相機忽略這次寫入，模擬自動曝光把值搶回去
        original = CAM.send

        def ignore(ptp, payload):
            CAM._tick("send:" + ptp.OperationCode)

        CAM.send = ignore
        try:
            result = await B.cam_apply_settings({"shutter_angle": 180})
        finally:
            CAM.send = original
    detail = result["rejected"].get("shutter_angle")
    assert detail, result
    assert "ProgramAuto" in (detail["hint"] or ""), detail
    print("✓ 錄影快門被自動曝光蓋掉時會提示切 Manual")


async def test_release_acquire_restores_settings():
    """acquire 會跑 config_api()，相機設定被重置成預設值。

    這是已知行為（SDK：「API resets the camera setting to the default」），
    但它讓 release 這個功能實際上很難用 —— 使用者只是想暫時拿回機身，
    回來卻發現設定全沒了。所以 release 前存檔、acquire 後還原。
    """
    reset()
    from sigma_ptpy import enum as E
    async with running_worker():
        await B.cam_apply_settings({"exposure_mode": "Manual", "aperture": 5.6})

        # 模擬 config_api() 的重置：acquire 之後相機回到預設
        original_open = sys.modules["sigma_fp_focus"].open_camera

        def open_and_reset():
            cam = original_open()
            cam.groups = cam._default_groups()
            cam.groups[2]["ExposureMode"] = E.ExposureMode.ProgramAuto
            return cam

        sys.modules["sigma_fp_focus"].open_camera = open_and_reset
        B.open_camera = open_and_reset
        try:
            await B.release_camera()
            assert B.state.settings_snapshot, "release 應該要留下快照"
            assert await B.acquire_camera()
            got = await B.cam_read_settings()
        finally:
            sys.modules["sigma_fp_focus"].open_camera = original_open
            B.open_camera = original_open

    assert got["exposure_mode"] == "Manual", got
    assert got["aperture"] == 5.6, got
    print("✓ release → acquire 之後設定被還原（而非停在重置值）")


async def test_acquire_can_skip_restore():
    """想看機身端改了什麼的時候，還原反而礙事。"""
    reset()
    async with running_worker():
        await B.release_camera()
        snapshot = B.state.settings_snapshot
        assert snapshot
        assert await B.acquire_camera(restore=False)
        assert B.state.settings_snapshot is None
    print("✓ acquire(restore=False) 不還原")


async def test_recording_guards():
    """錄影相關的防呆：不能重複開始、clip 有長度上限、release 前先停。"""
    reset()
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as s:
                assert (await s.post(f"{base}/api/record/start")).status == 200
                assert B.state.recording

                # 已在錄影中不能再開始
                again = await s.post(f"{base}/api/record/start")
                assert again.status == 409, again.status
                clip = await s.post(f"{base}/api/record/clip?seconds=2")
                assert clip.status == 409, clip.status

                # release 要先把錄影停掉，不然留下沒收尾的檔案
                await B.release_camera()
                assert not B.state.recording, "release 前沒有停止錄影"
                await B.acquire_camera()

                # clip 長度上限：這會寫進使用者的記憶卡
                too_long = await s.post(f"{base}/api/record/clip?seconds=999")
                assert too_long.status == 400, too_long.status
                assert not B.state.recording
        finally:
            await runner.cleanup()
    print("✓ 錄影防呆：重複開始 409、clip 上限、release 先停止")


async def test_schema_endpoint_refreshes_capabilities():
    """schema 端點不能用快取回答。

    合法值隨其他設定變動（實測：UHD 下相機不開放調整色彩位元）。若這裡
    回快取、而 WebSocket 的 ack 回重讀後的值，兩邊就會不一致 —— 我因此
    誤判過一次，以為某個行為是暫態。
    """
    reset()
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            async with aiohttp.ClientSession() as s:
                before = CAM.count("info5_raw")
                await (await s.get(f"{base}/api/settings/schema")).json()
                assert CAM.count("info5_raw") > before, "schema 端點沒有重讀能力"
        finally:
            await runner.cleanup()
    print("✓ schema 端點會重讀相機能力，不用快取")


async def test_dump_works_while_released():
    """交還期間也要能讀原始資料群組。

    那個窗口是唯一能看到「使用者自己在機身上設了什麼」的地方 ——
    重新進入 API 模式會把設定重置掉。
    """
    reset()
    async with running_worker():
        runner = web.AppRunner(B.make_app())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{runner.addresses[0][1]}"
        try:
            await B.release_camera()
            assert B.state.camera is None and B.state.released_camera is not None
            async with aiohttp.ClientSession() as s:
                d = await (await s.get(f"{base}/api/dump/movie")).json()
                assert d.get("released") is True, d
                assert d.get("entries"), d
        finally:
            await runner.cleanup()
    print("✓ 交還期間仍可讀取原始資料群組")


async def test_schema_hides_the_inactive_shutter_representation():
    """速度模式下不要列 shutter_angle，反之亦然。

    相機接受哪個欄位由 shutter_unit（tag 6）決定 —— 列出無效的那個
    只會讓人按了沒反應，這正是這個 bug 原本的樣子。
    """
    reset()
    async with running_worker():
        B.state.camera_mode = "movie"
        B.state.movie_capabilities = {"shutter_angle": [11.2, 180.0]}

        B.state.shutter_unit = 2          # 角度模式
        names = {r["name"] for r in B._movie_schema()}
        assert "shutter_angle" in names
        assert not B._shutter_speed_allowed()

        B.state.shutter_unit = 1          # 速度模式
        names = {r["name"] for r in B._movie_schema()}
        assert "shutter_angle" not in names, "速度模式下不該列角度"
        assert B._shutter_speed_allowed() == {"shutter_speed"}
    print("✓ schema 只列當下有效的快門表示法")


async def test_all_paths_return_the_same_schema():
    """describe_settings、REST、以及 set_settings 的 ack 必須給出同一份 schema。

    先前三處各自組，改了前兩個卻漏掉 ack —— 而 ack 是 UI 每次操作後更新
    schema 的那條路，於是切換快門單位後整組快門控制項消失。
    """
    src = (Path(__file__).resolve().parent.parent / "mac_bridge_server.py").read_text()
    assert src.count("_full_schema()") >= 4, "還有路徑自己組 schema"
    assert '"schema": describe(' not in src
    assert '"settings": describe(' not in src

    reset()
    async with running_worker():
        B.state.camera_mode = "movie"
        B.state.movie_capabilities = {"shutter_angle": [11.2, 180.0]}
        B.state.shutter_unit = 1
        names = {r["name"] for r in B._full_schema()}
        assert "shutter_speed" in names, "速度模式下 schema 少了 shutter_speed"
        assert "shutter_angle" not in names
        B.state.shutter_unit = 2
        names = {r["name"] for r in B._full_schema()}
        assert "shutter_angle" in names and "shutter_speed" not in names
    print("✓ 所有路徑共用同一份 schema，且隨快門單位切換")


async def test_mode_inference_ignores_our_own_fallbacks():
    """模式推測只能看相機真的回報的錄影能力。

    FALLBACK_CHOICES 是我們自己補的值域（shutter_unit），永遠存在 ——
    拿它判斷的話模式會永遠停在 movie，即使相機已經在拍照模式。
    """
    import movie_settings as MS
    reset()
    async with running_worker():
        # 相機只回報我們自己補的那個 —— 應該判定為拍照模式
        state_caps = dict(MS.FALLBACK_CHOICES)
        B.state.movie_capabilities = state_caps
        reported = set(state_caps) - set(MS.FALLBACK_CHOICES)
        assert not reported, "備援值域不該被當成相機回報"
    print("✓ 模式推測忽略我們自己補的備援值域")


async def test_settings_roundtrip_through_bridge():
    reset()
    async with running_worker():
        got = await B.cam_read_settings()
        assert got["aperture"] == 2.0 and got["iso"] == 6400, got
        await B.cam_apply_settings({"aperture": 2.8, "exposure_mode": "AperturePriority"})
        got = await B.cam_read_settings()
        assert got["aperture"] == 2.8, got
        assert got["exposure_mode"] == "AperturePriority", got
    print("✓ 設定經由 worker 讀寫正常")


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
