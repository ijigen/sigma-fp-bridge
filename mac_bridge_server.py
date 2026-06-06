#!/usr/bin/env python3
"""
Sigma fp Mac Bridge Server
============================

Mac 端的橋接 server。把 Sigma fp 的 USB PTP 控制包成：
  - WebSocket：低延遲即時控焦（給 iOS / iPhone app 用）
  - HTTP REST：query / 設定 / 校準表（給瀏覽器測試或別的 client）
  - MJPEG 串流：fp 的 live view（給 monitor / iPhone 看畫面）
  - Bonjour 廣告：iOS 能自動發現

執行：
    python3 mac_bridge_server.py

瀏覽器測試：
    http://localhost:8765/

iPhone：
    搜尋 _sigmafp._tcp Bonjour service 自動找到此 server

依賴：
    pip install sigma-ptpy aiohttp aiohttp-jinja2 zeroconf

相容性：
    macOS 12+, Python 3.10+
    fp 韌體 5.00+ / fp L 韌體 3.00+，USB Mode 設為 PTP
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import socket
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

# 引入 PoC 裡的 patch 跟基礎 helpers
sys.path.insert(0, str(Path(__file__).parent))
from sigma_fp_focus import (
    open_camera,
    close_camera,
    get_focus_state,
    set_focus_position,
    wait_focus_idle,
    distance_to_position,
    CamDataGroupFocusExt,
)
from sigma_ptpy.enum import FocusMode

# ─────────────────────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────────────────────

PORT = 8765
SERVICE_TYPE = "_sigmafp._tcp.local."
SERVICE_NAME = "Sigma fp Bridge"
STATE_DIR = Path.home() / ".sigma_fp_bridge"
CALIBRATION_FILE = STATE_DIR / "calibration.json"
LIVE_VIEW_INTERVAL_S = 0.04  # ~25fps live view stream
STATE_BROADCAST_INTERVAL_S = 0.1  # 10Hz state push to clients

log = logging.getLogger("sigma-bridge")


# ─────────────────────────────────────────────────────────────────────────────
# Server 狀態
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BridgeState:
    """中央 server 狀態。"""

    camera: object | None = None
    camera_lock: asyncio.Lock = None  # 改在 main() 內初始化（避開 module-load 時沒 event loop）
    ws_clients: set[web.WebSocketResponse] = field(default_factory=set)
    mjpeg_clients: set[web.StreamResponse] = field(default_factory=set)
    calibration: list[tuple[float, int]] = field(default_factory=list)
    active_lens_id: str = "default"
    last_focus_position: int | None = None
    last_focus_state: int | None = None
    last_lens_focal_mm: int | None = None
    camera_connected: bool = False
    reconnect_task: asyncio.Task | None = None
    last_focus_mode: str | None = None  # 顯示 AF/MF/AF_S 等狀態


state = BridgeState()


# ─────────────────────────────────────────────────────────────────────────────
# 校準表持久化
# ─────────────────────────────────────────────────────────────────────────────

def load_calibration() -> dict[str, list[list]]:
    """從 ~/.sigma_fp_bridge/calibration.json 讀所有鏡頭的校準表。"""
    if not CALIBRATION_FILE.exists():
        return {}
    try:
        return json.loads(CALIBRATION_FILE.read_text())
    except Exception as e:
        log.warning(f"校準檔讀取失敗：{e}")
        return {}


def save_calibration(all_tables: dict[str, list[list]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CALIBRATION_FILE.write_text(json.dumps(all_tables, indent=2))


def get_active_calibration() -> list[tuple[float, int]]:
    return state.calibration


def set_active_calibration(table: list[tuple[float, int]]) -> None:
    state.calibration = sorted(table)
    all_tables = load_calibration()
    all_tables[state.active_lens_id] = [list(x) for x in state.calibration]
    save_calibration(all_tables)


# ─────────────────────────────────────────────────────────────────────────────
# 相機操作（包成 async wrapper 以便配合 asyncio）
# ─────────────────────────────────────────────────────────────────────────────

async def run_in_executor(fn, *args, **kwargs):
    """sigma-ptpy 是同步 API，包成 executor 才不會擋 event loop。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


async def cam_get_focus() -> CamDataGroupFocusExt:
    async with state.camera_lock:
        return await run_in_executor(get_focus_state, state.camera)


async def cam_set_position(position: int) -> None:
    """設定焦點位置並 readback 確認。DEBUG 模式會印詳細狀態。"""
    async with state.camera_lock:
        await run_in_executor(set_focus_position, state.camera, position)
        # 寫入後立刻 readback 看相機接到什麼狀態
        try:
            f = await run_in_executor(get_focus_state, state.camera)
            log.info(
                f"set_position({position}) → readback: "
                f"FocusMode={f.FocusMode}, FocusPosition={f.FocusPosition}, "
                f"FocusState={f.FocusState}"
            )
        except Exception as e:
            log.warning(f"set 後 readback 失敗：{e}")


async def cam_wait_idle(timeout_s: float = 2.0) -> bool:
    async with state.camera_lock:
        return await run_in_executor(wait_focus_idle, state.camera, timeout_s, 0.05)


async def cam_get_view_frame() -> bytes | None:
    """JPEG live view 影格。sigma-ptpy 回傳 ViewFrame 物件，要取 .Data。"""
    async with state.camera_lock:
        try:
            frame = await run_in_executor(state.camera.get_view_frame)
            return frame.Data if frame is not None else None
        except Exception as e:
            log.debug(f"live view 取得失敗：{e}")
            return None


async def cam_get_datagroup1():
    async with state.camera_lock:
        return await run_in_executor(state.camera.get_cam_data_group1)


async def try_connect_camera() -> bool:
    """嘗試連線到相機，成功回 True。"""
    try:
        state.camera = await run_in_executor(open_camera)
        state.camera_connected = True
        log.info("相機已連線")
        await broadcast_state({"event": "camera_connected"})
        return True
    except Exception as e:
        log.warning(f"連不上相機：{e}")
        state.camera = None
        state.camera_connected = False
        return False


async def reconnect_loop():
    """背景任務：如果相機斷線，每 3 秒嘗試重連。"""
    while True:
        if not state.camera_connected:
            await try_connect_camera()
        await asyncio.sleep(3)


# ─────────────────────────────────────────────────────────────────────────────
# 廣播給所有 WS clients
# ─────────────────────────────────────────────────────────────────────────────

async def broadcast(message: dict) -> None:
    payload = json.dumps(message)
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_str(payload)
        except ConnectionResetError:
            dead.append(ws)
    for ws in dead:
        state.ws_clients.discard(ws)


async def broadcast_state(extra: dict | None = None) -> None:
    msg = {
        "type": "state",
        "ts": time.time(),
        "connected": state.camera_connected,
        "focus_position": state.last_focus_position,
        "focus_state": state.last_focus_state,
        "focus_mode": state.last_focus_mode,
        "focal_length_mm": state.last_lens_focal_mm,
        "active_lens_id": state.active_lens_id,
        "calibration_points": len(state.calibration),
    }
    if extra:
        msg.update(extra)
    await broadcast(msg)


async def state_polling_loop():
    """背景任務：定期讀相機狀態廣播給所有 client。"""
    while True:
        if state.camera_connected:
            try:
                f = await cam_get_focus()
                state.last_focus_position = f.FocusPosition
                state.last_focus_state = f.FocusState
                state.last_focus_mode = (
                    f.FocusMode.name if f.FocusMode is not None else None
                )
                g1 = await cam_get_datagroup1()
                state.last_lens_focal_mm = g1.CurrentLensFocalLength
                await broadcast_state()
            except Exception as e:
                log.warning(f"狀態讀取失敗：{e}")
                state.camera_connected = False
                await broadcast_state({"event": "camera_disconnected"})
        await asyncio.sleep(STATE_BROADCAST_INTERVAL_S)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    state.ws_clients.add(ws)
    log.info(f"WS client 連入（共 {len(state.ws_clients)}）")

    # 立即推送目前狀態
    await ws.send_str(json.dumps({
        "type": "hello",
        "server": "sigma-fp-bridge/0.1",
        "connected": state.camera_connected,
        "active_lens_id": state.active_lens_id,
    }))

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    req = json.loads(msg.data)
                    response = await handle_ws_command(req)
                    if response:
                        await ws.send_str(json.dumps(response))
                except Exception as e:
                    log.exception("WS command 失敗")
                    await ws.send_str(json.dumps({
                        "type": "error",
                        "error": str(e),
                    }))
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.warning(f"WS error: {ws.exception()}")
    finally:
        state.ws_clients.discard(ws)
        log.info(f"WS client 離開（剩 {len(state.ws_clients)}）")
    return ws


async def handle_ws_command(req: dict) -> dict | None:
    """支援的命令：
        {"cmd": "set_position", "position": <int>}
        {"cmd": "set_distance",  "distance": <float meters>}
        {"cmd": "get_state"}
        {"cmd": "calibration_add", "distance": <float>, "position": <int>}
        {"cmd": "calibration_clear"}
        {"cmd": "calibration_get"}
        {"cmd": "set_active_lens", "lens_id": <str>}
    """
    cmd = req.get("cmd")
    request_id = req.get("id")

    if not state.camera_connected and cmd != "get_state":
        return {"type": "error", "id": request_id, "error": "camera not connected"}

    if cmd == "set_position":
        pos = int(req["position"])
        await cam_set_position(pos)
        return {"type": "ack", "id": request_id, "position": pos}

    if cmd == "set_distance":
        if not state.calibration:
            return {"type": "error", "id": request_id, "error": "no calibration"}
        d = float(req["distance"])
        pos = distance_to_position(state.calibration, d)
        await cam_set_position(pos)
        return {"type": "ack", "id": request_id, "distance": d, "position": pos}

    if cmd == "get_state":
        f = await cam_get_focus() if state.camera_connected else None
        return {
            "type": "state_response",
            "id": request_id,
            "connected": state.camera_connected,
            "focus_position": getattr(f, "FocusPosition", None),
            "focus_state": getattr(f, "FocusState", None),
            "focus_mode": str(getattr(f, "FocusMode", None)),
        }

    if cmd == "calibration_add":
        d = float(req["distance"])
        p = int(req["position"])
        new_table = list(state.calibration) + [(d, p)]
        set_active_calibration(new_table)
        return {"type": "ack", "id": request_id, "calibration_points": len(state.calibration)}

    if cmd == "calibration_clear":
        set_active_calibration([])
        return {"type": "ack", "id": request_id, "calibration_points": 0}

    if cmd == "calibration_get":
        return {
            "type": "calibration",
            "id": request_id,
            "active_lens_id": state.active_lens_id,
            "table": [list(x) for x in state.calibration],
        }

    if cmd == "set_active_lens":
        lens_id = str(req["lens_id"])
        state.active_lens_id = lens_id
        all_tables = load_calibration()
        state.calibration = [tuple(x) for x in all_tables.get(lens_id, [])]
        return {
            "type": "ack",
            "id": request_id,
            "active_lens_id": lens_id,
            "calibration_points": len(state.calibration),
        }

    return {"type": "error", "id": request_id, "error": f"unknown cmd: {cmd}"}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP REST handlers（給瀏覽器或非 WS client 用）
# ─────────────────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    html_file = Path(__file__).parent / "static" / "index.html"
    if html_file.exists():
        return web.FileResponse(html_file)
    return web.Response(text="static/index.html not found", status=404)


async def handle_status(request: web.Request) -> web.Response:
    return web.json_response({
        "connected": state.camera_connected,
        "focus_position": state.last_focus_position,
        "focus_state": state.last_focus_state,
        "focal_length_mm": state.last_lens_focal_mm,
        "active_lens_id": state.active_lens_id,
        "calibration_points": len(state.calibration),
        "ws_clients": len(state.ws_clients),
    })


async def handle_focus_get(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    f = await cam_get_focus()
    return web.json_response({
        "focus_position": f.FocusPosition,
        "focus_state": f.FocusState,
        "focus_mode": str(f.FocusMode),
    })


async def handle_focus_post(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    data = await request.json()
    pos = int(data["position"])
    await cam_set_position(pos)
    return web.json_response({"ok": True, "position": pos})


async def handle_distance_post(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    if not state.calibration:
        return web.json_response({"error": "no calibration"}, status=400)
    data = await request.json()
    d = float(data["distance"])
    pos = distance_to_position(state.calibration, d)
    await cam_set_position(pos)
    return web.json_response({"ok": True, "distance": d, "position": pos})


async def handle_calibration_get(request: web.Request) -> web.Response:
    return web.json_response({
        "active_lens_id": state.active_lens_id,
        "table": [list(x) for x in state.calibration],
    })


async def handle_calibration_post(request: web.Request) -> web.Response:
    data = await request.json()
    table = [(float(d), int(p)) for d, p in data["table"]]
    set_active_calibration(table)
    return web.json_response({"ok": True, "calibration_points": len(state.calibration)})


# ─────────────────────────────────────────────────────────────────────────────
# Live view MJPEG stream
# ─────────────────────────────────────────────────────────────────────────────

async def handle_liveview(request: web.Request) -> web.StreamResponse:
    """MJPEG 串流：瀏覽器 <img src="/liveview.mjpeg"> 就能看。"""
    boundary = "frame"
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={
            "Content-Type": f"multipart/x-mixed-replace; boundary={boundary}",
            "Cache-Control": "no-cache, private",
            "Pragma": "no-cache",
        },
    )
    await resp.prepare(request)
    state.mjpeg_clients.add(resp)
    log.info(f"MJPEG client 連入（共 {len(state.mjpeg_clients)}）")

    try:
        while True:
            if not state.camera_connected:
                await asyncio.sleep(0.5)
                continue
            jpeg = await cam_get_view_frame()
            if jpeg:
                chunk = (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n"
                ).encode() + jpeg + b"\r\n"
                try:
                    await resp.write(chunk)
                except ConnectionResetError:
                    break
            await asyncio.sleep(LIVE_VIEW_INTERVAL_S)
    finally:
        state.mjpeg_clients.discard(resp)
        log.info(f"MJPEG client 離開（剩 {len(state.mjpeg_clients)}）")
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Bonjour / mDNS 廣告
# ─────────────────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    """挑一個有效的 LAN IP（不是 127.0.0.1）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


async def advertise_bonjour() -> tuple[AsyncZeroconf, ServiceInfo]:
    local_ip = get_local_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{SERVICE_NAME}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)],
        port=PORT,
        properties={
            b"ws": b"/ws",
            b"liveview": b"/liveview.mjpeg",
            b"version": b"0.1",
        },
        server=f"{socket.gethostname()}.local.",
    )
    zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    await zc.async_register_service(info)
    log.info(f"Bonjour 廣告：{SERVICE_NAME} @ {local_ip}:{PORT}")
    return zc, info


# ─────────────────────────────────────────────────────────────────────────────
# Application factory + main
# ─────────────────────────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/focus", handle_focus_get)
    app.router.add_post("/api/focus", handle_focus_post)
    app.router.add_post("/api/distance", handle_distance_post)
    app.router.add_get("/api/calibration", handle_calibration_get)
    app.router.add_post("/api/calibration", handle_calibration_post)
    app.router.add_get("/liveview.mjpeg", handle_liveview)
    return app


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    # 在 event loop 內建 lock，避免「Future attached to different loop」
    state.camera_lock = asyncio.Lock()

    # 把 ptpy 那堆 EvtPolling timeout 噪音降級為 DEBUG（不顯示）
    logging.getLogger("ptpy.transports.usb").setLevel(logging.CRITICAL)

    # 載入校準表
    all_tables = load_calibration()
    state.calibration = [tuple(x) for x in all_tables.get(state.active_lens_id, [])]
    log.info(f"已載入 {len(state.calibration)} 個校準點（鏡頭 ID: {state.active_lens_id}）")

    # 試連相機
    await try_connect_camera()
    state.reconnect_task = asyncio.create_task(reconnect_loop())
    polling_task = asyncio.create_task(state_polling_loop())

    # Bonjour
    zc, info = await advertise_bonjour()

    # 啟 HTTP server
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    local_ip = get_local_ip()
    log.info(f"")
    log.info(f"  瀏覽器測試:   http://{local_ip}:{PORT}/")
    log.info(f"  WebSocket:    ws://{local_ip}:{PORT}/ws")
    log.info(f"  Live view:    http://{local_ip}:{PORT}/liveview.mjpeg")
    log.info(f"  REST API:     http://{local_ip}:{PORT}/api/*")
    log.info(f"")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("收到中斷信號，準備關閉…")
    finally:
        polling_task.cancel()
        state.reconnect_task.cancel()
        for t in (polling_task, state.reconnect_task):
            with suppress(asyncio.CancelledError):
                await t
        await zc.async_unregister_service(info)
        await zc.async_close()
        if state.camera:
            with suppress(Exception):
                close_camera(state.camera)
        await runner.cleanup()
        log.info("已關閉。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
