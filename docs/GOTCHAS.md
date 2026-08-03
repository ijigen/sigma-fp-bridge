# Traps

Look up what you are seeing.

| What you see | Trap |
|---|---|
| Cannot claim the USB interface | [ptpcamerad](#1-macos-takes-the-camera-first) |
| `No backend available` under `sudo` | [DYLD_* is stripped](#2-sudo-strips-dyld_) |
| Wrong colour mode names, aperture errors | [sigma-ptpy is a different camera's library](#3-sigma-ptpy-is-for-a-different-camera) |
| Live view stutters when you touch anything | [one transaction at a time](#4-one-transaction-at-a-time) |
| Write returns OK, nothing changes | [silent refusals](#5-writes-that-are-accepted-and-ignored) |
| Focus position 0 treated as "no value" | [zero is legal](#6-zero-is-a-legal-value) |
| Shutter fires once, then never again | [database entry not released](#7-an-unreleased-entry-stops-the-shutter) |
| Camera vanishes from USB, needs power cycle | [movie download misuse](#8-movie-download-has-one-slot) |
| The body's buttons are dead | [the API owns the camera](#9-opening-the-api-resets-the-camera) |
| Stuck in manual focus forever | [position writes force MF](#10-a-position-write-forces-mf) |
| AF-S behaves like AF-C | [Pre-AF is not AF-C](#10-a-position-write-forces-mf) |

---

## 1. macOS takes the camera first

```
camera plugged in
       │
       ├──► ptpcamerad   ← grabs it immediately
       │
       └──► your process ← "could not claim interface"
```

Run as root and you win the race. That is the only reason this project uses
`sudo`; USB permissions are not the issue.

Sometimes `ptpcamerad` is not holding it and an un-elevated run works. Do not
rely on that — it becomes "sometimes it connects" with no visible cause.

**There is a way not to fight it at all.** `ImageCaptureCore` talks *through*
`ptpcamerad` instead of racing it, and `ICCameraDevice.requestSendPTPCommand`
carries arbitrary opcodes — including Sigma's vendor range. Measured, as an
ordinary user with no `sudo`:

```
GetDeviceInfo          0x1001   277 bytes   response 0x2001 OK
SigmaConfigApi         0x9035    79 bytes   response 0x2001 OK
SigmaGetCamDataGroup1  0x9012    21 bytes   response 0x2001 OK
```

The last payload decodes through this project's own parser to
`CurrentLensFocalLength 28.0`, `ISOSpeed 32`, `Aperture 16`, `MediaFreeSpace
19564` — the real settings, matching the lens actually mounted. The spike is in
`spike/icprobe.swift`.

What this changes is the **transport**, not the language. The protocol layer
handles bytes and does not care where they came from, so `camera_settings.py`,
`sigma_fp_focus.py`, the sigma-ptpy schemas, the tests and the UI would all be
untouched. pyobjc can call ImageCaptureCore, so this does not require rewriting
in Swift.

**Throughput is a wash.** 60 consecutive `SigmaGetViewFrame` calls against the
same camera and scene, the bridge's own throttle removed for the comparison:

| | Per frame | Rate | Frame size | Throughput |
|---|---|---|---|---|
| libusb (today) | 30.1 ms | 32.8/s | 624 KB | 20.0 MB/s |
| ImageCaptureCore | 29.7 ms | 33.1/s | 623 KB | 20.2 MB/s |

Both stop at about 33 frames per second, which is the camera's own ceiling —
the transport is not the limit, and neither is the language.

So the decision rests entirely elsewhere. It buys: no `sudo`, no libusb, the
`DYLD_*` problem disappears, and the binary can be signed and notarised as an
ordinary app, which removes the `chmod` and `xattr` lines too. Installation goes
from four lines to a double click.

It costs: macOS only (`run_linux.sh` would have to go), bridging a
completion-block API onto a worker that is currently synchronous, and a
dependency on pyobjc.

Still unknown: whether ImageCaptureCore's PTP passthrough supports the *out-data*
direction (`SigmaSetCamDataGroup*`, which is how every setting is written) — only
in-data commands have been tested. That is the next thing to check, and it is
decisive: a read-only transport is useless here.

---

## 2. `sudo` strips `DYLD_*`

```
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
sudo ./run.sh
        │
        └──► macOS removes DYLD_* ──► libusb not found
```

Exactly when you need it, since root is what claims the camera.

**Fix:** [`libusb-package`](https://pypi.org/project/libusb-package/) ships a
libusb and loads it by absolute path. Homebrew stops being a prerequisite too.

Same applies to any variable you meant to pass: use `sudo -E`.

---

## 3. sigma-ptpy is for a different camera

It models the SDK well, but was written for the SD Quattro generation.

| What | Wrong how | Fix |
|---|---|---|
| `ColorMode` | 12 values starting with `Sepia`. The fp has 16 and no Sepia. | Own table — `FpColorMode` |
| Aperture APEX | code `101` missing (between f/51 and f/64) | patch the table |
| `PictFileInfo2` | skips a fixed 12 bytes — only right for one file. DNG+JPEG breaks it. | walk the offset table |
| `CanSetInfo5` | raises `IndexError` on this body | parse the bytes yourself |

**`_transaction` is a property that increments on read.** Read it twice and you
have burned two IDs; `recv()` then sees a mismatched reply and resets the
device. The symptom appears far from the cause.

---

## 4. One transaction at a time

```
        ┌─────────┐  ┌──────────┐  ┌───────────┐
wire →  │ frame   │  │ settings │  │ focus     │  ...
        └─────────┘  └──────────┘  └───────────┘
              no pipelining, no overlap
```

Consequences:

- **Every poll costs frame rate.** A 5-second poll nobody reads is a 5-second
  tax on live view.
- **One stuck call stalls everything.** You need a timeout and a way to report
  *which* operation is stuck.
- **Prioritise deliberately.** Focus commands beat live-view frames, and stale
  frame requests should be dropped rather than queued.

---

## 5. Writes that are accepted and ignored

PTP returns OK. Nothing changes. No error anywhere.

| Write | Ignored when |
|---|---|
| `ShutterSpeed` | body is in CINE — use shutter angle |
| `Aperture`, `ShutterSpeed` | exposure mode owns that axis |
| `ISOSpeed` | ISO Auto is on |
| `ExpComp` | mode is M — camera declares min = max = 0 |
| `DMFPos` | focus area is not `OnePointSelection` |
| `FaceEyeAF`, `FocusArea` | focus mode is MF |
| `FocusMode = AF_C` | body is in CINE |

**Read back and compare after every write.** And canonicalise first — a value
written as `12` reads back as `'FovClassicYellow'`, and comparing those two
reports every successful write as a failure.

---

## 6. Zero is a legal value

```python
if value:          # wrong
if value is None:  # right
```

Focus position 0, database index 0, capture status 0 — all real. Tethered
capture here failed after the first shot because index 0 read as "no index".

---

## 7. An unreleased entry stops the shutter

```
shoot ──► entry in database ──► shoot again ──► nothing happens, no error
                   │
                   └── ClearImageDBSingle (0x901C) ──► working again
```

The entry number is the database **tail read before the shot**.

---

## 8. Movie download has one slot

`GetPartialMovieFile` serves database index **0**, whatever parameter you pass.

```
index 0 has a movie   ──►  works
index 0 is empty      ──►  USBTimeoutError
                            camera drops off USB
                            power cycle required
```

Restarting your program does not help: the state is the camera's, not the
session's. Check that a movie exists at index 0 before every call.

---

## 9. Opening the API resets the camera

`ConfigApi` (`0x9035`) discards what the operator set on the body, and every
physical control stays dead until `CloseApplication` (`0x902F`).

To hand the camera back and take it again without destroying their setup:

```
read settings ──► CloseApplication ──► ...body is theirs...
                                   ──► ConfigApi ──► write settings back
```

Changes they make while you are away are still lost. Say so in the UI.

---

## 10. A position write forces MF

To hold a manual focus position, the write must include:

```
FocusMode  = MF
AFLock     = Off
PreConstAF = Off
```

or the camera drives the focus straight back. **Nothing sets those back.** Offer
a focus slider with no focus-mode control and the camera is stuck in manual
after the first drag.

**`PreConstAF` is Pre-AF, not AF-C.** Turn it on for AF-S and the camera hunts
continuously — single AF that behaves like continuous AF. Tie it to AF-C only.
