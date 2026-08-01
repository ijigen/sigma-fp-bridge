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
- **Browser UI** with live preview, focus control, exposure and movie settings,
  recording, and distance calibration

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
| Exposure / white balance / format control | ✅ Working |
| Movie settings (shutter angle, frame rate, CinemaDNG) | ✅ Working |
| Recording start / stop | ✅ Working |
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

Shut the bridge down with Ctrl+C and control returns cleanly. To get the body
back **without** stopping the bridge, use `release` (WebSocket) or
`POST /api/release`, then `acquire` when you want control again. While released
there is no live view, no status polling and no focus control — the bridge is
genuinely off the camera.

`release` sends `sgm_CloseApplication` but **keeps the USB interface claimed**.
Letting go of it means macOS rebinds the camera to `ptpcamerad` within moments,
and this process cannot get it back — eight consecutive re-acquires failed with
the camera plainly visible on the bus. Holding the claim also makes re-acquiring
a single `sgm_ConfigApi` instead of a fresh USB fight.

Re-acquiring runs `sgm_ConfigApi` again, which resets the camera to defaults, so
`release` snapshots the settings and `acquire` restores them. Without that,
stepping away to press a button on the body would silently cost you every setting
you had dialled in.

**A consequence worth knowing:** changes you make on the body while released do
not survive re-acquiring — the reset discards them, and the restore then puts
back what was there before. Pass `restore: false` (WebSocket) or
`POST /api/acquire?restore=0` to keep the reset values instead.

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
python3 tests/test_ifd.py       # IFD parser, pure data
python3 tests/test_bridge.py    # camera worker + HTTP/WS/MJPEG, fake camera
python3 tests/test_session.py   # camera session lifecycle
python3 tests/test_settings.py  # settings encode/decode, batch apply
python3 tests/test_movie.py     # movie data group, recording probe
python3 tests/test_ui.py        # browser UI: syntax, wiring, render discipline
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

// Camera settings
ws.send(JSON.stringify({cmd: "describe_settings"}));   // choices for every setting
ws.send(JSON.stringify({cmd: "get_settings"}));
ws.send(JSON.stringify({cmd: "set_settings", settings: {aperture: 2.8, iso: 800}}));

// Hand the body back / take it again (see Gotcha 4)
ws.send(JSON.stringify({cmd: "release"}));
ws.send(JSON.stringify({cmd: "acquire"}));
```

`describe_settings`, `release` and `acquire` work even with no camera attached;
everything else requires a connection.

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

# Camera settings
curl http://localhost:8765/api/settings/schema   # what's settable, with choices
curl http://localhost:8765/api/settings
curl -X POST http://localhost:8765/api/settings -H 'Content-Type: application/json' \
  -d '{"aperture": 2.8, "iso": 800, "exposure_mode": "Manual"}'

# Hand the body back without stopping the bridge (see Gotcha 4)
curl -X POST http://localhost:8765/api/release
curl -X POST http://localhost:8765/api/acquire
```

### Camera settings

Exposure, white balance and image format are exposed in human units — the
protocol's 8-bit APEX codes are converted for you (`aperture: 2.8`, not `32`).
`GET /api/settings/schema` lists every setting with its allowed values, so a UI
can build itself.

**The allowed values come from the camera, not from a static table.** The APEX
conversion tables span far more than any real body accepts — ISO 6 to 102400 —
while an fp reports ISO 100–25600. `CanSetInfo5` carries the real limits as
fixed-point values scaled by 256:

| tag | meaning | example (fp, 40mm lens) |
|---|---|---|
| 215 | ISO range (APEX Sv, `Sv 5 = ISO 100`) | `(5.0, 13.0, 1.0, 0.33)` → ISO 100–25600, ⅓ stop |
| 216 | Auto ISO range | `(5.0, 11.0, …)` → ISO 100–6400 |
| 217 | Exposure compensation range (EV) | `(-5.0, 5.0, 0.33)` → ±5 EV, ⅓ stop |
| 658 | Focus position range | `(5974, 11116)` |

Values outside the reported range are rejected with an explanatory error rather
than sent and silently ignored by the camera. The ranges are re-read whenever the
lens or focal length changes.

Changes are validated as a batch before anything is sent: one bad value rejects
the whole request rather than leaving the camera half-configured. Settings that
share a `DataGroup` are written in a single transaction.

**Every write is read back and verified.** A PTP write returns OK even when the
camera then ignores the value, which is the worst kind of failure to debug — the
UI shows what you asked for and the camera does something else. Responses carry a
`rejected` map naming anything that did not stick:

```json
{"rejected": {"shutter_speed": {"requested": 0.002, "actual": 0.04,
  "hint": "exposure mode is AperturePriority; auto exposure overrides a manual shutter"}}}
```

That hint matters. Manual exposure values behave exactly like focus position: in
P or A mode the camera's auto-exposure overwrites your shutter the instant after
you set it, the same way auto-focus reclaims a focus position unless `AFLock` and
`PreConstAF` are switched off in the same write. Set `exposure_mode` to `Manual`
first.

| Settable | |
|---|---|
| Exposure | `exposure_mode` (P/A/S/M), `aperture`, `shutter_speed`, `iso`, `iso_auto`, `exposure_compensation`, `metering_mode` |
| White balance | `white_balance`, `color_temp` (Kelvin; needs `white_balance: ColorTemp`) |
| Format | `image_quality` (DNG/JPEG), `dng_quality` (12/14-bit), `resolution`, `aspect_ratio` |
| Look | `color_mode`, `color_space`, `tone_effect` |
| Drive | `drive_mode` |
| Cinema | `shutter_angle` (needs a frame rate — see below) |

#### Shutter angle

Cinema work thinks in shutter angle, not seconds: 180° means the exposure covers
half of each frame, which keeps motion blur consistent across frame rates.

    angle = 360 × exposure_seconds × frame_rate

There is no writable shutter-angle field in the protocol — `CanSetInfo5` tag 214
`ShutterAngle` is a capability report only — so the bridge converts the angle to
a shutter time and writes the ordinary `shutter_speed` field. Identical result
for the camera, familiar units for you.

Set the frame rate first (`set_frame_rate` over WebSocket, or the fps box in the
UI). An angle without a frame rate is meaningless and is rejected rather than
guessed at.

**Shutter speeds are discrete, so not every angle is reachable.** The response
always reports the angle you actually got:

| Requested | Frame rate | Actual |
|---|---|---|
| 180° | 24 fps | **172.8°** (1/50s — 1/48 isn't on the ⅓-stop scale) |
| 180° | 25 fps | 180.0° (1/50s) |
| 180° | 30 fps | 180.0° (1/60s) |

`shutter_angle` and `shutter_speed` are two views of one setting, so sending both
in the same request is rejected.

**Not settable over PTP:**

- ~~Still ↔ movie mode~~ — **this turned out to be writable.** See
  `capture_mode` below. The SDK says the setting "is synchronized with the switch
  status", which I read as "only the switch can change it". It is not: writing
  `DataGroupMovie` tag 1 switches the body, confirmed by watching the screen.
#### Stills and movie are different cameras

The body switch does not merely change what gets recorded — **it changes which
settings exist and what values they accept.** Ignoring that is how you end up
writing a value the camera accepts and discards.

| | STILL | CINE |
|---|---|---|
| `RecordFormat`, `CinemaDNGImageQuality`, `MovieResolution`, `ShutterAngle` | no legal values | populated |
| `ImageQuality`, `DNGQuality`, `StillImageResolution`, `DriveMode` | populated | populated but inert |
| Exposure compensation | ±5 EV | ±3 EV |
| Shutter | full APEX range | capped at 1/25 by frame rate |

`camera_mode` in the status and schema responses reports which side you are on,
and `/api/settings/schema` lists only the settings that apply. Writing one that
does not is refused with the alternative named — `shutter_speed` in CINE points
you at `shutter_angle` — rather than being sent and silently dropped.

The switch position has no directly readable field. `CanSetInfo5` tag 100
`StillMovieSwitch` returns `[1, 1]`, which appears to mean "both supported"
rather than "currently here". The mode is inferred from whether the movie
capability lists carry any values, which tracks the switch exactly in testing.

#### Movie mode (CINE)

With the body switch on CINE, **exposure is governed by `DataGroupMovie`, not
`DataGroup1`.** Writing `shutter_speed` there is accepted and discarded — the
value simply never changes. Use `shutter_angle`, which writes to the movie group.

| Setting | |
|---|---|
| `shutter_angle` | 11.2 – 360°, from the camera's own list |
| `frame_rate` | 23.98 / 25 / 29.97 |
| `cinema_dng_quality` | 12 / 10 / 8-bit |
| `record_format`, `movie_resolution` | camera-reported values, labelled |
| `mov_image_quality` | reported but **never settable in testing** — see below |

**`record_format`: 1 = CinemaDNG, 2 = MOV.** Established by recording a clip at
each setting on a freshly cleared card and looking at what came out — the fp does
not show the format on its main display, and the protocol reports a bare number.
`movie_resolution` 1 = FHD, 2 = UHD — 2 from that clip, 1 confirmed by
inspecting the recorded file. Values that have not been confirmed this way are
left unlabelled rather than guessed at.

`mov_image_quality` reads back as 1 or 2 and neither value has a meaning
established. The method that identified `record_format` — set each value, record
a clip, look at what came out — could not be run: the camera reported no settable
values for it in MOV and CinemaDNG, at UHD and FHD, and at 23.98, 29.97 and
59.94 fps. Whatever gates it was not found. It is hidden from the UI — a control that cannot be
changed and whose values mean nothing to the reader is just clutter — but the
protocol mapping and API access stay, so anyone who works out what gates it can
pick up where this left off.

**Two frame rates do not take.** Writing 29.97 stores 30 and 59.94 stores 60,
while 23.98, 25 and 50 are stored as asked — even though the camera lists 29.97
and 59.94 as legal and does not list 30 or 60 at all. Tested across both formats
and both resolutions, with the same result each time. **The cause is unknown.**
23.98 is an NTSC fractional rate and works, so a recording-standard setting does
not explain it. The value is read back and the mismatch reported rather than
passed off as success.

**Still/movie mode is `DataGroupMovie` tag 1** — 1 for STILL, 2 for CINE. Writing
it switches the body: the screen changes, and the whole capability set swaps over
(drive mode, continuous shooting, interval timer, aspect ratio, fill light and HDR
appear; audio, record format, CinemaDNG depth, resolution, frame rate, aperture,
T-stop, shutter and shutter angle disappear). Switching also drags the shutter
unit along with it.

This corrects the earlier claim that the mode could only be changed on the body.
It also means the bridge reads the mode rather than inferring it from which
capabilities are populated, which is what it did before.

**The shutter unit is `DataGroupMovie` tag 6** — 1 for speed, 2 for angle. Found
by writing to it: at 2 the camera declares 18 legal shutter angles, at 1 it
declares none while still reporting a shutter speed range. This is why writing
`shutter_speed` in CINE appeared to do nothing — the camera was in angle mode,
and the field it accepts depends on this tag. Confirmed by switching to 1 and writing 1/500, 1/50 and 1/125 — all three
read back exactly. The UI's seconds/angle buttons set the tag, so switching
there switches the camera, and the schema lists only the representation
currently in effect.

Both read-only routes to identifying it were closed: a change made on the body is
wiped by `sgm_ConfigApi` on re-acquire, and outside API mode the camera answers
vendor commands with an empty payload. Writing was the only way left.

**Aspect ratio, colour space and tone effect do nothing in CINE.** Written and
discarded, like `shutter_speed`. They are marked stills-only and omitted from the
movie schema.

**Judge a recording by `CaptStatus` and `ImageDBTail`, never by the movie file
info.** `SigmaGetMovieFileInfo` describes MOV only, so it is blind to CinemaDNG,
and it also goes stale within a PTP session: after five consecutive clips it was
still reporting the first one's name and size, and only refreshed after the
bridge reconnected. `POST /api/record/clip` reports `produced_something` from the
image database tail, which advances for every format.

I misread that staleness as recording having broken, and spent several rounds
chasing a camera fault that did not exist. The footage was on the card the whole
time.

Worth knowing: **changing `record_format` moves `cinema_dng_quality` on its own**
— switching to CinemaDNG dropped it from 12-bit to 8. Writing one tag is not
always a one-tag change, which is why writes are read back.

An inference that turned out to be wrong, kept here as a caution: with
`record_format` at 1, `CinemaDNGImageQuality` reports **no settable values** — yet
that setting records CinemaDNG perfectly well. An empty capability list means
"cannot be changed right now", not "does not apply". Reading it as the latter is
what led to guessing that 2 was CinemaDNG.

sigma-ptpy defines `SigmaGetCamDataGroupMovie` (0x9033) and
`SigmaSetCamDataGroupMovie` (0x9034) but ships no schema class or method for
either — the same gap that hid `FocusPosition`. `movie_settings.py` builds the
IFD directly.

**How the tag numbers were established**, since guessing them would be
irresponsible: `DataGroupMovie` tags are `CanSetInfo5` tags minus 100, confirmed
across the whole audio and format block — `FrameRate` at movie tag 61 reads
23.98 against `CanSetInfo5` 161, `CinemaDNGImageQuality` at 51 reads 12-bit
against 151, and so on. Shutter angle does not follow that offset, but the proof
is more direct: `CanSetInfo5` tag 214 lists the legal angles, its first entry is
`(112, 3600)`, and movie tag 7 read back exactly `(112, 3600)`. Converted, the
list is the standard cine sequence — 11.2, 22.5, 45, 60, 72, 75, 86.4, 90, 108,
120, 144, 150, 172.8, 180, 216, 270, 300, 360 — which nothing else would be.

Writes are restricted to values the camera itself declared legal. That is the
only defensible safety net when writing to an undocumented data group.

**Set `exposure_mode` to `Manual` first.** Shutter angle behaves like every other
manual exposure value: in ProgramAuto the camera picks the angle and overwrites
anything you write. Verified on a live body — the same write that did nothing in
ProgramAuto succeeded immediately in Manual. Rejections say so.

Changing `frame_rate` resets the shutter angle, since the angle is defined
relative to the frame.

Movie settings constrain each other, and the camera reports it. At UHD it
offers **no** settable CinemaDNG bit depth — the value is locked at 8-bit — and
only 23.98 and 25 fps; at FHD the depth opens up to 12/10/8 and the frame rates
extend to 29.97, 50 and 59.94. An empty capability list means "not changeable in
this combination", so the UI shows the locked value rather than hiding the
control.

Movie mode also changes the limits on ordinary settings — exposure compensation
narrows from ±5 EV to ±3, and shutter is capped at 1/25 by the frame rate. This
is why ranges are read from the camera rather than tabulated.

- **Movie record format (CinemaDNG, movie resolution) — now supported, see above.**
  Previously unreachable:
  sigma-ptpy defines the opcodes `SigmaGetCamDataGroupMovie` (0x9033) and
  `SigmaSetCamDataGroupMovie` (0x9034) but ships no schema class or method for
  them — the same gap that hid `FocusPosition`. `--dump-movie` issues the read
  opcode directly and prints the IFD, which is the first step to writing a setter.

  The tag numbers inside `DataGroupMovie` are still unknown and will not match
  `CanSetInfo5`'s: compare `DataGroupFocus`, which uses tags 1/2/81 for items
  `CanSetInfo5` numbers 600/601/612. They have to be read off the camera.

  ```bash
  # switch the body to CINE first — movie settings do not exist in stills mode
  sudo .venv/bin/python sigma_fp_focus.py --dump-movie
  ```

  Change one setting on the body (frame rate, record format), dump again, and
  whichever tag moved is that setting.

### Tethered capture

`POST /api/capture` takes a frame and pulls the image back over USB, saving it
under `~/.sigma_fp_bridge/photos/`. The download asks the camera for the buffer
it holds after a Camera Control mode shot — `PictFileInfo2` for the address and
size, then `GetBigPartialPictFile` in chunks.

`dest_to_save` (DataGroup3) is a separate setting: `InCamera` (card),
`InComputer` ("drive in PC side") or `Both`. **It does not gate the download** —
tested on all three values, and the buffer read back the same 8.9 MB frame every
time, `InCamera` included. Whether the card also receives a copy could not be
checked from the host: PTP object enumeration reports an empty card while the
camera is in API mode, whatever has been written.

```bash
curl -X POST 'http://localhost:8765/api/capture'            # shoot and download
curl -X POST 'http://localhost:8765/api/capture?fetch=0'    # shoot only
curl -X POST 'http://localhost:8765/api/capture?af=1'       # autofocus first
```

Autofocus is off by default: this project drives focus over PTP, and letting the
camera focus before the frame would discard the position you set.

**Movies cannot be pulled back this way.** `SigmaGetPartialMovieFile` (0x9037)
exists as an opcode but sigma-ptpy does not wrap it and its parameters are
undocumented, so it needs the same reverse-engineering `DataGroupMovie` did.
`SigmaGetMovieFileInfo` reports a file size but no address to read from, so there
is nothing to build on yet.

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
