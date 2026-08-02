# sigma-fp-bridge

Control a **Sigma fp** over USB — focus, exposure, movie settings, recording,
tethered stills — from a browser, a script, or anything that speaks HTTP.

[![The bridge driving a Sigma fp over USB](docs/demo.png)](docs/demo.mov)

*[▶ Walkthrough](docs/demo.mov) (1:49)*

> Built with [Claude Code](https://claude.com/claude-code). The protocol
> reverse-engineering, the library patches and most of the code came out of
> pair-programming with it.

---

## Run it

```bash
sudo ./dist/sigma-fp-bridge
```

Then open **<http://localhost:1025/>**.

### Connecting the camera

1. **USB-C cable**, camera to computer. A charge-only cable will not do.
2. The camera shows a **USB mode** menu — choose **Camera Control**.
   *Mass Storage* mounts the card, *Video Class (UVC)* makes it a webcam;
   neither speaks this protocol. No menu means a mode is pinned:
   **SYSTEM → USB Mode**.
3. Run the bridge. It waits for the camera and reconnects on its own, so the
   order does not matter.

`sudo` is not about USB permissions — macOS's `ptpcamerad` grabs the camera the
moment it appears, and root wins that race
([details](docs/GOTCHAS.md#macos-fights-you-for-the-camera)).

While connected, **every control on the camera body is dead**. That is the
protocol, not a fault. **Release to Body** hands it back.

Other port: `SIGMA_BRIDGE_PORT=9000 sudo -E ./dist/sigma-fp-bridge`.
(1025 is 10/25, the day the fp shipped.)

---

## What it does

**Focus** — drive the lens motor to an absolute position, with the range read
from the mounted lens; a slider that sends continuously while dragged; MF /
AF-S / AF-C; tap the preview to place the AF point, with the camera's real AF
area drawn on the frame; AF frame size, face and eye detection.

**Exposure and image** — aperture, shutter (speed or angle), ISO, ISO Auto,
exposure compensation, metering, white balance and colour temperature, colour
mode, colour space, tone, lens optics compensation, DC crop, electronic
stabilisation, HDR, fill light.

**Movie** — start and stop recording; format, resolution, bit depth, frame rate,
audio; pull a recorded MOV back at around 56 MB/s.

**Stills** — tethered capture downloaded to the computer, DNG or JPEG or both,
optionally without writing to the card.

**Live view** — MJPEG at about 24 fps, which is what the camera answers at.

Options come from what the camera declares it can do *right now*, and every
write is read back and compared — so a setting the camera silently ignored is
reported rather than accepted.

### Scope

- **CINE is the tested path.** STILL works — tethered capture, DNG+JPEG,
  stills-only settings — but has had far less use.
- **macOS is the tested platform.** Linux should work; [in a
  VM](docs/UTM_SETUP.md) is documented.
- One body, one lens (45mm F2.8 DG DN). If yours behaves differently, please
  open an issue.
- **UHD 12-bit CinemaDNG is not reachable** over USB
  ([why](docs/PROTOCOL.md#what-is-not-reachable)).

---

## Documentation

| | |
|---|---|
| **[docs/API.md](docs/API.md)** | Every endpoint and WebSocket message |
| **[docs/PROTOCOL.md](docs/PROTOCOL.md)** | The camera's PTP protocol, measured |
| **[docs/GOTCHAS.md](docs/GOTCHAS.md)** | The traps, likeliest first |
| [docs/UTM_SETUP.md](docs/UTM_SETUP.md) | Running in a Linux VM |
| [docs/iphone_architecture.md](docs/iphone_architecture.md) | Notes on a phone client |

Writing your own implementation: GOTCHAS first, PROTOCOL second. The protocol is
just work; the traps are what cost time.

```bash
curl http://localhost:1025/api/status
curl -X POST http://localhost:1025/api/focus -H 'Content-Type: application/json' \
     -d '{"position": 8500}'
curl -X POST http://localhost:1025/api/settings -H 'Content-Type: application/json' \
     -d '{"iso": 400, "shutter_angle": 180}'
curl -X POST http://localhost:1025/api/record/start
```

---

## From source

`run_mac.sh` runs the tree, never the binary — that is the one to use while
changing code. Both need `sudo`, for the same unrelated reason.

```bash
git clone https://github.com/<you>/sigma-fp-bridge.git
cd sigma-fp-bridge
sudo ./run_mac.sh                # creates the venv on first run
.venv/bin/python diagnose.py     # reads the camera, never drives the motor
```

Homebrew is not required: [`libusb-package`](https://pypi.org/project/libusb-package/)
ships a libusb loaded by absolute path, which matters because
[`sudo` strips `DYLD_*`](docs/GOTCHAS.md#sudo-strips-dyld_).

Build a single file:

```bash
.venv/bin/python -m pip install pyinstaller
.venv/bin/python -m PyInstaller sigma-fp-bridge.spec
```

Tests need no camera — a fake one stands in:

```bash
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

---

## How it is put together

```
browser ──HTTP/WS──┐
scripts ──HTTP─────┤
                   ▼
            mac_bridge_server.py     aiohttp; one asyncio loop
                   │
            CameraWorker             one thread owning the camera,
                   │                 with priorities and timeouts
                   ▼
            sigma_fp_focus.py        vendor PTP: focus, settings, capture
            camera_settings.py       settings table, APEX conversion
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
frame — and stale live-view requests are dropped rather than queued. When an
operation wedges, `/api/status` still answers and names the step it stalled on.

---

## Contributing

Most useful: hardware I do not have. Focus ranges, `GET /api/dump/info5` output,
anything that behaves differently.

Corrections to [PROTOCOL.md](docs/PROTOCOL.md) especially — it was all measured
on one body, and measured once is not the same as true.

---

MIT — see [LICENSE](LICENSE). Not affiliated with SIGMA.
