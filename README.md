# sigma-fp-bridge

Control a **Sigma fp** from a computer over USB — focus, exposure, movie
settings, recording, tethered stills — and drive it from a browser, a script, or
anything that speaks HTTP.

[![The bridge driving a Sigma fp over USB](docs/demo.png)](docs/demo.mov)

*[▶ Watch the walkthrough](docs/demo.mov) (1:49) — focus, tap-to-focus, settings,
recording.*

It started as focus control alone, which is where the lens-motor part comes
from: it drives the lens's internal motor directly, with no follow-focus rig.
The git history still says `sigma-fp-focus-bridge`, deliberately — the record of
what was tried, got wrong, and corrected is a large part of what this repo is
worth.

> **🤖 Built by pair-programming with [openclaw](https://github.com/openclaw)**,
> an AI agent harness. Most of the protocol reverse-engineering and library
> patching came from a Claude-driven agent stepping through PTP details faster
> than I could solo.

---

## Run it

The simplest path needs nothing installed:

```bash
sudo ./dist/sigma-fp-bridge
```

Then open **<http://localhost:1025/>**.

### Connecting the camera

1. **USB-C cable**, camera to computer. The fp's port is USB 3.1 Gen 1; any
   data-capable USB-C cable works. A charge-only cable will not.
2. The camera shows a **USB mode** menu. Choose **Camera Control**.
   - *Mass Storage* mounts the card. *Video Class (UVC)* makes it a webcam.
     Neither speaks the protocol this uses.
   - If no menu appears, someone has pinned a mode: **SYSTEM → USB Mode**, set
     it to *Camera Control* or back to *Select when connecting*.
3. Run the bridge. It waits for the camera and reconnects on its own, so the
   order does not matter.
4. Open <http://localhost:1025/>.

`sudo` is not about USB permissions. macOS's own `ptpcamerad` grabs the camera
the moment it appears, and running as root is what wins that race —
[details](docs/GOTCHAS.md#macos-fights-you-for-the-camera).

While the bridge is connected, **every control on the camera body is dead**.
That is the protocol, not a fault. Press **Release to Body** to hand it back.

Different port: `SIGMA_BRIDGE_PORT=9000 sudo -E ./dist/sigma-fp-bridge`.
(1025 is 10/25 — the day the fp shipped.)

---

## What it does

**Focus**

- Drive the lens motor to an absolute position, with the range read from the
  mounted lens
- A slider that sends continuously while you drag it, which works over a
  one-transaction-at-a-time bus because position writes coalesce
- Focus mode — MF / AF-S / AF-C — including the way back from manual
- Tap the preview to place the AF point; the camera's real AF area is drawn on
  the frame, because it covers only about 75% × 81% of it
- AF frame size, face detection, eye detection

**Exposure and image**

- Aperture, shutter (speed or angle), ISO, ISO Auto, exposure compensation
- Metering, white balance including colour temperature, colour mode, colour
  space, tone
- Lens optics compensation, DC crop, electronic stabilisation, HDR, fill light
- Options come from what the camera declares it can do *right now*, so nothing
  is offered that would be silently ignored

**Movie**

- Start and stop recording; footage goes to the card
- Format (CinemaDNG / MOV), resolution, bit depth, frame rate, audio
- Pull a recorded MOV back to the computer at around 56 MB/s

**Stills**

- Tethered capture, downloaded to the computer — DNG, JPEG, or both
- Optionally without writing to the card at all

**Live view**

- MJPEG at about 24 fps, which is what the camera will answer at

**Other**

- Bonjour advertisement, so other devices find it without being told an address
- Release the camera to the body and take it back, with settings preserved
- Every write is read back and compared, so "the camera ignored that" is
  reported rather than silently accepted

### Scope

- **CINE is the tested path.** STILL mode works — tethered capture, DNG+JPEG,
  the stills-only settings — but it has had far less use, and settings that only
  exist in STILL (drive mode, HDR, interval timer) are wired up and lightly
  exercised rather than trusted.
- **macOS is the tested platform.** Linux should work and there is a launcher
  for it; [running in a VM](docs/UTM_SETUP.md) is documented.
- Tested on one body with the 45mm F2.8 DG DN. Focus ranges and lens behaviour
  vary; if yours differs, please open an issue.
- **UHD 12-bit CinemaDNG is not reachable** over USB and cannot be made so —
  [why](docs/PROTOCOL.md#what-is-not-reachable).

---

## Documentation

| | |
|---|---|
| **[docs/API.md](docs/API.md)** | Every HTTP endpoint and WebSocket message, with examples |
| **[docs/PROTOCOL.md](docs/PROTOCOL.md)** | The camera's PTP protocol: opcodes, encoding, tags, measured behaviour |
| **[docs/GOTCHAS.md](docs/GOTCHAS.md)** | The traps, ordered by how likely you are to hit them |
| [docs/UTM_SETUP.md](docs/UTM_SETUP.md) | Running the bridge in a Linux VM |
| [docs/iphone_architecture.md](docs/iphone_architecture.md) | Notes on a phone client |

If you are writing your own implementation rather than using this one, read
GOTCHAS first and PROTOCOL second. The gotchas are what cost time here; the
protocol is just work.

---

## Driving it without the browser

```bash
curl http://localhost:1025/api/status
curl -X POST http://localhost:1025/api/focus -H 'Content-Type: application/json' \
     -d '{"position": 8500}'
curl -X POST http://localhost:1025/api/focus/mode -H 'Content-Type: application/json' \
     -d '{"mode": "AF_S", "point": [300, 500]}'
curl -X POST http://localhost:1025/api/settings -H 'Content-Type: application/json' \
     -d '{"iso": 400, "shutter_angle": 180}'
curl -X POST http://localhost:1025/api/record/start
```

Full reference in [docs/API.md](docs/API.md).

---

## Running from source

There are two ways to run this and they are separate things:

| | Runs | Needs |
|---|---|---|
| `sudo ./dist/sigma-fp-bridge` | a single built binary | nothing else |
| `sudo ./run_mac.sh` | the source tree | git, python3, a venv, network |

`run_mac.sh` never runs the binary — it always executes `mac_bridge_server.py`
from the tree, so it is what you want while changing code. Both need `sudo`, for
the same unrelated reason.

```bash
git clone https://github.com/<you>/sigma-fp-bridge.git
cd sigma-fp-bridge
sudo ./run_mac.sh          # creates the venv on first run
```

Homebrew is not required. `requirements.txt` pulls in
[`libusb-package`](https://pypi.org/project/libusb-package/), which ships a
libusb binary loaded by absolute path — necessary because
[`sudo` strips `DYLD_*`](docs/GOTCHAS.md#sudo-strips-dyld_) and a Homebrew
libusb then cannot be found, precisely when you need it.

Before starting the bridge, this reads the camera without touching the motor:

```bash
.venv/bin/python diagnose.py
```

### A single-file build

```bash
.venv/bin/python -m pip install pyinstaller
.venv/bin/python -m PyInstaller sigma-fp-bridge.spec
sudo ./dist/sigma-fp-bridge
```

`static/` is declared in the spec because the page is read from disk and so is
invisible to the import graph. libusb needs no special handling —
`libusb-package` ships a PyInstaller hook.

### Tests

```bash
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

No camera needed: a fake one stands in. Every test is written to fail if the
behaviour it describes is removed — several were rewritten after a mutation
survived them.

---

## How it is put together

```
browser ──HTTP/WS──┐
scripts ──HTTP─────┤
                   ▼
            mac_bridge_server.py     aiohttp; one asyncio loop
                   │
            CameraWorker             a single thread owning the camera,
                   │                 with priorities and timeouts
                   ▼
            sigma_fp_focus.py        vendor PTP: focus, settings, capture
            camera_settings.py       the settings table and APEX conversion
            movie_settings.py        DataGroupMovie
            recording.py             record, list, download
            capture.py               tethered stills
            ifd.py                   the wire encoding
                   │
                   ▼
              sigma-ptpy → ptpy → pyusb → libusb → Sigma fp
```

USB PTP runs one transaction at a time, so camera access is serialised through a
single worker. Requests carry a priority — a focus command beats a live-view
frame — and stale live-view requests are dropped rather than queued behind
something slow. Every operation has a timeout, and when one wedges, the status
endpoint still answers and names the step it stalled on.

---

## Contributing

The most useful thing is coverage of hardware I do not have: other lenses, the
fp L, other bodies in the range. Focus ranges, `CanSetInfo5` dumps
(`GET /api/dump/info5`) and anything that behaves differently are all welcome
in an issue.

Corrections to the protocol notes are especially welcome. Everything in
[PROTOCOL.md](docs/PROTOCOL.md) was measured on one body, and "measured once" is
not the same as "true".

---

## Licence

MIT — see [LICENSE](LICENSE).

Not affiliated with SIGMA. "Sigma" and "fp" are their trademarks.
