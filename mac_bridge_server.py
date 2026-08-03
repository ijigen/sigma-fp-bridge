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
    http://localhost:1025/

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
import pwd
import socket
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

import aiohttp
from aiohttp import web
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

# 引入 PoC 裡的 patch 跟基礎 helpers
sys.path.insert(0, str(Path(__file__).parent))
import capture
import movie_settings
import ptp_probe
import recording
from ifd import parse_ifd, to_json
from camera_settings import (
    read_status,
    AUTO_OVERRIDE_HINTS,
    SettingError,
    apply_settings,
    describe,
    read_settings,
    verify_applied,
)
from sigma_fp_focus import (
    enter_api_mode,
    leave_api_mode,
    open_camera,
    read_capabilities,
    read_choice_values,
    read_info5_raw,
    read_movie_group_raw,
    close_camera,
    get_focus_range,
    get_focus_state,
    set_focus_position,
    set_focus_mode,
    set_face_eye_af,
    set_focus_area,
    set_focus_point,
    set_focus_point_size,
    trigger_af,
    read_focus_area_bounds,
    read_focus_choices,
    CamDataGroupFocusExt,
)

# ─────────────────────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("sigma-bridge")

#: 版本號。執行檔的檔名由 sigma-fp-bridge.spec 從這裡讀出來 —— 只有一個
#: 來源，檔名和程式回報的版本才不會各說各話。
VERSION = "0.1.1"

#: 10/25 —— fp 出貨的日子（2019-10-25）。
#:
#: 剛好也是「非特權」範圍的第一個埠。這支程式平常是 sudo 跑的（要從
#: ptpcamerad 手上搶相機），但綁的埠不必跟著吃特權：綁在 1024 以下的話，
#: 之後就算相機那邊不用 root 了也還是會卡在這裡。
#:
#: 可用 SIGMA_BRIDGE_PORT 覆寫。
PORT = int(os.environ.get("SIGMA_BRIDGE_PORT", "1025"))
SERVICE_TYPE = "_sigmafp._tcp.local."
SERVICE_NAME = "Sigma fp Bridge"


def _resolve_state_dir() -> Path:
    """校準檔要放**真正使用者**的家目錄，不是 root 的。

    macOS 上要從 ptpcamerad 手中搶到相機必須是 root，所以 bridge 平常都是
    sudo 跑的 —— 而 sudo 下 Path.home() 會變成 /var/root。照著寫的話校準表
    會存進 root 的家目錄：使用者原本的資料看不見，之後不用 sudo 跑又換一份，
    兩邊永遠對不起來。用 SUDO_USER 還原成真正的使用者。

    可用 SIGMA_BRIDGE_STATE_DIR 覆寫（測試用）。
    """
    override = os.environ.get("SIGMA_BRIDGE_STATE_DIR")
    if override:
        return Path(override)

    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".sigma_fp_bridge"
        except KeyError:
            log.warning(f"SUDO_USER={sudo_user} 查不到，改用 {Path.home()}")

    return Path.home() / ".sigma_fp_bridge"


def _restore_ownership(path: Path) -> None:
    """把 root 建出來的檔案還給原使用者。

    不還的話，之後不用 sudo 跑就會因為 root 擁有而寫不進去。
    """
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user or os.geteuid() != 0:
        return
    try:
        entry = pwd.getpwnam(sudo_user)
        os.chown(path, entry.pw_uid, entry.pw_gid)
    except (KeyError, OSError) as e:
        log.debug(f"還原 {path} 擁有者失敗：{e}")


def _app_dir() -> Path:
    """程式自己所在的目錄。

    打包成單一執行檔時，資料是解壓到暫存目錄的，__file__ 指不到那裡 ——
    PyInstaller 會把路徑放在 sys._MEIPASS。沒有這個分支的話，打包出來的
    執行檔開得起來，但網頁會 404。
    """
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


STATE_DIR = _resolve_state_dir()
#: live view 的目標間隔。0 = 不節流。
#:
#: 這是上限不是下限：相機比目標慢時完全不睡。
#:
#: 33 ms 是量出來的，不是猜的。完全不節流時 bridge 每秒問相機 42.5 次，
#: 但其中只有 30 張內容真的不同 —— 另外 30% 是同一張又拿了一次，那些 PTP
#: 交易白白佔用匯流排，而匯流排一次只跑一筆，會直接排擠對焦指令。
#: 貼著相機的實際產出速率，就不浪費也不設限。
#: 用 SIGMA_LIVEVIEW_INTERVAL 覆寫。
LIVE_VIEW_INTERVAL_S = float(os.environ.get("SIGMA_LIVEVIEW_INTERVAL", "0.033"))
STATE_BROADCAST_INTERVAL_S = 0.1  # 10Hz state push to clients
#: 影格請求排隊超過這麼久就放棄。
#:
#: 正常運作時永遠碰不到：控制指令只要 1 ms，堆不出 250 ms。它只在真的長操作
#: （下載 27 MB 的 DNG 實測 2.5 秒、拍攝）時觸發，而那時候那張影格確實已經
#: 過期 —— 不是「影格可以隨便丟」，是「250 ms 前的畫面對預覽和對焦都沒有用」。
LIVE_VIEW_STALE_S = 0.25
MJPEG_IDLE_CHECK_S = 1.0  # 沒有新影格時，每隔這麼久確認一次 client 還在不在
FOCAL_POLL_EVERY = 10  # 每 N 次狀態輪詢才讀一次焦距（焦距很少變，不值得 10Hz 打 USB）


# ─────────────────────────────────────────────────────────────────────────────
# Server 狀態
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BridgeState:
    """中央 server 狀態。"""

    camera: object | None = None
    ws_clients: set[web.WebSocketResponse] = field(default_factory=set)
    mjpeg_clients: set[web.StreamResponse] = field(default_factory=set)
    active_lens_id: str = "default"
    last_focus_position: int | None = None
    last_focus_state: int | None = None
    last_lens_focal_mm: int | None = None
    # CanSetInfo5 tag 658 回報的 (min, max)。隨鏡頭與變焦位置而變，
    # 讀不到時是 None（UI 會退回自己長 slider 的舊行為）。
    focus_range: tuple[int, int] | None = None
    # 相機實際接受的數值範圍（ISO、曝光補償…）。APEX 換算表涵蓋的範圍遠大於
    # 任何一台實機，不靠這個過濾的話 UI 會列出一堆設了會失敗的值。
    capabilities: dict = field(default_factory=dict)
    #: 錄影設定的合法值清單（來自 CanSetInfo5 的 150/151/160/161/214 等）
    movie_capabilities: dict = field(default_factory=dict)
    #: release 前的設定快照，acquire 後用來還原（見 release_camera）
    settings_snapshot: dict | None = None
    #: 交還期間保留的相機連線。放開 USB 會被 macOS 搶走，所以只退出 API 模式。
    released_camera: object | None = None
    #: 影片下載進度 {"done": int, "total": int}，沒在下載時是 None
    movie_progress: dict | None = None
    #: 相機唯讀狀態（卡片空間、電池、鏡頭焦段），讀設定時一併更新
    camera_status: dict | None = None
    #: 相機當下接受的列舉值 {設定名稱: [原始值]}。跟 capabilities 分開存 ——
    #: 那個字典的每個值都是帶 min/max 的 dict，形狀不同的東西不能混進去。
    choice_values: dict = field(default_factory=dict)
    #: 最近一次讀到的設定值。describe() 用它來判斷「宣告清單是不是真的值列表」
    #: —— 目前生效的值不在裡面，那就不是。
    last_settings: dict = field(default_factory=dict)
    #: 錄影時快門用速度還是角度（DataGroupMovie tag 6：1 = 速度、2 = 角度）
    shutter_unit: int | None = None
    #: "stills" | "movie" | None —— 機身撥桿位置（推測而來，見 refresh_capabilities）
    camera_mode: str | None = None
    #: 錄影中與否。bridge 自己記，因為相機沒有可直接查詢的「正在錄影」旗標
    recording: bool = False
    recording_started_at: float | None = None
    # 快門角度換算用的幀率。每次讀設定時會從相機的 DataGroupMovie tag 61
    # 更新，所以這個預設值只在還沒讀過或不在 CINE 模式時有意義。
    frame_rate: float = 24.0
    camera_connected: bool = False
    # True = 使用者主動交還相機，自動重連要停手，否則放開的下一秒就被搶回去
    released_by_user: bool = False
    reconnect_task: asyncio.Task | None = None
    last_focus_mode: str | None = None  # 顯示 AF/MF/AF_S 等狀態
    last_face_eye_af: str | None = None
    last_face_eye_status: str | None = None
    last_focus_area: str | None = None
    last_focus_point: list | None = None
    #: 最後一次「命令」的位置，以及命令發出的時間。跟 last_focus_position
    #: 不同 —— 那個是相機回報的實際值，會落後。影格要標的是命令，因為
    #: 消費端問的是「這張影像反映的是不是我剛下的那個位置」。
    last_focus_position_cmd: int | None = None
    position_changed_at: float | None = None
    last_point_size: int | None = None
    last_continuous_af: str | None = None


state = BridgeState()


# ─────────────────────────────────────────────────────────────────────────────
# 相機 worker
#
# USB PTP 一次只能跑一個 transaction，所以相機存取一定要序列化。以前是所有
# 呼叫端各自去搶同一把 asyncio.Lock，那有三個問題：
#
#   1. asyncio.Lock 是先到先服務。使用者的控焦指令會排在已經在等的一堆
#      live view 影格請求後面 —— 最該即時的東西反而最慢。
#   2. 每個 MJPEG client 各自去抓影格。開兩個瀏覽器分頁就是兩倍 USB 流量，
#      抓回來的還是同一張圖。
#   3. 會長時間持鎖的操作（例如輪詢等馬達停止）在放開之前，把 live view
#      跟狀態更新整個凍住。
#
# 改成：單一 worker task 獨佔相機，所有存取變成投進優先權佇列的 job。
# 控焦 > 狀態 > live view。live view 由單一 producer 抓、扇出給所有 client。
# ─────────────────────────────────────────────────────────────────────────────


class Priority(IntEnum):
    """數字小的先做。

    這個順序的理由是**排程，不是重要性**。量到的每筆交易成本：

        影格抓取（623 KB）   29.7 ms      ← 佔掉整條線的 89%
        控制指令              1.0 ms
        狀態讀取              1.0 ms

    短的先跑，總等待最小：控制先跑讓影格晚 1 ms，影格先跑讓控制晚 30 ms。
    PTP 一次只能一筆交易，所以這是唯一能調的東西。

    ⚠️ 不要把 LIVEVIEW 排在最後讀成「影格不重要」。它是跟焦 AI 的感測器輸入，
    掉一張就是掉一次量測。正因為如此才不能讓它去堵一個只要 1 ms 的指令 ——
    那 30 ms 會回過頭變成對焦誤差。舊註解寫「掉了就掉了」，那是 live view
    還只是預覽時的說法。
    """

    CONTROL = 0    # 控焦指令 / 連線管理。1 ms，先跑幾乎不影響別人
    STATUS = 1     # 狀態輪詢。同樣是 1 ms
    LIVEVIEW = 2   # 影格抓取。29.7 ms，讓它排在最後是為了不放大別人的延遲


#: 單一相機操作的上限。超過就認定 PTP 呼叫回不來了。
#: 抓得比最慢的合法操作寬鬆 —— 拍攝本身最多等 30 秒產生影像，再加上
#: 27 MB DNG 的下載（實測 2.5 秒），120 秒有很大餘裕。
DEFAULT_JOB_TIMEOUT_S = 120.0


class CameraStuck(RuntimeError):
    """相機操作超過上限沒回來。

    Python 沒辦法中止卡在 USB 呼叫裡的執行緒，所以這個狀態是不可逆的：
    那條執行緒會一直佔著相機。唯一的復原方式是重啟 bridge。這個例外的意義
    是「立刻講出來」而不是讓每個請求無聲地排隊等一個永遠不會結束的工作。
    """


class CameraUnavailable(RuntimeError):
    """相機沒連上時，需要相機的 job 會拿到這個。"""


@dataclass
class JobSkipped:
    """worker 沒有真的把這個 job 送到 USB 上。

    reason 是 "superseded"（被更新的同類指令蓋過）或 "expired"（排太久已無意義）。
    """

    reason: str


@dataclass
class CameraJob:
    priority: int
    seq: int
    fn: Callable[[], object]
    future: asyncio.Future
    needs_camera: bool = True
    coalesce_key: str | None = None
    expires_at: float | None = None
    timeout: float = DEFAULT_JOB_TIMEOUT_S


class CameraWorker:
    """序列化所有相機存取的單一 owner。

    只有 worker 這個 task 會碰 state.camera，也只有它會丟東西給 executor，
    所以相機同時間永遠只有一個 in-flight transaction —— 不需要額外的鎖。
    """

    def __init__(self) -> None:
        # 注意：這裡刻意不建 asyncio.PriorityQueue。Python 3.9 的 asyncio.Queue
        # 在 __init__ 就把自己綁到「當下的」event loop，而這個物件是 module
        # 層級建立的 —— 那時候還沒有 asyncio.run() 的那個 loop。綁錯 loop 的話
        # put_nowait() 喚醒的 getter future 屬於一個永遠不會再跑的 loop，
        # worker 會靜靜地卡死。改在 start() 裡建（那時已經在正確的 loop 內）。
        self._queue: asyncio.PriorityQueue | None = None
        self._seq = 0
        self._newest: dict[str, int] = {}  # coalesce_key -> 目前最新的 seq
        self._task: asyncio.Task | None = None
        #: 有工作逾時沒回來就記在這裡。設了之後所有工作立刻失敗，不再排隊。
        self.stuck: str | None = None

    # -- 生命週期 ---------------------------------------------------------

    def start(self) -> None:
        self._queue = asyncio.PriorityQueue()
        self._task = asyncio.create_task(self._run(), name="camera-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    # -- 提交 -------------------------------------------------------------

    def submit(
        self,
        fn: Callable[[], object],
        *,
        priority: Priority = Priority.CONTROL,
        needs_camera: bool = True,
        coalesce_key: str | None = None,
        ttl: float | None = None,
        timeout: float = DEFAULT_JOB_TIMEOUT_S,
    ) -> asyncio.Future:
        """把一個同步的相機操作排進佇列，回傳等結果用的 future。

        Args:
            fn: 零參數的同步 callable，會在 executor 裡跑。刻意設計成零參數
                ——想拿 state.camera 就在 fn 內部讀，這樣重連換掉相機物件之後
                排在佇列裡的 job 不會抓到舊的。
            coalesce_key: 同一個 key 只有最新的那個會真的執行，先前排隊的
                直接以 JobSkipped("superseded") 收場。適用於「絕對值設定」
                這種語意 —— 例如焦點位置，只有最後一個目標值有意義。
            ttl: 秒。超過就不執行，回 JobSkipped("expired")。
            timeout: 秒。開始執行之後超過這麼久還沒回來就判定相機卡住。
        """
        if self._queue is None:
            raise RuntimeError("CameraWorker 還沒 start()")
        loop = asyncio.get_running_loop()
        self._seq += 1
        job = CameraJob(
            priority=int(priority),
            seq=self._seq,
            fn=fn,
            future=loop.create_future(),
            needs_camera=needs_camera,
            coalesce_key=coalesce_key,
            expires_at=(time.monotonic() + ttl) if ttl is not None else None,
            timeout=timeout,
        )
        if coalesce_key is not None:
            self._newest[coalesce_key] = job.seq
        # seq 放在 tuple 裡當 tie-breaker，這樣同優先權時是 FIFO，
        # 而且比較永遠不會比到 CameraJob 本身（它不可比較）。
        self._queue.put_nowait((job.priority, job.seq, job))
        return job.future

    async def call(self, fn: Callable[[], object], **kwargs):
        """submit() 之後直接等結果。"""
        return await self.submit(fn, **kwargs)

    # -- worker 本體 ------------------------------------------------------

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            _, _, job = await self._queue.get()
            try:
                if job.future.cancelled():
                    continue

                if (
                    job.coalesce_key is not None
                    and self._newest.get(job.coalesce_key) != job.seq
                ):
                    # 已經有更新的同類指令進來了，這個不用送 USB
                    job.future.set_result(JobSkipped("superseded"))
                    continue

                if job.expires_at is not None and time.monotonic() > job.expires_at:
                    job.future.set_result(JobSkipped("expired"))
                    continue

                if job.needs_camera and state.camera is None:
                    job.future.set_exception(CameraUnavailable("camera not connected"))
                    continue

                if self.stuck is not None:
                    # 前一個工作還卡在 USB 上，那條執行緒仍握著相機。
                    # 再送只會多卡一條執行緒，而且一樣不會回來。
                    job.future.set_exception(CameraStuck(self.stuck))
                    continue

                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, job.fn), timeout=job.timeout)
                except asyncio.TimeoutError:
                    # wait_for 只放棄等待 —— executor 那條執行緒還卡在 USB 呼叫裡，
                    # 沒有辦法叫醒它。所以這裡不重試，直接標記成不可用。
                    self.stuck = (
                        f"相機操作超過 {job.timeout:.0f} 秒沒有回應，PTP 呼叫回不來了。"
                        "the thread cannot be killed - restart the bridge.")
                    print(f"錯誤：{self.stuck}", file=sys.stderr)
                    job.future.set_exception(CameraStuck(self.stuck))
                    continue
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.cancel()
                raise
            except Exception as e:
                if not job.future.done():
                    job.future.set_exception(e)
            else:
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                self._queue.task_done()


worker = CameraWorker()


# ─────────────────────────────────────────────────────────────────────────────
# Live view 影格扇出
# ─────────────────────────────────────────────────────────────────────────────


class FrameHub:
    """單一 producer 抓影格，所有 MJPEG client 共享。

    跟不上的 client 會直接跳過中間的影格拿最新的一張，不會回壓到相機。
    以前每個 client 各自抓，client 數量直接等於 USB 負載倍數。
    """

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.seq = 0
        self.meta: dict = {}
        self._event: asyncio.Event | None = None

    def _get_event(self) -> asyncio.Event:
        # 跟 CameraWorker._queue 同一個理由：Python 3.9 的 asyncio.Event 在
        # __init__ 就綁定 event loop，而這個物件是 module 層級建立的。
        # 延後到第一次使用（此時已在正確的 loop 內）才建。
        if self._event is None:
            self._event = asyncio.Event()
        return self._event

    def publish(self, jpeg: bytes) -> None:
        self.frame = jpeg
        self.seq += 1
        # 每張影格帶上「它是在什麼狀態下抓的」。消費端要判斷一張影像反映
        # 的是不是最新下的位置，靠的就是 pos_age_ms —— 沒有這個就只能等一
        # 個保守的固定時間，而那個等待佔掉了對焦迴路快一半的時間。
        now = time.time()
        changed = state.position_changed_at
        self.meta = {
            "seq": self.seq,
            "t": now,
            "pos": state.last_focus_position_cmd,
            "pos_age_ms": None if changed is None else round((now - changed) * 1000),
        }
        # set() 會叫醒所有等待者，緊接著 clear() 讓下一輪重新等。
        # 已經被叫醒的不會因為 clear() 而失效。
        event = self._get_event()
        event.set()
        event.clear()

    async def wait_for_next_meta(self, last_seq: int) -> tuple[int, bytes, dict]:
        """跟 wait_for_next 一樣，但連同影格的中繼資料一起回。"""
        seq, jpeg = await self.wait_for_next(last_seq)
        return seq, jpeg, dict(self.meta)

    async def wait_for_next(self, last_seq: int) -> tuple[int, bytes]:
        """等到有一張比 last_seq 新的影格。忙碌中的 client 會直接拿到最新的。"""
        event = self._get_event()
        while self.seq == last_seq or self.frame is None:
            await event.wait()
        return self.seq, self.frame


frames = FrameHub()


# ─────────────────────────────────────────────────────────────────────────────
# 相機操作（都走 worker）
# ─────────────────────────────────────────────────────────────────────────────


async def cam_get_focus() -> CamDataGroupFocusExt:
    return await worker.call(
        lambda: get_focus_state(state.camera), priority=Priority.STATUS
    )


async def refresh_focus_range() -> None:
    """讀當下鏡頭的合法焦點位置範圍。

    讀不到不算錯 —— 有些鏡頭 / 韌體不回報，這時 state.focus_range 留 None，
    UI 會退回原本自己長 slider 上限的行為。
    """
    try:
        lo, hi = await worker.call(
            lambda: get_focus_range(state.camera), priority=Priority.STATUS
        )
    except CameraUnavailable:
        return
    except LookupError as e:
        state.focus_range = None
        log.warning(f"相機沒回報焦點範圍：{e}")
        return
    except Exception as e:
        state.focus_range = None
        log.warning(f"讀焦點範圍失敗：{e}")
        return
    if state.focus_range != (lo, hi):
        log.info(f"焦點位置範圍：{lo} ~ {hi}")
    state.focus_range = (lo, hi)


async def refresh_capabilities() -> None:
    """重讀相機接受的數值範圍。鏡頭 / 模式改變時要跟著更新。"""
    try:
        caps = await worker.call(
            lambda: read_capabilities(state.camera), priority=Priority.STATUS
        )
        state.choice_values = await worker.call(
            lambda: read_choice_values(state.camera), priority=Priority.STATUS
        )
    except CameraUnavailable:
        return
    except Exception as e:
        log.warning(f"讀取相機能力失敗：{e}")
        return
    try:
        info5 = await worker.call(
            lambda: read_info5_raw(state.camera), priority=Priority.STATUS
        )
        state.movie_capabilities = movie_settings.read_capabilities(state.camera, info5)
    except Exception as e:
        log.debug(f"讀取錄影能力失敗：{e}")

    # 機身撥桿位置沒有直接可讀的欄位（CanSetInfo5 tag 100 StillMovieSwitch
    # 回的是 [1, 1]，看起來是「兩種都支援」而不是目前在哪一邊）。但錄影專屬
    # 的合法值清單只在 CINE 模式下才有內容 —— 實測：STILL 時 RecordFormat /
    # CinemaDNGImageQuality / MovieResolution / ShutterAngle 全是空的，
    # 切到 CINE 就都有值。用這個推測模式。
    # 首選讀 DataGroupMovie tag 1（capture_mode），那是機身模式本身。
    # 讀不到才退回推測：看相機有沒有回報錄影專屬的能力 —— 但要排除
    # FALLBACK_CHOICES，那是我們自己補的值域，永遠存在。
    mode = None
    try:
        movie = await worker.call(
            lambda: movie_settings.read_settings(state.camera), priority=Priority.STATUS)
        if movie.get("capture_mode") in (1, 2):
            mode = "stills" if movie["capture_mode"] == 1 else "movie"
        state.shutter_unit = movie.get("shutter_unit")
    except Exception as e:
        log.debug(f"讀取機身模式失敗：{e}")
    if mode is None:
        reported = set(state.movie_capabilities) - set(movie_settings.FALLBACK_CHOICES)
        mode = "movie" if reported else "stills"
    if mode != state.camera_mode:
        log.info(f"機身模式：{'錄影 (CINE)' if mode == 'movie' else '拍照 (STILL)'}")
    state.camera_mode = mode

    if caps != state.capabilities:
        summary = ", ".join(
            f"{k} {v.get('min')}–{v.get('max')}" for k, v in sorted(caps.items())
        )
        log.info(f"相機接受範圍：{summary}")
    state.capabilities = caps


@dataclass
class SetPositionResult:
    """cam_set_position() 的結果。"""

    requested: int          # 呼叫端本來要的值
    position: int           # 實際送到相機的值（可能被 clamp）
    applied: bool           # False = 還沒送出就被更新的指令取代
    clamped: bool           # True = 超出合法範圍，已修到邊界


def clamp_to_range(position: int) -> tuple[int, bool]:
    """把位置限制在相機回報的合法範圍內。不知道範圍就原樣放行。"""
    if state.focus_range is None:
        return position, False
    lo, hi = state.focus_range
    clamped = max(lo, min(hi, position))
    return clamped, clamped != position


#: 讀回的位置離目標多遠才算「沒寫進去」。馬達有量化，差幾個單位是正常的
#: （實測寫 9000 讀回 8999 / 9005）。
_POSITION_TOLERANCE = 24


def _set_and_readback(position: int):
    """在 executor 裡跑：設定位置，然後立刻讀回來。

    兩個動作放同一個 job，中間不會被別的 transaction 插進來，
    readback 讀到的就確實是這次寫入的結果。

    **從 AF 切到 MF 的那一次寫入，位置會被相機忽略。** 實測（起點 AF-S，
    位置 8999）：

        寫 6500  →  位置 8999，模式從 3 變成 1    ← 只有模式生效
        寫 9000  →  位置 9005                    ← 已經在 MF，生效

    set_focus_position 一次寫入同時帶 FocusMode=MF 和 FocusPosition，所以
    離開 AF 之後的第一筆一定丟掉位置。平常看不出來，因為拉滑桿會連送很多
    筆，第二筆就補上了 —— 但程式化地叫它「去某個位置」就會靜靜地失敗。

    只在讀回真的不符時補寫一次，所以正常情況不多付任何代價。
    """
    set_focus_position(state.camera, position)
    try:
        after = get_focus_state(state.camera)
    except Exception as e:  # readback 失敗不該讓整個 set 算失敗
        log.warning(f"set 後 readback 失敗：{e}")
        return None

    got = getattr(after, "FocusPosition", None)
    if got is not None and abs(got - position) > _POSITION_TOLERANCE:
        log.debug(f"位置沒寫進去（要 {position}，讀回 {got}）—— 補寫一次")
        set_focus_position(state.camera, position)
        try:
            after = get_focus_state(state.camera)
        except Exception as e:
            log.warning(f"補寫後 readback 失敗：{e}")
    return after


async def cam_set_position(position: int) -> SetPositionResult:
    """設定焦點位置。

    coalesce：焦點位置是絕對值而不是增量，所以拖 slider 或 iOS 端高頻餵
    目標值時，排隊中的舊值直接丟掉只送最新的，是正確也是想要的行為。
    """
    target, clamped = clamp_to_range(position)
    if clamped:
        log.warning(f"位置 {position} 超出範圍 {state.focus_range}，修正為 {target}")

    if target != state.last_focus_position_cmd:
        state.last_focus_position_cmd = target
        state.position_changed_at = time.time()

    result = await worker.call(
        lambda: _set_and_readback(target),
        priority=Priority.CONTROL,
        coalesce_key="set_position",
    )
    if isinstance(result, JobSkipped):
        log.debug(f"set_position({target}) 被較新的指令取代")
        return SetPositionResult(position, target, applied=False, clamped=clamped)
    if result is not None:
        log.info(
            f"set_position({target}) → readback: "
            f"FocusMode={result.FocusMode}, FocusPosition={result.FocusPosition}, "
            f"FocusState={result.FocusState}"
        )
    return SetPositionResult(position, target, applied=True, clamped=clamped)


async def cam_wait_idle(timeout_s: float = 2.0, poll_interval_s: float = 0.05) -> bool:
    """等馬達停下來。

    刻意用「一次一個短 job」而不是把整段輪詢丟進一個 job —— 後者會獨佔
    相機好幾秒，live view 跟狀態更新全部卡死。
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            f = await cam_get_focus()
        except CameraUnavailable:
            return False
        if f.FocusState == 0:  # Idle
            return True
        await asyncio.sleep(poll_interval_s)
    return False


def _full_schema() -> list:
    """完整的設定 schema：靜態 + 錄影，依當下模式與快門單位過濾。

    只有這一個來源。先前 describe_settings、REST、以及 set_settings 的 ack
    各自組一份，改了前兩個卻漏掉 ack —— 而 ack 正是 UI 每次操作後用來更新
    schema 的那條路，於是切換快門單位後整組控制項消失。
    """
    return (describe(state.capabilities, state.camera_mode, _shutter_speed_allowed(),
                     state.choice_values, state.last_settings)
            + _movie_schema() + _capture_mode_schema())


def _movie_schema() -> list:
    """錄影設定的 schema，並依快門單位濾掉當下無效的那一個。

    速度模式下列出 shutter_angle 只會讓人按了沒反應 —— 相機接受哪個欄位
    是由 shutter_unit（tag 6）決定的。
    """
    if state.camera_mode != "movie":
        return []
    rows = movie_settings.describe(state.movie_capabilities)
    if state.shutter_unit == 1:
        rows = [r for r in rows if r["name"] != "shutter_angle"]
    return rows


def _capture_mode_schema() -> list:
    """機身模式那一列。錄影模式以外也要能拿到 —— 不然切到拍照後就沒有
    路可以切回去了。"""
    return [r for r in movie_settings.describe(movie_settings.FALLBACK_CHOICES)
            if r["name"] == "capture_mode"]


def _shutter_speed_allowed() -> set:
    """錄影模式下相機切到「快門速度」時，shutter_speed 才是有效的。

    這是 DataGroupMovie tag 6 決定的（1 = 速度、2 = 角度）—— 用寫入試探
    確認：設成 2 時相機宣告 18 個合法快門角度，設成 1 時變成 0 個。
    """
    if state.camera_mode == "movie" and state.shutter_unit == 1:
        return {"shutter_speed"}
    return set()


def _read_all_settings() -> dict:
    """在 executor 裡跑：靜態設定 + 錄影設定一起讀。

    CINE 模式下曝光由 DataGroupMovie 管，光看 DataGroup1 會誤導 ——
    它照樣回報一個快門值，但那個值寫不進去也不是實際生效的。

    錄影設定先讀，因為快門角度換算需要幀率，而相機自己就報得出來
    （DataGroupMovie tag 61）。先前這裡用的是使用者手動指定的 state.frame_rate
    —— 那是在這個 DataGroup 的 tag 還沒解出來時的權宜做法，猜錯就會讓角度
    算錯，而使用者不會發現。
    """
    try:
        state.camera_status = read_status(state.camera)
    except Exception as e:
        log.warning(f"狀態讀取失敗：{e}")

    movie = {}
    try:
        movie = movie_settings.read_settings(state.camera)
        fps = movie.get("frame_rate")
        if isinstance(fps, (int, float)) and fps > 0:
            state.frame_rate = float(fps)
    except Exception as e:
        log.debug(f"錄影設定讀取失敗（可能不在 CINE 模式）：{e}")

    out = read_settings(state.camera, state.frame_rate)
    if movie:
        out.update(movie)
        state.shutter_unit = movie.get("shutter_unit")
        # tag 1 直接就是機身模式，比從能力清單推測可靠得多
        if movie.get("capture_mode") in (1, 2):
            state.camera_mode = "stills" if movie["capture_mode"] == 1 else "movie"
    # describe() 要用它判斷「宣告清單是不是真的值列表」
    state.last_settings = dict(out)
    return out


async def cam_read_settings() -> dict:
    return await worker.call(
        _read_all_settings, priority=Priority.STATUS,
    )


def _split_movie_changes(changes: dict) -> tuple[dict, dict]:
    """把變更拆成「錄影設定」與「靜態設定」兩堆。"""
    movie = {k: v for k, v in changes.items() if k in movie_settings.MOVIE_BY_NAME}
    still = {k: v for k, v in changes.items() if k not in movie_settings.MOVIE_BY_NAME}
    return movie, still


def _apply_and_verify(changes: dict) -> dict:
    """在 executor 裡跑：套用設定，然後立刻回讀確認相機真的吃了。

    寫入與驗證放同一個 job，中間不會被別的 transaction 插進來。
    """
    movie_changes, still_changes = _split_movie_changes(changes)

    applied: dict = {}
    if movie_changes:
        applied.update(movie_settings.apply_settings(
            state.camera, movie_changes, state.movie_capabilities))
    if still_changes:
        applied.update(apply_settings(state.camera, still_changes,
                                      state.capabilities, state.frame_rate,
                                      state.camera_mode, _shutter_speed_allowed()))

    actual = _read_all_settings()
    mode = actual.get("exposure_mode")
    rejected = {}
    for name, wanted in applied.items():
        got = actual.get(name)
        if got is None or _roughly_equal_setting(name, got, wanted):
            continue
        # 錄影快門跟靜態快門一樣會被自動曝光搶走 —— 實測：ProgramAuto 下
        # 寫 shutter_angle 沒作用，切到 Manual 就成功。給出同樣的提示。
        modes = AUTO_OVERRIDE_HINTS.get(name)
        hint = None
        if modes and mode not in modes:
            hint = (f"曝光模式目前是 {mode}，自動曝光會覆蓋手動設的 {name}。"
                    f"先把 exposure_mode 設成 {' 或 '.join(modes)} 再試。")
        rejected[name] = {"requested": wanted, "actual": got, "hint": hint}
    # 靜態設定的「被自動曝光蓋掉」提示比較講究，沿用原本那套
    for name, detail in verify_applied(state.camera, still_changes,
                                       state.frame_rate).items():
        rejected[name] = detail
    return {"applied": applied, "rejected": rejected}


def _roughly_equal_setting(name: str, got, wanted) -> bool:
    from camera_settings import (LOOSE_EPSILON, LOOSE_SETTINGS, VALUE_EPSILON,
                                 _roughly_equal, canonical_value)
    tol = LOOSE_EPSILON if name in LOOSE_SETTINGS else VALUE_EPSILON
    return _roughly_equal(got, canonical_value(name, wanted), tol)


async def cam_apply_settings(changes: dict) -> dict:
    """套用設定變更並回報哪些相機沒吃。整批一起驗證，不會出現一半成功的狀態。"""
    result = await worker.call(
        lambda: _apply_and_verify(changes), priority=Priority.CONTROL
    )
    for name, detail in result.get("rejected", {}).items():
        log.warning(
            f"相機沒有接受 {name}={detail['requested']}（實際 {detail['actual']}）"
            + (f" — {detail['hint']}" if detail.get("hint") else "")
        )
    return result


def _grab_view_frame() -> bytes | None:
    """在 executor 裡跑：sigma-ptpy 回傳 ViewFrame 物件，要取 .Data。"""
    frame = state.camera.get_view_frame()
    return frame.Data if frame is not None else None


async def try_connect_camera() -> bool:
    """嘗試連線到相機，成功回 True。"""
    # 開相機也走 worker，免得跟進行中的 transaction 撞在一起
    old = state.camera
    if old is not None:
        state.camera = None
        with suppress(Exception):
            await worker.call(lambda: close_camera(old), needs_camera=False)

    try:
        cam = await worker.call(open_camera, needs_camera=False)
    except Exception as e:
        log.warning(f"連不上相機：{e}")
        state.camera = None
        state.camera_connected = False
        return False

    state.camera = cam
    state.camera_connected = True
    log.info("相機已連線")
    await refresh_focus_range()
    await refresh_capabilities()
    await broadcast_state({"event": "camera_connected"})
    return True


def mark_disconnected() -> None:
    """標記相機掉線，讓 reconnect_loop 接手。"""
    state.camera_connected = False


async def release_camera() -> None:
    """交還機身操作，但保持 bridge 執行中。

    config_api() 讓相機進入 API 模式後，機身除了電源以外全部按鍵失效
    （見 README Gotcha 4）。想在不關掉 bridge 的情況下實體操作相機，
    就得真的退出 API 模式 —— 送 CloseApplication 並關掉 session。

    代價：釋放期間沒有 live view、沒有狀態更新、不能控焦。重新取得時
    相機設定會再被 config_api() 重置一次。
    """
    state.released_by_user = True

    # 錄影中就放手會留下一段沒收尾的檔案，先停下來
    if state.recording:
        log.warning("release 前先停止錄影")
        with suppress(Exception):
            await worker.call(lambda: recording.stop(state.camera),
                              priority=Priority.CONTROL)
        state.recording = False
        state.recording_started_at = None

    # 先存檔再放手。重新取得相機時 config_api() 會把設定重置成預設值
    # （SDK 原文："API resets the camera setting to the default"），
    # 不存的話使用者只是暫時拿回機身，回來就發現設定全沒了。
    try:
        state.settings_snapshot = await cam_read_settings()
        log.info(f"已保存 {len(state.settings_snapshot)} 項設定，供 acquire 後還原")
    except Exception as e:
        state.settings_snapshot = None
        log.warning(f"設定快照失敗，acquire 後將無法還原：{e}")

    cam = state.camera
    # 先清掉再關，這樣進行中的 job 會拿到 CameraUnavailable 而不是半死的 handle
    state.camera = None
    state.camera_connected = False
    state.focus_range = None
    state.capabilities = {}
    state.movie_capabilities = {}

    # 只退出 API 模式，不放開 USB —— 放開的話 macOS 會立刻搶走裝置，
    # 之後就再也 acquire 不回來（實測踩過）。物件留著給 acquire 重用。
    if cam is not None:
        try:
            await worker.call(lambda: leave_api_mode(cam), needs_camera=False)
            state.released_camera = cam
        except Exception as e:
            log.warning(f"退出 API 模式失敗，改為完整關閉：{e}")
            with suppress(Exception):
                await worker.call(lambda: close_camera(cam), needs_camera=False)
            state.released_camera = None
    log.info("已交還相機，機身操作恢復（bridge 仍持有 USB）")
    await broadcast_state({"event": "camera_released"})


async def acquire_camera(restore: bool = True) -> bool:
    """重新取得相機控制權，並還原 release 前的設定。

    Args:
        restore: False = 保留相機重置後的預設值。想看機身端改了什麼的時候用
            —— 還原會把那些變更蓋掉。
    """
    state.released_by_user = False

    # 已經連著就什麼都不要做。try_connect_camera() 會先 close_camera(old)
    # 再重開，而 close_camera 會釋放 USB interface —— 那個空檔 macOS 會把
    # 相機搶回去（Gotcha 5）。對一個本來就正常的連線做這件事，等於拿它去賭。
    if state.camera_connected and state.camera is not None:
        log.info("已經連著相機，acquire 不做任何事")
        return True

    # 交還時保留下來的連線可以直接重新進入 API 模式，省掉一次 USB 爭奪
    cam = state.released_camera
    state.released_camera = None
    if cam is not None:
        try:
            await worker.call(lambda: enter_api_mode(cam), needs_camera=False)
            state.camera = cam
            state.camera_connected = True
            log.info("已取回相機（重用既有連線）")
            await refresh_focus_range()
            await refresh_capabilities()
            await broadcast_state({"event": "camera_connected"})
        except Exception as e:
            log.warning(f"重用既有連線失敗，改為重新連線：{e}")
            with suppress(Exception):
                await worker.call(lambda: close_camera(cam), needs_camera=False)
            if not await try_connect_camera():
                return False
    elif not await try_connect_camera():
        return False

    snapshot = state.settings_snapshot
    state.settings_snapshot = None
    if not restore or not snapshot:
        return True

    # 曝光模式要先設：自動模式會擋掉手動的快門 / 光圈，順序錯了就白做
    mode = snapshot.get("exposure_mode")
    if mode:
        with suppress(Exception):
            await cam_apply_settings({"exposure_mode": mode})

    rest = {k: v for k, v in snapshot.items()
            if v is not None and k != "exposure_mode"}
    if rest:
        try:
            result = await cam_apply_settings(rest)
            missed = list(result.get("rejected", {}))
            log.info(
                f"已還原 {len(rest) - len(missed)} 項設定"
                + (f"，{len(missed)} 項還原失敗：{', '.join(missed)}" if missed else "")
            )
        except Exception as e:
            log.warning(f"還原設定失敗：{e}")
    return True


async def reconnect_loop():
    """背景任務：如果相機斷線，每 3 秒嘗試重連。"""
    while True:
        if not state.camera_connected and not state.released_by_user:
            await try_connect_camera()
        await asyncio.sleep(3)


async def liveview_loop():
    """背景任務：唯一的 live view producer。

    每次都等上一張抓完才送下一個請求，所以佇列裡最多只會有一個影格 job
    —— 這就是回壓機制。相機只能給 8fps 的話，就自然變 8fps，
    而不是堆出一串永遠追不上的請求。
    """
    while True:
        if not state.camera_connected or not state.mjpeg_clients:
            await asyncio.sleep(0.2)
            continue
        started = time.monotonic()
        try:
            result = await worker.call(
                _grab_view_frame,
                priority=Priority.LIVEVIEW,
                ttl=LIVE_VIEW_STALE_S,
            )
        except CameraUnavailable:
            await asyncio.sleep(0.2)
            continue
        except Exception as e:
            log.debug(f"live view 取得失敗：{e}")
            await asyncio.sleep(0.2)
            continue

        if isinstance(result, JobSkipped):
            continue  # 排太久，直接抓下一張新的
        if result:
            frames.publish(result)

        # 節流，不是延遲。先前這裡直接 sleep(LIVE_VIEW_INTERVAL_S)，那是加在
        # 取得時間**之後** —— 每輪變成「PTP 時間 + 40ms」，而不是「至少間隔
        # 40ms」。實測 15.2 fps，其中有一大塊是這個多睡的時間。
        # 相機給得比目標慢時就完全不睡，直接抓下一張。
        elapsed = time.monotonic() - started
        remaining = LIVE_VIEW_INTERVAL_S - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)


# ─────────────────────────────────────────────────────────────────────────────
# 廣播給所有 WS clients
# ─────────────────────────────────────────────────────────────────────────────

async def broadcast(message: dict) -> None:
    payload = json.dumps(message)
    dead = []
    for ws in list(state.ws_clients):
        try:
            await ws.send_str(payload)
        except Exception:
            # 任何送不出去的都當 client 已死。以前只接 ConnectionResetError，
            # 其他例外會往上炸掉呼叫端的迴圈（通常是狀態輪詢），
            # 結果一個壞掉的 WS client 就害整台 bridge 誤判相機斷線。
            dead.append(ws)
    for ws in dead:
        state.ws_clients.discard(ws)


async def broadcast_state(extra: dict | None = None) -> None:
    msg = {
        "type": "state",
        "ts": time.time(),
        "connected": state.camera_connected,
        "released": state.released_by_user,
        "camera_mode": state.camera_mode,
        "recording": state.recording,
        "recording_seconds": (
            round(time.monotonic() - state.recording_started_at, 1)
            if state.recording_started_at else None
        ),
        "focus_position": state.last_focus_position,
        "focus_state": state.last_focus_state,
        "focus_mode": state.last_focus_mode,
        # 對焦面板要用的：臉眼偵測、對焦區域、對焦點、持續對焦
        "face_eye_af": state.last_face_eye_af,
        "face_eye_detected": state.last_face_eye_status,
        "focus_area": state.last_focus_area,
        "focus_point": state.last_focus_point,
        "point_size": state.last_point_size,
        "continuous_af": state.last_continuous_af,
        "focal_length_mm": state.last_lens_focal_mm,
        "focus_range": list(state.focus_range) if state.focus_range else None,
        "frame_rate": state.frame_rate,
        "active_lens_id": state.active_lens_id,
    }
    if extra:
        msg.update(extra)
    await broadcast(msg)


async def state_polling_loop():
    """背景任務：定期讀相機狀態廣播給所有 client。"""
    tick = 0
    while True:
        if state.camera_connected:
            try:
                f = await cam_get_focus()
                state.last_focus_position = f.FocusPosition
                state.last_focus_state = f.FocusState
                state.last_focus_mode = (
                    f.FocusMode.name if f.FocusMode is not None else None
                )
                state.last_face_eye_af = _enum_name(getattr(f, "FaceEyeAF", None))
                state.last_face_eye_status = _enum_name(getattr(f, "FaceEyeAFStatus", None))
                state.last_focus_area = _enum_name(getattr(f, "FocusArea", None))
                point = getattr(f, "DMFPos", None)
                state.last_focus_point = list(point) if point else None
                state.last_point_size = getattr(f, "DMFSize", None)
                state.last_continuous_af = _enum_name(getattr(f, "PreConstAF", None))
                # 焦距只有換鏡頭 / 轉變焦環才會變，用不著跟焦點狀態一樣 10Hz 打 USB
                if tick % FOCAL_POLL_EVERY == 0:
                    g1 = await worker.call(
                        lambda: state.camera.get_cam_data_group1(),
                        priority=Priority.STATUS,
                    )
                    focal = g1.CurrentLensFocalLength
                    if focal != state.last_lens_focal_mm:
                        state.last_lens_focal_mm = focal
                        # 焦點範圍與可用 ISO 等都可能隨鏡頭 / 模式改變，一併重讀
                        await refresh_focus_range()
                        await refresh_capabilities()
                await broadcast_state()
            except CameraUnavailable:
                mark_disconnected()
            except Exception as e:
                log.warning(f"狀態讀取失敗：{e}")
                mark_disconnected()
                await broadcast_state({"event": "camera_disconnected"})
        tick += 1
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
        "focus_range": list(state.focus_range) if state.focus_range else None,
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
        {"cmd": "get_state"}
        {"cmd": "set_active_lens", "lens_id": <str>}
    """
    cmd = req.get("cmd")
    request_id = req.get("id")

    # 這幾個在沒相機時也要能用：查狀態、看有哪些設定、以及重新取得控制權
    ALWAYS_ALLOWED = {"get_state", "describe_settings", "acquire", "release",
                      "set_frame_rate"}

    if not state.camera_connected and cmd not in ALWAYS_ALLOWED:
        return {"type": "error", "id": request_id, "error": "camera not connected"}

    if cmd == "release":
        await release_camera()
        return {"type": "ack", "id": request_id, "released": True}

    if cmd == "acquire":
        ok = await acquire_camera(restore=req.get("restore", True))
        return {"type": "ack", "id": request_id, "released": not ok, "connected": ok}

    if cmd in ("record_start", "record_stop"):
        if cmd == "record_start" and state.recording:
            return {"type": "error", "id": request_id, "error": "already recording"}
        fn = recording.start if cmd == "record_start" else recording.stop
        await worker.call(lambda: fn(state.camera), priority=Priority.CONTROL)
        state.recording = cmd == "record_start"
        state.recording_started_at = time.monotonic() if state.recording else None
        await broadcast_state()
        return {"type": "ack", "id": request_id, "recording": state.recording}

    if cmd == "capture_status":
        return {
            "type": "capture_status",
            "id": request_id,
            "status": await worker.call(
                lambda: recording.capture_status(state.camera),
                priority=Priority.STATUS),
        }

    if cmd == "describe_settings":
        return {
            "type": "settings_schema",
            "id": request_id,
            "settings": _full_schema(),
            "capabilities": state.capabilities,
            "movie_capabilities": state.movie_capabilities,
            "camera_mode": state.camera_mode,
            "shutter_unit": state.shutter_unit,
            "frame_rate": state.frame_rate,
        }

    if cmd == "set_frame_rate":
        try:
            fps = float(req["frame_rate"])
        except (KeyError, TypeError, ValueError):
            return {"type": "error", "id": request_id, "error": "frame_rate must be a number"}
        if fps <= 0:
            return {"type": "error", "id": request_id, "error": "frame_rate must be > 0"}
        state.frame_rate = fps
        log.info(f"快門角度換算幀率設為 {fps}")
        return {"type": "ack", "id": request_id, "frame_rate": fps}

    if cmd == "get_settings":
        return {
            "type": "settings",
            "id": request_id,
            "settings": await cam_read_settings(),
        }

    if cmd == "set_settings":
        changes = req.get("settings") or {}
        try:
            result = await cam_apply_settings(changes)
        except (SettingError, movie_settings.MovieSettingError) as e:
            return {"type": "error", "id": request_id, "error": str(e)}
        # 合法值會互相牽動：改了錄影格式，可選的幀率與位元深度就跟著變
        # （實測：record_format 1 -> 幀率少掉 29.97、位元深度清單整個消失）。
        # 所以套用後重讀能力並把新的 schema 一起回去，UI 不必自己猜。
        await refresh_capabilities()
        return {
            "type": "ack",
            "id": request_id,
            "applied": result["applied"],
            "rejected": result["rejected"],
            "settings": await cam_read_settings(),
            "schema": _full_schema(),
            "camera_mode": state.camera_mode,
        }

    if cmd == "set_position":
        result = await cam_set_position(int(req["position"]))
        return {
            "type": "ack",
            "id": request_id,
            "position": result.position,
            "requested": result.requested,
            "applied": result.applied,
            "clamped": result.clamped,
        }

    if cmd == "set_active_lens":
        lens_id = str(req["lens_id"])
        state.active_lens_id = lens_id
        return {
            "type": "ack",
            "id": request_id,
            "active_lens_id": lens_id,
            }

    return {"type": "error", "id": request_id, "error": f"unknown cmd: {cmd}"}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP REST handlers（給瀏覽器或非 WS client 用）
# ─────────────────────────────────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    html_file = _app_dir() / "static" / "index.html"
    if html_file.exists():
        return web.FileResponse(html_file)
    return web.Response(text="static/index.html not found", status=404)


async def handle_status(request: web.Request) -> web.Response:
    return web.json_response({
        "connected": state.camera_connected,
        # 卡住的話這裡會有訊息。/api/status 不碰相機，所以就算 worker 死了
        # 這個端點仍答得出來 —— 卡住時它是唯一還能講話的地方。
        "stuck": worker.stuck,
        # 拍攝進行到哪一步。卡住時這是唯一能指出「卡在哪個 PTP 呼叫」的線索。
        "capture_step": capture.current_step(),
        # 影片下載的進度。檔案大，沒有進度看起來就像當掉了。
        "movie_progress": getattr(state, "movie_progress", None),
        # 卡片剩餘空間、電池、鏡頭焦段範圍。一直都在 DataGroup1/3 裡沒被讀。
        "camera_status": getattr(state, "camera_status", None),
        "released": state.released_by_user,
        "camera_mode": state.camera_mode,
        "recording": state.recording,
        "focus_position": state.last_focus_position,
        "focus_state": state.last_focus_state,
        "focal_length_mm": state.last_lens_focal_mm,
        "focus_range": list(state.focus_range) if state.focus_range else None,
        "active_lens_id": state.active_lens_id,
        "ws_clients": len(state.ws_clients),
    })


def _enum_name(value):
    return getattr(value, "name", None) if value is not None else None


async def handle_focus_get(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    f = await cam_get_focus()
    return web.json_response({
        "focus_position": f.FocusPosition,
        "focus_state": f.FocusState,
        "focus_mode": _enum_name(f.FocusMode),
        "face_eye_af": _enum_name(getattr(f, "FaceEyeAF", None)),
        # 相機自己回報的偵測結果，唯讀
        "face_eye_detected": _enum_name(getattr(f, "FaceEyeAFStatus", None)),
        "focus_area": _enum_name(getattr(f, "FocusArea", None)),
        # (y, x)。座標系見 /api/focus/bounds
        "focus_point": getattr(f, "DMFPos", None),
        "point_size": getattr(f, "DMFSize", None),
        "continuous_af": _enum_name(getattr(f, "PreConstAF", None)),
        "af_lock": _enum_name(getattr(f, "AFLock", None)),
    })


async def handle_focus_bounds(request: web.Request) -> web.Response:
    """對焦點的座標範圍與各項可用值。"""
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    from sigma_ptpy.enum import FocusMode, FaceEyeAF, FocusArea

    def read_both():
        return (read_focus_area_bounds(state.camera),
                read_focus_choices(state.camera))

    bounds, choices = await worker.call(read_both, priority=Priority.STATUS)
    # 相機宣告的優先。列 enum 全部成員的話，會把相機不提供的東西畫成按鈕 ——
    # 實測 600 只有 [MF, AF_C, AF_S]，enum 裡的 AF(2) 按了永遠沒反應。
    return web.json_response({
        "point": bounds,
        "focus_modes": choices.get("focus_modes") or [m.name for m in FocusMode],
        "face_eye_options": choices.get("face_eye_options") or [m.name for m in FaceEyeAF],
        "focus_areas": choices.get("focus_areas") or [m.name for m in FocusArea],
    })


async def handle_focus_mode(request: web.Request) -> web.Response:
    """切換對焦模式 / 臉眼偵測 / 對焦區域 / 對焦點。

    這是拉過滑桿之後回到自動對焦的途徑 —— set_focus_position 為了不讓相機
    搶回焦點會強制寫 MF，而在這個端點出現之前沒有任何地方寫得回去。
    """
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)

    actions = []
    if "mode" in data:
        def apply_mode():
            want = data.get("continuous_af")
            if want is None:
                # Pre-AF 預設只跟 AF-C 走（開給 AF-S 會讓它一直獵取，
                # 表現得像 AF-C）。但臉／眼偵測需要 Pre-AF 才會運作 ——
                # 偵測開著時就不能關掉它，否則使用者每切一次對焦模式，
                # 臉部偵測就默默死一次。
                now = get_focus_state(state.camera)
                detecting = _enum_name(getattr(now, "FaceEyeAF", None)) not in (
                    None, "Off")
                if detecting:
                    want = True
            set_focus_mode(state.camera, data["mode"], want)
        actions.append(apply_mode)
    if "face_eye_af" in data:
        actions.append(lambda: set_face_eye_af(state.camera, data["face_eye_af"]))
        # 相機的臉／眼偵測只在 Pre-AF 開著時才運作。實測：關著時
        # FaceEyeAFStatus 永遠是 0，開了之後才回報偵測到臉 —— 而使用者
        # 看到的是「選了 Face Only 卻沒有任何反應」。
        #
        # 這件事在 CINE 下特別容易發生：Pre-AF 先前只綁在 AF-C 上，而
        # 相機在 CINE 拒收 AF-C，所以臉部偵測永遠開不起來。
        #
        # 選「偵測臉」就是在說「請你去找臉」，那本來就蘊含要持續偵測。
        if data["face_eye_af"] not in (None, "Off") and "mode" not in data:
            actions.append(lambda: set_focus_mode(
                state.camera,
                getattr(get_focus_state(state.camera), "FocusMode", None) or "AF_S",
                continuous_af=True))
    if "focus_area" in data:
        actions.append(lambda: set_focus_area(state.camera, data["focus_area"]))
    if "point_size" in data:
        actions.append(lambda: set_focus_point_size(state.camera, data["point_size"]))
    if "point" in data:
        point = data["point"]
        if not (isinstance(point, (list, tuple)) and len(point) == 2):
            return web.json_response({"error": "point must be [y, x]"}, status=400)
        # 對焦點只在單點選擇下有意義 —— 多點模式相機會把它鎖在正中央
        # [340, 512]，於是點畫面任何位置都對焦在同一處。實測確認。
        # 使用者要的是「對這裡」，不是「先去改一個他沒聽過的模式」。
        if "focus_area" not in data:
            actions.insert(0, lambda: set_focus_area(state.camera, "OnePointSelection"))
        # 臉／眼偵測開著時，對焦點由相機決定 —— 指定座標就是在說「不要找臉，
        # 對這裡」。不關掉的話點了畫面完全沒有反應，而使用者根本不知道還有
        # 一個偵測模式擋在中間。跟上面那行同一個道理。
        if "face_eye_af" not in data:
            actions.insert(0, lambda: set_face_eye_af(state.camera, "Off"))
        # 指定對焦點就是在說「交給相機對這裡」，所以自動切回 AF ——
        # 跟「拉對焦滑桿自動切 MF」對稱，兩邊都是從動作推出意圖。
        # MF 下對焦點寫得進去但不會有任何作用，那才是真的沒反應。
        # 讀當下的，不要用 state.last_focus_mode —— 那是定時廣播更新的快取，
        # 剛拉完滑桿的那一刻它還是舊值。
        if "mode" not in data:
            def af_if_manual():
                now = getattr(get_focus_state(state.camera), "FocusMode", None)
                if getattr(now, "name", None) == "MF":
                    set_focus_mode(state.camera, "AF_S")
            actions.insert(0, af_if_manual)
        actions.append(lambda: set_focus_point(state.camera, point[0], point[1]))
    if not actions:
        return web.json_response(
            {"error": "nothing to apply (mode / face_eye_af / focus_area / point)"},
            status=400)

    # 改對焦目標之後要觸發一次 AF，否則設定生效而鏡頭不動（見 trigger_af）。
    # 移動對焦點如此，選臉／眼偵測也如此 —— 後者原本不在這個條件裡，所以
    # 使用者看到的是「選了 Face Only 完全沒反應」，而相機其實已經看到臉了。
    #
    # 關掉偵測不算：那不是「改對焦到別的東西上」。
    # 只在 AF 模式下做：MF 下觸發 AF 會把手動設好的位置搶走。
    refocus = (data.get("af_trigger", True)
               and ("point" in data
                    or data.get("face_eye_af") not in (None, "Off")))

    def apply_all():
        for action in actions:
            action()
        state_after = get_focus_state(state.camera)
        mode = getattr(state_after.FocusMode, "name", None)
        if refocus and mode and mode != "MF":
            trigger_af(state.camera)
            state_after = get_focus_state(state.camera)
        return state_after

    try:
        f = await worker.call(apply_all, priority=Priority.CONTROL)
    except (ValueError, KeyError) as e:
        # 值不合法是呼叫端的錯，不是伺服器壞掉。KeyError 也接住 ——
        # enum 查表失敗會丟它，而那同樣是「使用者給了不認得的值」。
        return web.json_response({"error": str(e)}, status=400)

    # 寫入後讀回 —— 相機會夾值，回報實際生效的才誠實
    return web.json_response({
        "ok": True,
        "focus_mode": _enum_name(f.FocusMode),
        "face_eye_af": _enum_name(getattr(f, "FaceEyeAF", None)),
        "focus_area": _enum_name(getattr(f, "FocusArea", None)),
        "focus_point": getattr(f, "DMFPos", None),
        "point_size": getattr(f, "DMFSize", None),
        "continuous_af": _enum_name(getattr(f, "PreConstAF", None)),
    })


async def handle_focus_post(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    data = await request.json()
    result = await cam_set_position(int(data["position"]))
    return web.json_response({
        "ok": True,
        "position": result.position,
        "requested": result.requested,
        "applied": result.applied,
        "clamped": result.clamped,
    })


async def handle_settings_schema(request: web.Request) -> web.Response:
    """所有可設定項目的中繼資料。

    會先重讀能力 —— 合法值隨其他設定變動（實測：UHD 下相機不開放調整
    色彩位元、幀率也只剩兩個）。用快取回答會讓這個端點跟 WebSocket 的
    ack 給出不同答案，除錯時非常容易誤判。
    """
    if state.camera_connected:
        with suppress(Exception):
            await refresh_capabilities()
    return web.json_response({
        "settings": _full_schema(),
        "capabilities": state.capabilities,
        "movie_capabilities": state.movie_capabilities,
        "camera_mode": state.camera_mode,
        "shutter_unit": state.shutter_unit,
    })


async def handle_settings_get(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    return web.json_response({"settings": await cam_read_settings()})


async def handle_settings_post(request: web.Request) -> web.Response:
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    data = await request.json()
    changes = data.get("settings", data)
    result = await cam_apply_settings(changes)
    # 跟 WebSocket 那條路徑保持一致：套用後重讀能力
    with suppress(Exception):
        await refresh_capabilities()
    return web.json_response({
        "ok": not result["rejected"],
        "applied": result["applied"],
        "rejected": result["rejected"],
        "settings": await cam_read_settings(),
    })


async def handle_record(request: web.Request) -> web.Response:
    """開始 / 停止錄影，或錄一小段並回報產出的檔案。

    POST /api/record/start | /api/record/stop | /api/record/clip?seconds=1.5
    """
    action = request.match_info.get("action")
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)

    # 這些操作走的是未文件化 / 未驗證的協定路徑，例外要看得見而不是回 500
    try:
        return await _do_record(action, request)
    except Exception as e:
        log.warning(f"錄影操作 {action} 失敗：{type(e).__name__}: {e}")
        return web.json_response(
            {"error": f"{type(e).__name__}: {e}", "action": action}, status=502
        )


async def _do_record(action: str, request: web.Request) -> web.Response:

    if action == "start":
        if state.recording:
            return web.json_response(
                {"error": "already recording", "recording": True}, status=409)
        await worker.call(lambda: recording.start(state.camera), priority=Priority.CONTROL)
        state.recording = True
        state.recording_started_at = time.monotonic()
        await broadcast_state({"event": "recording_started"})
        return web.json_response({"ok": True, "recording": True})

    if action == "stop":
        await worker.call(lambda: recording.stop(state.camera), priority=Priority.CONTROL)
        state.recording = False
        state.recording_started_at = None
        await broadcast_state({"event": "recording_stopped"})
        return web.json_response({"ok": True, "recording": False})

    if action == "clip":
        if state.recording:
            return web.json_response(
                {"error": "already recording - stop first", "recording": True}, status=409)
        seconds = float(request.query.get("seconds", 1.5))
        # 上限存在的理由：這會寫到使用者的記憶卡上。要長時間錄製請用
        # start/stop，那樣使用者看得到自己在錄。
        if not 0 < seconds <= 30:
            return web.json_response(
                {"error": "seconds must be 0-30; use start/stop for longer takes"},
                status=400)
        state.recording = True
        state.recording_started_at = time.monotonic()
        await broadcast_state({"event": "recording_started"})
        try:
            result = await worker.call(
                lambda: recording.record_clip(state.camera, seconds),
                priority=Priority.CONTROL,
            )
        finally:
            state.recording = False
            state.recording_started_at = None
            await broadcast_state({"event": "recording_stopped"})
        return web.json_response({
            "ok": True,
            "seconds": result["seconds"],
            "listing_supported": result["listing_supported"],
            "new_files": [
                {"filename": e.filename, "size": e.size,
                 "format": f"0x{e.format_code:04x}"}
                for e in result["new"]
            ],
            "movie_info": result["movie_info"],
            "status_before": result["status_before"],
            "status_after": result["status_after"],
            "produced_something": result["produced_something"],
        })

    if action == "clear":
        # image_id 給了就只清那一筆。用途是「下載完一段就釋放，讓下一段遞補」——
        # 0x9037 只服務一個位置，逐筆釋放是唯一不必 release+acquire 就能換
        # 下一段的方法（前提是它服務的是 head 而不是固定的 0，尚未確認）。
        single = request.query.get("image_id")
        if single is not None:
            try:
                image_id = int(single)
            except ValueError:
                return web.json_response({"error": "image_id must be an integer"}, status=400)

            def clear_one():
                capture.clear_slot(state.camera, image_id)
                return recording.capture_status(state.camera)

            after = await worker.call(clear_one, priority=Priority.CONTROL)
            return web.json_response({"ok": True, "cleared": image_id, "status": after})

        # 復原用：資料庫項目沒被釋放會累積，塞滿之後相機就拍不成了。
        # 實際發生過 —— tail 累積到 6、slot 0 卡在 ImageGenFailed。
        def clear_all():
            st = recording.capture_status(state.camera)
            tail = st.get("db_tail") or 0
            for i in range(0, int(tail) + 1):
                capture.clear_slot(state.camera, i)
            return recording.capture_status(state.camera)

        after = await worker.call(clear_all, priority=Priority.CONTROL)
        return web.json_response({"ok": True, "status": after})

    if action == "status":
        # image_id 可指定 —— 狀態是分項目的，查 0 不一定看得到待取那筆
        try:
            image_id = int(request.query.get("image_id", 0))
        except ValueError:
            return web.json_response({"error": "image_id must be an integer"}, status=400)
        info = await worker.call(
            lambda: recording.capture_status(state.camera, image_id),
            priority=Priority.STATUS,
        )
        return web.json_response(info)

    if action == "movie_info":
        info = await worker.call(
            lambda: recording.describe_last_movie(state.camera), priority=Priority.STATUS
        )
        return web.json_response(info)

    if action == "download":
        # 影片下載。檔案很大（FHD 三秒約 19 MB，UHD 更多），所以走 CONTROL
        # 優先權，並把進度記在 state 上讓 /api/status 看得到。
        if state.recording:
            # 錄影中要求傳輸會讓相機進入不再服務影片傳輸的狀態，實測重現過。
            # 那個狀態只有把相機斷電才會好，所以這裡直接擋掉。
            return web.json_response(
                {"error": "cannot download while recording - measured to put the "
                          "camera in a state only a power cycle clears. Stop first."},
                status=409)

        movies = await worker.call(
            lambda: recording.movie_files(state.camera), priority=Priority.STATUS)
        if not movies:
            # 沒有影片還發 0x9037，相機會 USB 逾時掉線，只能斷電。防線不是提示。
            return web.json_response(
                {"error": "the camera has no movie to serve. release + acquire to "
                          "reset the database, then record - that take lands at "
                          "index 0, the only one that can be downloaded."},
                status=404)

        if not any(m.index == 0 for m in movies):
            # 0x9037 只服務索引 0。資料庫 head 前進之後，新錄的那段就下載不了。
            return web.json_response(
                {"error": "no movie at database index 0, which is the only one "
                          "0x9037 serves (found at "
                          + ", ".join(str(m.index) for m in movies) + "). POST "
                          "/api/release then /api/acquire to reset the database, "
                          "then record."},
                status=409)
        try:
            index = int(request.query.get("index", 0))
        except ValueError:
            return web.json_response({"error": "index must be an integer"}, status=400)
        if not 0 <= index < len(movies):
            return web.json_response(
                {"error": f"index out of range ({len(movies)} file(s))"}, status=400)
        if movies[index].index != 0:
            return web.json_response(
                {"error": f"this one is at database index {movies[index].index}; "
                          "only index 0 can be downloaded"}, status=409)

        save = request.query.get("save", "1") not in ("0", "false", "no")
        movies_dir = STATE_DIR / "movies"

        def report(done, total):
            state.movie_progress = {"done": done, "total": total}

        # 下載是長時間操作，不能套用一般指令的 120 秒上限 —— 30 秒的 FHD
        # 影片就有約 290 MB，光是傳輸就會超過，會被自己的看門狗砍掉。
        # 依大小給預算，並假設最差 0.5 MB/s（實測 2.4 MB/s，留了近五倍餘裕）。
        budget = 120.0 + movies[index].size / (512 * 1024)

        try:
            movie = await worker.call(
                lambda: recording.download_movie(
                    state.camera, movies[index],
                    movies_dir if save else None, progress=report),
                priority=Priority.CONTROL, timeout=budget)
        except recording.RecordingError as e:
            return web.json_response({"error": str(e)}, status=502)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)
        finally:
            state.movie_progress = None

        if save and movie.data:
            _restore_ownership(movies_dir)
            with suppress(Exception):
                _restore_ownership(movies_dir / movie.filename)

        out = movie.as_dict()
        out["ok"] = True
        if save and movie.data:
            out["saved_to"] = str(movies_dir / movie.filename)
        return web.json_response(out)

    if action == "movies":
        movies = await worker.call(
            lambda: recording.movie_files(state.camera), priority=Priority.STATUS)
        return web.json_response({"movies": [m.as_dict() for m in movies]})

    if action == "files":
        entries = await worker.call(
            lambda: recording.list_objects(state.camera), priority=Priority.STATUS
        )
        return web.json_response({
            "files": [{"filename": e.filename, "size": e.size,
                       "format": f"0x{e.format_code:04x}"} for e in entries]
        })

    return web.json_response({"error": f"unknown action: {action}"}, status=404)


async def handle_capture(request: web.Request) -> web.Response:
    """拍一張，並把影像抓回電腦。

    dest_to_save 不影響這裡拿不拿得到影像 —— 四個值（含 Null 與 InCamera）
    都實測過，每個都拍成並下載成功。它控制的是要不要順便寫進記憶卡：
    InCamera／Both 會寫，Null／InComputer 不寫（對卡驗證過）。想連機拍攝
    又不佔卡就設 InComputer。

    以上是**靜態影像**的結論。錄影下的行為還沒有有效資料 —— 唯一做過的對照
    因為查錯資料庫索引而作廢。
    """
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    save = request.query.get("save", "1") not in ("0", "false", "no")
    autofocus = request.query.get("af", "0") in ("1", "true", "yes")
    fetch = request.query.get("fetch", "1") not in ("0", "false", "no")
    # 這兩個開關只在要觀察相機對「項目沒釋放」的原始反應時才關掉
    release_stale = request.query.get("release_stale", "1") not in ("0", "false", "no")
    release = request.query.get("release", "1") not in ("0", "false", "no")

    photos = STATE_DIR / "photos"
    try:
        image = await worker.call(
            lambda: capture.capture(state.camera, photos if save else None,
                                    autofocus=autofocus, fetch=fetch,
                                    release_stale=release_stale, release=release),
            priority=Priority.CONTROL,
        )
    except capture.CaptureError as e:
        return web.json_response({"error": str(e)}, status=502)
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)

    # DNGAndJPEG 一次拍攝會產生兩個檔案，兩個都要把擁有者改回使用者
    written = [f for f in [image, *image.companions] if f.data]
    if save and written:
        _restore_ownership(photos)
        for one in written:
            with suppress(Exception):
                _restore_ownership(photos / one.filename)

    out = image.as_dict()
    out["ok"] = True
    if save and written:
        out["saved_to"] = str(photos / image.filename)
        out["saved_files"] = [str(photos / one.filename) for one in written]
    return web.json_response(out)


async def handle_probe_movie(request: web.Request) -> web.Response:
    """把單一 tag 寫進 DataGroupMovie，並回報前後的完整內容。

    協定探測用。DataGroupMovie 有幾個欄位（tag 1 / 5 / 6）對任何已知設定
    都不反應，意義不明；要判斷它們是什麼，只剩下「寫寫看有什麼變化」這條路
    —— 機身端的變更會被 config_api 重置，而非 API 模式下相機根本不回應。

    刻意不做合法值檢查（那正是要探測的），但型別限制在小範圍內，
    而且會回傳寫入前後的內容讓呼叫端能還原。
    """
    if not state.camera_connected:
        return web.json_response({"error": "not connected"}, status=503)
    try:
        data = await request.json()
        tag = int(data["tag"])
        type_name = data.get("type", "UInt8")
        value = data["value"]
    except (KeyError, TypeError, ValueError) as e:
        return web.json_response({"error": f"tag / value required: {e}"}, status=400)

    def read_group():
        return {e.tag: e.values for e in parse_ifd(movie_settings.read_raw(state.camera)).entries}

    try:
        before = await worker.call(read_group, priority=Priority.STATUS)
        log.warning(f"探測寫入 DataGroupMovie tag {tag} = {value!r} ({type_name})")
        await worker.call(
            lambda: movie_settings.write_tag(state.camera, tag, type_name, value),
            priority=Priority.CONTROL,
        )
        after = await worker.call(read_group, priority=Priority.STATUS)
    except movie_settings.MovieSettingError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)

    changed = {k: [before.get(k), v] for k, v in after.items() if before.get(k) != v}
    return web.json_response({
        "ok": True, "tag": tag, "value": value,
        "changed": changed, "before": before, "after": after,
    })


async def handle_ptp_probe(request: web.Request) -> web.Response:
    """研究用：指定 opcode 和參數，把相機回的原始位元組拿回來。

    為了省掉「改程式 → 重啟 bridge → 看結果」這個循環。那個循環貴到會讓人
    先寫好假設再驗證，而這個專案幾乎每個錯誤結論都是這樣來的。

    GET  /api/probe/ptp                     列出認得的 opcode
    POST /api/probe/ptp {"opcode":..., "params":[...]}
    """
    if request.method == "GET":
        return web.json_response(
            {"opcodes": {k: f"0x{v:04x}" for k, v in
                         sorted(ptp_probe.known_opcodes().items(), key=lambda kv: kv[1])}})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON body required"}, status=400)

    opcode = body.get("opcode")
    if isinstance(opcode, str) and opcode.startswith("0x"):
        try:
            opcode = int(opcode, 16)          # 探未文件化指令用
        except ValueError:
            return web.json_response({"error": "malformed hex opcode"}, status=400)
    if not isinstance(opcode, (str, int)):
        return web.json_response({"error": "opcode must be a name or a number"}, status=400)
    params = body.get("params") or []
    if not isinstance(params, list) or not all(isinstance(x, int) for x in params):
        return web.json_response({"error": "params must be an array of integers"}, status=400)

    cam = state.camera or state.released_camera
    if cam is None:
        return web.json_response({"error": "not connected"}, status=503)

    if state.recording and opcode in ("SigmaGetPartialMovieFile", 0x9037):
        # 實測兩次都出事：一次讓相機不再服務影片傳輸，一次直接 USBTimeoutError
        # 之後從 USB 上掉線。這是研究端點，但這個組合會弄壞硬體狀態，擋掉。
        return web.json_response(
            {"error": "cannot send SigmaGetPartialMovieFile while recording - "
                      "measured to stop the camera serving transfers, or drop it "
                      "off USB entirely."},
            status=409)

    try:
        raw = await worker.call(
            lambda: ptp_probe.recv_raw(cam, opcode, params),
            priority=Priority.STATUS, needs_camera=False)
    except ptp_probe.ProbeError as e:
        return web.json_response({"error": str(e)}, status=502)
    except Exception as e:
        return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)

    out = ptp_probe.describe(raw)
    out["opcode"] = opcode if isinstance(opcode, str) else f"0x{opcode:04x}"
    out["params"] = params
    return web.json_response(out)


async def handle_event_probe(request: web.Request) -> web.Response:
    """研究用：收集相機主動發出的 PTP 事件。

    不對相機發任何指令 —— ptpy 已經有背景執行緒在輪詢事件，這裡只是把佇列
    讀乾淨。所以錄影期間也可以安全呼叫，而且不需要排進相機工作佇列。
    """
    cam = state.camera or state.released_camera
    if cam is None:
        return web.json_response({"error": "not connected"}, status=503)
    try:
        seconds = min(10.0, max(0.1, float(request.query.get("seconds", 2))))
    except ValueError:
        return web.json_response({"error": "seconds must be a number"}, status=400)

    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(
        None, lambda: ptp_probe.drain_events(cam, seconds))
    return web.json_response({"seconds": seconds, "count": len(events),
                              "events": events})


async def handle_dump(request: web.Request) -> web.Response:
    """把 CanSetInfo5 / DataGroupMovie 的原始 IFD 以 JSON 吐出來。

    純唯讀的協定探勘端點。走 HTTP 的意義在於：相機被 bridge 佔著的時候，
    CLI 版的 --dump-* 根本連不上；而且 HTTP 不需要 root。
    """
    which = request.match_info.get("which", "info5")

    # 交還期間也允許讀取。那個窗口裡相機處於「使用者自己的設定」狀態，
    # 而重新進入 API 模式會把設定重置掉 —— 想觀察機身端的變更，
    # 只有這個窗口看得到。相機不一定會在非 API 模式下回應，失敗就回錯誤。
    cam = state.camera or state.released_camera
    if cam is None:
        return web.json_response({"error": "not connected"}, status=503)
    released = state.camera is None

    if which == "pict":
        # PictFileInfo2 不是 IFD，是固定結構，所以不能走下面的 parse_ifd。
        # 回原始位元組加上 sigma-ptpy 現行結構的切法，兩者並排才看得出
        # DNGAndJPEG 模式差在哪。
        try:
            raw = await worker.call(lambda: capture.pict_file_info_raw(cam),
                                    priority=Priority.STATUS, needs_camera=False)
        except Exception as e:
            return web.json_response(
                {"error": f"{type(e).__name__}: {e}", "released": released}, status=502)
        fields = {}
        if len(raw) >= 24:
            fields = {
                "_Unknown0": raw[0:12].hex(),
                "FileAddress": int.from_bytes(raw[12:16], "little"),
                "FileSize": int.from_bytes(raw[16:20], "little"),
                "PathNameOffset": int.from_bytes(raw[20:24], "little"),
            }
        return web.json_response({
            "bytes": len(raw), "raw_hex": raw.hex(),
            "as_current_schema": fields,
            "ascii": "".join(chr(b) if 32 <= b < 127 else "." for b in raw),
            "released": released,
        })

    readers = {"info5": read_info5_raw, "movie": read_movie_group_raw}
    reader = readers.get(which)
    if reader is None:
        return web.json_response(
            {"error": f"unknown dump: {which} (available: {', '.join(readers)}, pict)"},
            status=404
        )

    try:
        raw = await worker.call(lambda: reader(cam), priority=Priority.STATUS,
                                needs_camera=False)
    except Exception as e:
        return web.json_response(
            {"error": f"{type(e).__name__}: {e}", "released": released},
            status=502)

    if not raw:
        return web.json_response(
            {"error": "the camera returned an empty payload", "raw_hex": "", "released": released})
    try:
        out = to_json(parse_ifd(raw))
        out["released"] = released
        return web.json_response(out)
    except Exception as e:
        return web.json_response({"error": str(e), "raw_hex": raw.hex(),
                                  "released": released})


async def handle_release(request: web.Request) -> web.Response:
    await release_camera()
    return web.json_response({"ok": True, "released": True})


async def handle_acquire(request: web.Request) -> web.Response:
    restore = request.query.get("restore", "1") not in ("0", "false", "no")
    ok = await acquire_camera(restore=restore)
    return web.json_response({"ok": ok, "released": not ok, "connected": ok})


async def handle_liveview_ws(request: web.Request) -> web.WebSocketResponse:
    """影格 + 中繼資料的 WebSocket 串流。

    跟 /liveview.mjpeg 同一個來源，差別在每張影格前面掛一段 JSON：抓的時
    間、當時命令的對焦位置、以及那個位置已經維持多久。

    為什麼這件事值得一個新端點：自動對焦要判斷「這張影像反映的是不是我剛
    下的位置」。沒有這個資訊就只能等一個保守的固定時間（實測相機管線延遲
    193 ms），而那段等待佔掉對焦迴路將近一半的時間。有了 pos_age_ms，消費
    端可以一邊下新指令一邊繼續收前一個位置的影格，靠標籤分類 —— 死區從
    每個週期的成本變成一個固定的落後。

    每則訊息是二進位：

        4 bytes   JSON 標頭長度（big-endian uint32）
        N bytes   JSON 標頭
        剩下      JPEG

    合成一則而不是分兩則，是因為兩則訊息之間沒有原子性保證 —— 客戶端
    重連或掉訊息時會錯位，而錯位的後果是「用錯的位置標籤去解讀影像」。
    """
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    state.mjpeg_clients.add(ws)
    log.info(f"liveview WS client 連入（共 {len(state.mjpeg_clients)}）")
    last_seq = frames.seq
    try:
        while not ws.closed:
            try:
                last_seq, jpeg, meta = await asyncio.wait_for(
                    frames.wait_for_next_meta(last_seq), timeout=MJPEG_IDLE_CHECK_S)
            except asyncio.TimeoutError:
                continue
            header = json.dumps(meta, separators=(",", ":")).encode()
            await ws.send_bytes(len(header).to_bytes(4, "big") + header + jpeg)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        state.mjpeg_clients.discard(ws)
        log.info(f"liveview WS client 離開（剩 {len(state.mjpeg_clients)}）")
    return ws


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

    def peer_gone() -> bool:
        return request.transport is None or request.transport.is_closing()

    # 從目前這張開始等下一張，不重播已經發過的影格
    last_seq = frames.seq
    try:
        while True:
            try:
                last_seq, jpeg = await asyncio.wait_for(
                    frames.wait_for_next(last_seq), timeout=MJPEG_IDLE_CHECK_S
                )
            except asyncio.TimeoutError:
                # 沒有新影格（相機斷線、或還沒接上）。這條路上不會有寫入，
                # 也就不會靠寫入失敗發現對方已經走了 —— 得主動檢查，
                # 否則這個 handler 會變成永遠不結束的幽靈，
                # 害 mjpeg_clients 一直不歸零、關機時也收不掉。
                if peer_gone():
                    break
                continue
            if peer_gone():
                break
            chunk = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpeg)}\r\n\r\n"
            ).encode() + jpeg + b"\r\n"
            await resp.write(chunk)
            # 寫入期間 producer 可能已經又發了好幾張；下一輪 wait_for_next
            # 會直接拿最新的那張，中間的自動跳過。
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception as e:
        log.debug(f"MJPEG client 寫入失敗：{e}")
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

@web.middleware
async def camera_unavailable_middleware(request: web.Request, handler):
    """相機在請求處理到一半掉線時，回 503 而不是 500。

    handler 開頭雖然有檢查 camera_connected，但那之後到 job 真正執行之間
    還是有空窗，斷線剛好落在裡面時就靠這層兜住。
    """
    # 卡住時各 handler 開頭的 camera_connected 檢查會先觸發，回報「not
    # connected」—— 那是誤導，相機還在，是我們自己的執行緒回不來。在這裡先攔。
    # /api/status 例外：它不碰相機，卡住時就靠它把狀況講出來。
    if worker.stuck is not None and request.path != "/api/status":
        return web.json_response(
            {"error": worker.stuck, "stuck": True,
             "capture_step": capture.current_step(),
             "recovery": "restart the bridge"},
            status=503)
    try:
        return await handler(request)
    except CameraUnavailable as e:
        return web.json_response({"error": str(e)}, status=503)
    except CameraStuck as e:
        # 不可復原，所以回覆裡直接講出唯一的解法，別讓人以為重試會有用
        return web.json_response(
            {"error": str(e), "stuck": True, "recovery": "restart the bridge"},
            status=503)
    except (SettingError, movie_settings.MovieSettingError) as e:
        # 設定名稱或值不合法是呼叫端的錯，不是伺服器壞掉
        return web.json_response({"error": str(e)}, status=400)


def make_app() -> web.Application:
    app = web.Application(middlewares=[camera_unavailable_middleware])
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/probe/ptp", handle_ptp_probe)
    app.router.add_get("/api/probe/events", handle_event_probe)
    app.router.add_post("/api/probe/ptp", handle_ptp_probe)
    app.router.add_get("/api/focus", handle_focus_get)
    app.router.add_post("/api/focus", handle_focus_post)
    app.router.add_get("/api/focus/bounds", handle_focus_bounds)
    app.router.add_post("/api/focus/mode", handle_focus_mode)
    app.router.add_get("/api/settings/schema", handle_settings_schema)
    app.router.add_get("/api/settings", handle_settings_get)
    app.router.add_post("/api/settings", handle_settings_post)
    app.router.add_get("/api/dump/{which}", handle_dump)
    app.router.add_post("/api/capture", handle_capture)
    app.router.add_post("/api/probe/movie", handle_probe_movie)
    app.router.add_post("/api/record/{action}", handle_record)
    app.router.add_get("/api/record/{action}", handle_record)
    app.router.add_post("/api/release", handle_release)
    app.router.add_post("/api/acquire", handle_acquire)
    app.router.add_get("/liveview.mjpeg", handle_liveview)
    app.router.add_get("/ws/liveview", handle_liveview_ws)
    return app


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    # 把 ptpy 那堆 EvtPolling timeout 噪音降級為 DEBUG（不顯示）
    logging.getLogger("ptpy.transports.usb").setLevel(logging.CRITICAL)

    # 載入校準表

    # worker 必須先起來——連相機本身也是一個 job
    worker.start()

    await try_connect_camera()
    state.reconnect_task = asyncio.create_task(reconnect_loop())
    polling_task = asyncio.create_task(state_polling_loop())
    liveview_task = asyncio.create_task(liveview_loop())

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
        # 先停掉會餵 job 的迴圈，再停 worker，最後才關相機
        for t in (polling_task, liveview_task, state.reconnect_task):
            t.cancel()
        for t in (polling_task, liveview_task, state.reconnect_task):
            with suppress(asyncio.CancelledError):
                await t
        await worker.stop()
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
