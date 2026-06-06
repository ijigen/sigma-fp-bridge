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
brew install libusb python@3.9
xcode-select --install  # for git
```

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

### 4. macOS PTP daemon workaround

> ⚠️ **This pollutes your macOS environment temporarily.** Read the warning at the end before proceeding.

```bash
# Pause macOS's PTP daemons so libusb can claim the camera
sudo killall -STOP ptpcamerad cameracaptured mscamerad-xpc

# Plug in your fp NOW (after STOP, before running the bridge)
```

### 5. Run

```bash
./run_mac.sh
```

You should see:

```
INFO sigma-bridge | 相機已連線
INFO sigma-bridge | 瀏覽器測試: http://192.168.x.x:8765/
```

Open `http://localhost:8765/` and drag the focus slider. The lens should move in real-time.

### 6. When you're done

```bash
sudo killall -CONT ptpcamerad cameracaptured mscamerad-xpc
# Or just restart your Mac to fully reset macOS PTP behavior.
```

---

## The four gotchas

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

### Gotcha 4: macOS aggressively blocks raw USB to PTP cameras

When you plug in any PTP-class USB camera, macOS launches three daemons that claim the device's IOKit interface:

- `/usr/libexec/ptpcamerad`
- `/usr/libexec/cameracaptured`
- `mscamerad-xpc` (Image Capture XPC service)

While they hold the device, `libusb_detach_kernel_driver()` fails and the bridge can't acquire the camera. Three things to know:

1. **`launchctl disable` doesn't stick** on SIP-enabled macOS for these system services.
2. **`killall ptpcamerad` doesn't help** either — launchd respawns it instantly on the next USB hotplug.
3. **`SIGSTOP` works** — pausing the process leaves launchd thinking it's alive (so no respawn), but the process can't claim USB while paused.

```bash
sudo killall -STOP ptpcamerad cameracaptured mscamerad-xpc
```

**You must run this BEFORE plugging in the camera**, or unplug-replug after running it. The kernel IOKit binding happens during USB enumeration.

> ### 🚨 Environment pollution warning
>
> While the daemons are stopped, **macOS Photos / Image Capture / Finder / iCloud import all stop recognizing your camera**. iPhone backup over USB also breaks.
>
> Always run `sudo killall -CONT ptpcamerad cameracaptured mscamerad-xpc` when you finish, or restart your Mac. Don't leave your machine in this state.

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

---

## Lens compatibility (help wanted)

Sigma's official SDK supports Focus Position in principle, but the actual lens motor's response varies. Help us build the compatibility list.

| Lens | PTP Focus Position | Notes |
|---|---|---|
| Sigma 28mm F1.4 DG DN Art | ✅ Working | HLA motor |
| Sigma 17mm F4 DG DN Contemporary | ❓ Uncertain | May depend on AF/MF switch state |

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

## File layout

```
.
├── sigma_fp_focus.py       Low-level: sigma-ptpy patch + helpers
├── mac_bridge_server.py    HTTP / WebSocket / MJPEG server
├── static/
│   └── index.html          Browser test UI
├── debug_encode.py         Standalone IFD encoder sanity test
├── diagnose.py             Dump all camera state for troubleshooting
├── run_mac.sh              One-shot launcher (auto-creates venv)
├── requirements.txt
└── README.md
```

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

Server pushes a `state` message at ~10 Hz with current focus position / state / mode.

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
