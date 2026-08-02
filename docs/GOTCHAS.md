# Traps

Every one of these cost real time here. They are ordered by how likely you are
to hit them, not by how interesting they are.

- [macOS fights you for the camera](#macos-fights-you-for-the-camera)
- [sudo strips DYLD_*](#sudo-strips-dyld_)
- [sigma-ptpy needs patching](#sigma-ptpy-needs-patching)
- [USB PTP is one transaction at a time](#usb-ptp-is-one-transaction-at-a-time)
- [The camera accepts writes it then ignores](#the-camera-accepts-writes-it-then-ignores)
- [Zero is a legal value](#zero-is-a-legal-value)
- [An unreleased database entry stops the shutter](#an-unreleased-database-entry-stops-the-shutter)
- [Movie download has one slot](#movie-download-has-one-slot)
- [Opening the API resets the camera](#opening-the-api-resets-the-camera)
- [Writing a focus position turns off autofocus for good](#writing-a-focus-position-turns-off-autofocus-for-good)

---

## macOS fights you for the camera

`ptpcamerad` claims any PTP camera the instant it enumerates. You will see the
device, fail to claim the interface, and get an error that does not mention the
real cause.

Running as root is enough to win the race in practice. That is why everything
here is `sudo`. It is not about the USB permissions themselves.

Occasionally `ptpcamerad` is not holding the device and an un-elevated run
works. Do not build on that — it turns into "sometimes it connects" with no
visible reason.

---

## sudo strips DYLD_*

macOS removes `DYLD_*` from the environment of setuid/elevated processes. So a
launcher that exports `DYLD_FALLBACK_LIBRARY_PATH` to help pyusb find a Homebrew
libusb is doing nothing under `sudo` — which is exactly when you need it.

The fix used here is [`libusb-package`](https://pypi.org/project/libusb-package/),
which ships a libusb binary and loads it by absolute path, so nothing has to be
found on a search path. It also means Homebrew is not a prerequisite.

Same trap applies to any environment variable you meant to pass through: use
`sudo -E` or set it inside the program.

---

## sigma-ptpy needs patching

The library models the SDK closely and was written for the SD Quattro
generation. On the fp:

**`ColorMode` is the wrong table.** It lists 12 values starting with `Sepia`.
The fp has 16 colour modes and no Sepia at all (its sepia is a tone under
Monochrome). Value 1 on the fp is Warm Gold. This repo carries its own
`FpColorMode` — see [PROTOCOL.md](PROTOCOL.md) and `camera_settings.py`.

**One aperture code is missing.** The APEX table skips `101`; its neighbours are
99 (f/51) and 104 (f/64), so a request that lands there raises instead of
converting.

**`PictFileInfo2` parsing assumes one file.** It skips a fixed 12 bytes, which
only works when `FileCount == 1`. DNG+JPEG breaks it. Walk the offset table
instead.

**`CanSetInfo5` parsing raises `IndexError`** on this body's payload. This repo
parses the raw bytes itself so one bad entry does not cost the whole structure.

**`_transaction` is a property that auto-increments.** Read it twice and you
have consumed two transaction IDs. `recv()` validates the session, transaction
and operation code of the reply and calls `__dev.reset()` on a mismatch — so a
stray read of that property shows up much later as a device reset.

---

## USB PTP is one transaction at a time

There is no pipelining. A live-view frame, a settings read and a focus write all
queue behind each other on the same wire.

This has consequences for design, not just performance:

- Anything that polls costs you frame rate. A five-second poll for something
  nobody reads is a five-second tax on live view.
- A stuck transaction stalls everything. You need a timeout and a way to report
  *which* operation is stuck, or the whole thing looks hung with no explanation.
- Serialise deliberately, with priorities. Live view should lose to a focus
  command, and stale live-view requests should be dropped rather than queued.

---

## The camera accepts writes it then ignores

PTP returns OK. The setting does not change. There is no error anywhere.

Cases confirmed on this body:

| Write | Ignored when |
|---|---|
| `ShutterSpeed` | body is in CINE (use shutter angle) |
| `Aperture`, `ShutterSpeed` | exposure mode hands that axis to the camera |
| `ISOSpeed` | ISO Auto is on |
| `ExpComp` | exposure mode is M — the camera declares min = max = 0 |
| `DMFPos` | focus area is not `OnePointSelection` |
| `FaceEyeAF`, `FocusArea` | focus mode is MF |
| `FocusMode = AF_C` | body is in CINE |

**Read back after every write and compare.** It is the only way to tell. When
comparing, canonicalise first: a value written as a number reads back as a name,
and comparing `12` with `'FovClassicYellow'` reports every successful write as a
rejection.

---

## Zero is a legal value

`if value:` is wrong for camera fields. Focus position 0, image database index
0, capture status 0 — all real values that a truthiness test discards.

The tethered-capture path here failed after the first shot for exactly this
reason: index 0 was treated as "no index".

---

## An unreleased database entry stops the shutter

Take a picture, leave its entry in the image database, and the next
`SnapCommand` silently does nothing.

Release with `ClearImageDBSingle` (`0x901C`) after you have the data. Note the
entry number is the database **tail read before the shot**.

---

## Movie download has one slot

`GetPartialMovieFile` serves database index 0 and only index 0, whatever
parameter you pass.

And if you call it when there is nothing servable there, the camera does not
return an error — it stops answering USB and needs a power cycle. Restarting
your program will not clear it, because it is the camera's state and not the
session's. Check that a movie exists at index 0 before every call.

---

## Opening the API resets the camera

`ConfigApi` (`0x9035`) discards whatever the operator set on the body, and locks
every physical control until you send `CloseApplication` (`0x902F`).

If you want to hand the camera back and take it again without destroying their
setup, snapshot the settings before closing and restore them after re-opening.
Changes they make on the body while you are away are still lost — there is no
way around that, so say so in the UI rather than surprising them.

---

## Writing a focus position turns off autofocus for good

To hold a manual position, the write has to include `FocusMode = MF`,
`AFLock = Off`, `PreConstAF = Off` — otherwise the camera drives the focus back.

Nothing sets them back. Offer a focus slider without a focus-mode control and
the camera is stuck in manual focus after the first drag, with no way back short
of a power cycle.

Related: `PreConstAF` is Pre-AF, not AF-C. Enabling it for AF-S turns single
autofocus into continuous autofocus, which looks like a camera fault rather than
a setting.
