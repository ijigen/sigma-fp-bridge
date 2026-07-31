# sigma-fp-focus-bridge

USB-driven focus control for **Sigma fp** via the camera's official PTP protocol.
Drives the lens's internal motor directly — no external follow-focus rig.

![demo](docs/demo.gif) <!-- 影片連結之後補 -->

> **🤖 Built by pair-programming with [openclaw](https://github.com/openclaw)**, an AI agent harness.
> Most of the protocol reverse-engineering and library patching in this repo came from a Claude-driven agent stepping through PTP details much faster than I could solo.

---

## What this is

A Python bridge server that talks to Sigma fp over USB PTP and exposes:

- **WebSocket API** for real-time focus control (`set_position`, `get_state`, calibration)
- **REST API** for one-shot HTTP commands
- **MJPEG live view stream** browsers / OpenCV / OBS can consume
- **Bonjour / mDNS broadcast** so iOS clients can auto-discover the bridge
- **Browser test UI** with live preview, slider, and calibration table

You point your camera at something, slide a number on the screen, and the lens motor moves to that position.

## Why this exists

Almost every "follow focus" solution for fp uses an external motor clamped on the focus ring (DJI Focus Pro, Tilta Nucleus, PDMovie). This works directly through the **lens's internal stepper / linear actuator** over USB — no rigging, no mechanical interface.

Useful for:
- DIY autofocus assist rigs (LiDAR / face tracking driving focus)
- Timelapse with programmed focus pulls
- Bullet-time or motion-control setups
- Anyone allergic to strapping motors onto their lens

## Status

| Component | Status |
|---|---|
| Mac bridge (Python) | ✅ Working |
| Browser UI | ✅ Working |
| WebSocket + REST + MJPEG | ✅ Working |
| Calibration persistence | ✅ Working |
| Bonjour mDNS | ✅ Working |
| iOS app | ❌ Not yet |
| Linux / Windows bridge | ⚠ Should work, not tested |

## Tested with

- **Camera**: Sigma fp, firmware **Ver. 5.02** (latest, 2023-08-03)
- **Lens**: **Sigma 28mm F1.4 DG DN | Art** (HLA motor) — confirmed
- **Host**: macOS Sequoia 15.x, Apple Silicon, Python 3.9

If you test other configurations, please open an issue or PR.

---

## Quickstart (macOS)

### 1. Prerequisites

```bash
xcode-select --install  # for git + system python3
```

**Homebrew is not required.** `requirements.txt` pulls in
[`libusb-package`](https://pypi.org/project/libusb-package/), which bundles a
libusb binary; the bridge falls back to it whenever the system libusb isn't
found. `brew install libusb` still works if you prefer a system one.

> This fallback matters more than it looks: **`sudo` strips `DYLD_*` environment
> variables**, so the `DYLD_FALLBACK_LIBRARY_PATH` that `run_mac.sh` exports to
> locate a Homebrew libusb is silently discarded under root — and root is exactly
> what you need to claim the camera. The bundled library is loaded by absolute
> path, so it survives.

### 2. Clone + setup

```bash
git clone https://github.com/<you>/sigma-fp-focus-bridge.git
cd sigma-fp-focus-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` includes [sigma-ptpy from git](https://github.com/makanikai/sigma-ptpy) because the PyPI version is too old.

### 3. Camera setup

- Power on, USB-C to your Mac
- **Menu → SYSTEM → USB Mode → PTP** (not Mass Storage, not Webcam)
- **Menu → Custom Settings → Linear Focus → ON**
- Set the camera to **CINE** (movie) mode
- **Lens physical AF/MF switch → AF** (counter-intuitive, see below)

### 4. Run as root

macOS binds PTP-class cameras to `ptpcamerad` at USB enumeration. Detaching that
binding uses IOKit's device-capture API, **which requires root** — so just run
with `sudo`:

```bash
sudo ./run_mac.sh
```

That's it. The system daemons keep running; Photos, Image Capture, and iPhone
backup all keep working. If root still can't claim the device, see
[Gotcha 5](#gotcha-5-macos-binds-ptp-cameras-at-enumeration) for the fallback.

### 5. Verify

```bash
sudo .venv/bin/python sigma_fp_focus.py --dump-info5
```

Read-only — it never drives the motor. Prints the mounted lens, current focus
state, and the focus position range. Good first check that everything is wired
up before starting the bridge.

Then start the bridge. You should see:

```
INFO sigma-bridge | 相機已連線
INFO sigma-bridge | 瀏覽器測試: http://192.168.x.x:8765/
```

Open `http://localhost:8765/` and drag the focus slider. The lens should move in real-time.

Nothing to undo afterwards — no system state was changed.

---

## The five gotchas

Things that cost hours to figure out. Sharing so you don't have to repeat them.

### Gotcha 1: SDK doc tags are decimal, NOT hex

The official SDK doc lists `DataGroupFocus` tags as:

```
0001 Focus Mode
0002 AF Lock
0010 Focus Area
0080 Focus State (Read-Only)
0081 Focus Position
```

These look like 4-digit hex codes. **They are not.** They're decimal numbers with leading zeros for visual alignment.

- `0081` = decimal `81` (= `0x51` hex)
- **NOT** `0x81` hex (= decimal `129`)

I lost about two hours sending Tag 129 and wondering why the camera ignored every command.

### Gotcha 2: Linear Focus must be enabled

Without **Menu → Custom Settings → Linear Focus → ON**, the lens motor silently ignores PTP focus commands. The camera replies "OK", but nothing moves.

Linear Focus was originally a video shooter setting (consistent focus pull speed regardless of ring rotation speed). It also turns out to be the master gate for accepting electronic focus position commands.

### Gotcha 3: Lens physical AF/MF switch must be on AF

This one is genuinely backwards from intuition:

- **Lens switch on AF**: PTP commands work. Camera body controls focus, you can override programmatically (set FocusMode=MF via PTP, then set FocusPosition).
- **Lens switch on MF**: PTP commands silently ignored. The lens treats the physical ring as the only valid input.

So: leave the physical switch on **AF** at all times, and let PTP handle the "MF" mode switching in software.

### Gotcha 4: the camera body locks up while the bridge is connected

This one is by design, and it surprises everyone. From Sigma's own SDK
documentation for `sgm_ConfigApi` — the first command any API client must send:

> When this function is executed, API resets the camera setting to the default.
> (When API connection is closed, the camera setting returns to the setting value
> which the user specified before using API.) **Furthermore, API does not accept
> any operation other than the power-off operation.**

So while the bridge holds the camera, **every physical control on the body stops
responding** — dials, buttons, touchscreen, the lot. Only the power switch works.
There is no way around this: it's the cost of entering API control mode, not
something the bridge chooses.

Two consequences worth internalising:

- **Your camera settings are reset to defaults** on connect, and only restored
  when the API connection is closed *properly*.
- **Closing the PTP session is not enough.** The camera leaves API mode on
  `sgm_CloseApplication` (or a USB disconnect). `close_camera()` sends it before
  closing the session — if you write your own client, don't skip it, or the only
  way to get the body back is unplugging the cable or power-cycling.

Shut the bridge down with Ctrl+C and control returns cleanly.

### Gotcha 5: macOS binds PTP cameras at enumeration

When you plug in any PTP-class USB camera, macOS binds it to system daemons that
claim the device's IOKit interface:

- `/usr/libexec/ptpcamerad`
- `/usr/libexec/cameracaptured`
- `mscamerad-xpc` (Image Capture XPC service)

While they hold the device, an unprivileged `libusb_detach_kernel_driver()` fails
and the bridge can't acquire the camera.

**The fix is just `sudo`.** On macOS, libusb implements detach via IOKit's device
capture API (`USBDeviceReEnumerate` with the capture mask), and **that API
requires root**. An unprivileged detach failing is expected behaviour, not macOS
blocking you. Verified on macOS Sequoia with all three daemons running normally
(libusb 1.0.30) — root claimed the camera without touching them.

Two things that do *not* work, in case you're tempted:

1. **`launchctl disable` doesn't stick** on SIP-enabled macOS for these services.
2. **`killall ptpcamerad` doesn't help** — launchd respawns it instantly on the
   next USB hotplug.

<details>
<summary>Fallback: pausing the daemons (only if root still fails)</summary>

`SIGSTOP` works where `killall` doesn't — pausing leaves launchd thinking the
process is alive (so no respawn), but a paused process can't claim USB.

```bash
sudo killall -STOP ptpcamerad cameracaptured mscamerad-xpc
```

**Run this BEFORE plugging in the camera**, or unplug-replug afterwards — the
IOKit binding happens during USB enumeration.

> 🚨 **This pollutes your macOS environment.** While the daemons are stopped,
> Photos / Image Capture / Finder / iCloud import all stop recognizing your
> camera, and iPhone backup over USB breaks. Always restore with
> `sudo killall -CONT ptpcamerad cameracaptured mscamerad-xpc` when you finish.

</details>

---

## sigma-ptpy patches

[`sigma-ptpy`](https://github.com/makanikai/sigma-ptpy) is a great library but was written against pre-v3.00 SDK and doesn't support the new focus tags. We monkey-patch it.

### Add `FocusPosition` (Tag 81) + `FocusState` (Tag 80)

```python
import sigma_ptpy.sigma_ptpy as _sigma_ptpy_module
import sigma_ptpy.schema as _sigma_schema_module
from sigma_ptpy.schema import CamDataGroupFocus, DirectoryType

class CamDataGroupFocusExt(CamDataGroupFocus):
    def __init__(self, FocusPosition=None, FocusState=None, **kwargs):
        super().__init__(**kwargs)
        self.FocusPosition = FocusPosition
        self.FocusState = FocusState

    def encode(self):
        data = []
        # ... copy the parent's existing tag encoding ...
        if self.FocusPosition is not None:
            # Tag 81 (decimal), Type UInt16, Count 1
            data.append((81, DirectoryType.UInt16, self.FocusPosition))
        return self._encode(data)

    def decode(self, rawdata):
        super().decode(rawdata)
        for tag, val in self._decode(rawdata):
            if tag == 80:
                self.FocusState = val[0]
            elif tag == 81:
                self.FocusPosition = val[0]

# Replace in the module so cam.get_cam_data_group_focus() returns the Ext class
_sigma_ptpy_module.CamDataGroupFocus = CamDataGroupFocusExt
_sigma_schema_module.CamDataGroupFocus = CamDataGroupFocusExt
```

Full version with proper logging and error handling in [`sigma_fp_focus.py`](./sigma_fp_focus.py).

### Use it

```python
from sigma_ptpy import SigmaPTPy
# (after patch is installed)

cam = SigmaPTPy()
with cam.session():
    cam.config_api()
    focus = CamDataGroupFocusExt(
        FocusMode=FocusMode.MF,
        AFLock=AFLock.Off,
        PreConstAF=PreConstAF.Off,
        FocusPosition=1500,  # tweak per lens; see below
    )
    cam.set_cam_data_group_focus(focus)
    # Lens motor starts moving.

    state = cam.get_cam_data_group_focus()
    print(state.FocusPosition, state.FocusState)  # current pos, 0=Idle 1=Moving
```

### Focus position range — `CanSetInfo5` tag 658

Valid `FocusPosition` values differ per lens and per zoom position. The camera
reports the range in `CanSetInfo5`, but sigma-ptpy only decodes the tags it
already knows about and discards the raw payload — so we patch `decode()` to
keep the bytes and parse them ourselves (`ifd.py`).

```bash
sudo .venv/bin/python sigma_fp_focus.py --dump-info5
```

Read-only; it never drives the motor. Prints the mounted lens and current focus
state, then a hex dump and every directory entry, with tags in **both decimal and
hex** so you can cross-reference the SDK PDF either way.

**The tag is decimal `658`** — [Gotcha 1](#gotcha-1-sdk-doc-tags-are-decimal-not-hex)
again. The SDK doc writes it `0658`; hex `0x658` (= 1624) does not exist in the
payload. Two `UInt16`s, `(min, max)`. This matches how every other range in
`CanSetInfo5` is encoded — e.g. tag 340 is `(-50/10, 50/10, 2/10)`, i.e.
exposure compensation −5.0…+5.0 in 0.2 steps.

`get_focus_range(cam)` returns `(min, max)` and cross-checks the current position
against it.

**Measured ranges — please add yours:**

| Lens | Reported focal length | Focus position range |
|---|---|---|
| (unidentified, needs confirming) | 40.0 mm | 5974 – 11116 |

Confirmed on that lens by parking focus at the end of travel and reading back
`FocusPosition` — it returned exactly `11116`, the reported maximum.

If you run the dump, please open an issue with the output. The header block
identifies which lens the numbers belong to, so the whole dump is
self-describing.

---

## Lens compatibility (help wanted)

Sigma's official SDK supports Focus Position in principle, but the actual lens motor's response varies. Help us build the compatibility list.

| Lens | PTP Focus Position | Notes |
|---|---|---|
| Sigma 28mm F1.4 DG DN Art | ✅ Working | HLA motor |
| Sigma 17mm F4 DG DN Contemporary | ❓ Uncertain | May depend on AF/MF switch state |
| Unidentified, reports 40.0 mm | ✅ Reports range | 5974 – 11116; motor drive not yet retested |

**If you have an L-mount AF lens, please test and PR your result.**

Quick test:
1. Set up as in Quickstart
2. Open the browser UI
3. Set focus position to various values
4. Check whether the lens motor physically moves AND whether `Focus State` shows `1 (Moving)` then `0 (Idle)`

---

## Architecture

```
                    Mac running this bridge
        ┌─────────────────────────────────────────┐
        │  Python (aiohttp)                       │
        │  ├─ sigma-ptpy + monkey-patch           │
        │  ├─ camera worker (priority queue)      │
        │  ├─ WebSocket /ws    (real-time control)│
        │  ├─ REST /api/*      (one-shot HTTP)    │
        │  ├─ MJPEG /liveview.mjpeg (live view)   │
        │  ├─ Bonjour _sigmafp._tcp               │
        │  └─ Calibration JSON store              │
        └──────────────┬──────────────────────────┘
                       │ USB-C (PTP, opcodes 0x9012-0x9037)
                       ▼
                   Sigma fp
                       ▲
                       │ Wi-Fi / local network
        ┌──────────────┴──────────────────────────┐
        │  Clients                                │
        │  - Browser at http://host:8765/         │
        │  - Future: iOS app via Bonjour          │
        │  - Anything that speaks WebSocket / HTTP│
        └─────────────────────────────────────────┘
```

### Camera worker

USB PTP runs one transaction at a time, so camera access has to be serialized.
A single worker task owns the camera; everything else submits jobs to a priority
queue — **control > status > live view**. This matters because a plain lock is
first-come-first-served, which puts your focus command behind however many live
view frame requests happen to be queued.

Three consequences worth knowing:

- **Focus commands preempt live view.** Frame grabs never delay a focus move.
- **Rapid setpoints coalesce.** Drag a slider (or drive focus from ARKit at 30 Hz)
  and only the newest position is sent; the superseded ones are dropped before
  they reach USB. Focus position is absolute state, not an increment, so this is
  lossless in the sense that matters. Responses carry `"applied": false` when a
  command was superseded.
- **Live view is fanned out, not per-client.** One producer grabs each frame and
  publishes it to every MJPEG client. Two browser tabs cost one frame grab, not
  two. Clients that can't keep up skip to the newest frame instead of applying
  backpressure to the camera.

## File layout

```
.
├── sigma_fp_focus.py       Low-level: sigma-ptpy patch + helpers
├── mac_bridge_server.py    HTTP / WebSocket / MJPEG server
├── ifd.py                  Sigma PTP IFD parser (no sigma-ptpy dependency)
├── static/
│   └── index.html          Browser test UI
├── debug_encode.py         Standalone IFD encoder sanity test
├── diagnose.py             Dump all camera state for troubleshooting
├── tests/                  Runs against a fake camera — no hardware needed
├── run_mac.sh              One-shot launcher (auto-creates venv)
├── requirements.txt
└── README.md
```

### Tests

```bash
python3 tests/test_ifd.py      # IFD parser, pure data
python3 tests/test_bridge.py   # camera worker + HTTP/WS/MJPEG, fake camera
python3 tests/test_session.py  # camera session lifecycle
```

Both run without a Sigma fp attached. `tests/test_bridge.py` pins the properties
that concurrency bugs quietly break: focus commands preempting queued live view
frames, rapid setpoints coalescing into one USB write, MJPEG clients sharing one
frame stream, and live view surviving a motor-settle wait.

## API reference

### WebSocket (`/ws`)

```javascript
ws.send(JSON.stringify({cmd: "set_position", position: 1500}));
ws.send(JSON.stringify({cmd: "set_distance", distance: 2.5}));  // uses calibration table
ws.send(JSON.stringify({cmd: "get_state"}));
ws.send(JSON.stringify({cmd: "calibration_add", distance: 2.0, position: 1500}));
ws.send(JSON.stringify({cmd: "calibration_clear"}));
ws.send(JSON.stringify({cmd: "set_active_lens", lens_id: "28mm_art"}));
```

Server pushes a `state` message at ~10 Hz with current focus position / state /
mode, plus `focus_range` — the `(min, max)` the camera reports for the mounted
lens, or `null` if it doesn't report one. The `hello` message carries it too, so
a client knows the valid bounds before the first state push.

Positions outside the range are clamped to the nearest bound rather than sent to
the camera. Acks report what happened:

```json
{"type": "ack", "requested": 100, "position": 5974, "applied": true, "clamped": true}
```

`applied: false` means the command was superseded by a newer one before it
reached USB (see [camera worker](#camera-worker)).

### REST

```bash
curl http://localhost:8765/api/status
curl -X POST http://localhost:8765/api/focus  -d '{"position":1500}' -H "Content-Type: application/json"
curl -X POST http://localhost:8765/api/distance -d '{"distance":2.5}' -H "Content-Type: application/json"
curl http://localhost:8765/api/calibration
```

### Live view

```
GET http://localhost:8765/liveview.mjpeg
```

Plain MJPEG stream, ~25 fps. Drop into `<img src="...">` or read with OpenCV.

---

## Tips for replicating this

This was a few hours of dense debugging. If you're trying something similar:

- **Pair with an AI agent** (I used [openclaw](https://github.com/openclaw)). Stepping through PTP protocol bytes, monkey-patching libraries, and decoding obscure SDK doc traps is exactly the kind of work an agent can sprint through. Most of the "this can't work" → "oh actually" moments came from agent-driven hypothesis testing.
- **Read the SDK PDF carefully** for tag IDs. The decimal/hex trap (Gotcha 1) is in there if you look for it. We didn't.
- **Test with a known-good lens first** (like 28mm Art) to validate the protocol works, then try cheaper / questionable lenses.
- **Sniff USB traffic** if commands seem ignored — `usbmon` on Linux or USBPcap in a Windows VM is invaluable.

## License

MIT. See [LICENSE](./LICENSE).

## Acknowledgements

- [`sigma-ptpy`](https://github.com/makanikai/sigma-ptpy) by makanikai — the Python PTP library this builds on.
- Sigma for publishing the [Camera Control SDK](https://www.sigma-global.com/en/cameras/fp-series/download/sdk/) (even if the docs hide a few traps).
- [openclaw](https://github.com/openclaw) for the agent harness used to debug this.
