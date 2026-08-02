# The Sigma fp over USB PTP

Everything here was measured against a real fp. Where the SDK documentation and
the camera disagree, the camera wins and the disagreement is written down.

The reference implementation is this repository. Start from
[`sigma_fp_focus.py`](../sigma_fp_focus.py) for the low-level layer and
[`ifd.py`](../ifd.py) for the encoding.

- [Getting on the bus](#getting-on-the-bus)
- [The session](#the-session)
- [How data is encoded](#how-data-is-encoded)
- [Opcodes](#opcodes)
- [Settings: the data groups](#settings-the-data-groups)
- [What the camera says it can do](#what-the-camera-says-it-can-do)
- [Focus](#focus)
- [Live view](#live-view)
- [Stills](#stills)
- [Movies](#movies)
- [The image database](#the-image-database)
- [What is not reachable](#what-is-not-reachable)

---

## Getting on the bus

The fp is a standard USB PTP device (vendor `0x1003`) with a vendor extension.
Any PTP library gets you a session; the interesting operations are all vendor
opcodes in the `0x90xx` range.

This project uses [sigma-ptpy](https://github.com/makanikai/sigma-ptpy) on top of
[ptpy](https://github.com/Parrot-Developers/ptpy) and pyusb. sigma-ptpy models
the SDK closely, which is useful, but it was written against the SD Quattro
generation and several of its tables are wrong for the fp. Those cases are
called out below.

**On macOS you are competing for the device.** `ptpcamerad` grabs any PTP camera
the moment it enumerates. See [GOTCHAS.md](GOTCHAS.md#macos-fights-you-for-the-camera).

---

## The session

```
ConfigApi      0x9035   open the vendor API
  ... everything else ...
CloseApplication 0x902F  leave it
```

`ConfigApi` is not optional. Until you send it the vendor opcodes return nothing
useful.

Two consequences that are easy to learn the hard way:

**Every control on the body goes dead** while the API is open. That is the
protocol, not a fault. The dial, the buttons, the touchscreen — all inert until
you send `CloseApplication`.

**`ConfigApi` resets settings.** Anything the operator set on the body before you
connected is gone. If you want it back you have to read the settings before
opening the API and write them again after — which is what this bridge does
around release/acquire.

---

## How data is encoded

Almost every vendor operation moves an **IFD** — the same shape as a TIFF
directory, little-endian throughout.

```
offset  size  field
0       4     DataLength     total bytes of the payload
4       4     DirectoryCount number of entries
8       12×N  entries
```

Each entry:

```
offset  size  field
0       2     Tag
2       2     Type      1=UInt8 2=Int8 3=UInt16 4=Int16 5=UInt32
                        6=Int32 7=String 8=Rational 9=Any8 ...
4       4     Count
8       4     Value, or an offset into the payload when the value
              does not fit in four bytes
```

**Tags in the SDK documentation are decimal, not hexadecimal.** This costs an
afternoon if you assume otherwise: every tag you look up lands on a plausible
but wrong field, and the camera answers plausibly to all of them.

`ifd.py` in this repo parses and builds these. It exists because sigma-ptpy's
own parser raises `IndexError` on this body's `CanSetInfo5` payload, and losing
the whole structure to one bad entry is not acceptable when the rest is
correct.

---

## Opcodes

Confirmed against the fp. Names are sigma-ptpy's.

| Op | Name | Notes |
|---|---|---|
| `0x9012` | GetCamDataGroup1 | exposure |
| `0x9013` | GetCamDataGroup2 | mode, quality, WB |
| `0x9014` | GetCamDataGroup3 | colour, destination |
| `0x9015` | GetCamCaptStatus | poll after a shot |
| `0x9016` | SetCamDataGroup1 | |
| `0x9017` | SetCamDataGroup2 | |
| `0x9018` | SetCamDataGroup3 | |
| `0x9019` | SetCamClockAdj | |
| `0x901B` | SnapCommand | shutter / AF drive |
| `0x901C` | ClearImageDBSingle | release one database entry |
| `0x9022` | GetBigPartialPictFile | pull a still |
| `0x9023` / `0x9024` | Get/SetCamDataGroup4 | corrections, stabilisation |
| `0x9027` / `0x9028` | Get/SetCamDataGroup5 | colour temp, aspect, interval |
| `0x9029` | GetLastCommandData | |
| `0x902A` | FreeArrayMemory | |
| `0x902B` | GetViewFrame | live view JPEG |
| `0x902C` | — | undocumented; see below |
| `0x902D` | GetPictFileInfo2 | describes the pending stills |
| `0x902F` | CloseApplication | |
| `0x9030` | GetCamCanSetInfo5 | what is settable right now |
| `0x9031` / `0x9032` | Get/SetCamDataGroupFocus | |
| `0x9033` / `0x9034` | Get/SetCamDataGroupMovie | |
| `0x9035` | ConfigApi | |
| `0x9036` | GetMovieFileInfo | |
| `0x9037` | GetPartialMovieFile | **only serves index 0** |

**`0x902C` is a change notification.** It is not in the SDK. It returns an IFD
whose tag 4 is a list of opcodes, and that list changes as you operate the
camera — it is the camera telling you which groups have new data. Useful if you
want to avoid polling everything.

The gaps (`0x9010`, `0x9011`, `0x901A`, `0x901D`–`0x9021`, `0x9025`, `0x9026`,
`0x902E`, `0x9038`–`0x9042`) were scanned on this body. Three answer:
`0x9010`, `0x9011`, `0x901A` return payloads with no discernible structure;
`0x902E` always returns empty; `0x9039` returns a small flag whose meaning is
unknown. The rest are silent or drop the device off the bus.

---

## Settings: the data groups

Settings live in six groups. Reading a group gives you an IFD; writing takes
one. **One write touches one group**, so batching changes means sorting them by
group first.

`camera_settings.py` has the full table. Summary:

| Group | Holds |
|---|---|
| 1 | aperture, shutter, ISO, ISO auto, exposure compensation |
| 2 | exposure mode, metering, white balance, image quality, resolution, drive mode |
| 3 | colour mode, colour space, destination to save |
| 4 | DNG quality, lens corrections, stabilisation, DC crop, HDR, fill light |
| 5 | colour temperature, aspect ratio, tone, interval timer |
| Movie | capture mode, shutter angle, record format, resolution, frame rate, audio |

Exposure values are **APEX-encoded**, not raw. f/2.0 is `24`, not `2`.
sigma-ptpy ships the conversion tables and one of its aperture codes is wrong
(`101` is missing, its neighbours being 99 = f/51 and 104 = f/64) — see
[the patches](GOTCHAS.md#sigma-ptpy-needs-patching).

### DataGroupMovie

Tags are decimal:

| Tag | Field |
|---|---|
| 1 | CaptureMode — 1 = STILL, 2 = CINE. **More reliable than inferring it.** |
| 6 | ShutterUnit — 1 = speed, 2 = angle |
| 7 | ShutterAngle |
| 10 | AudioRecord |
| 11 | NumOfVoiceChannels |
| 12 | GainAdjustMethod |
| 13 | ManualGainAdjustEV |
| 14 | WindNoiseCanceller |
| 50 | RecordFormat — 1 = CinemaDNG, 2 = MOV |
| 51 | CinemaDNGQuality |
| 52 | MovImageQuality |
| 60 | MovieResolution |
| 61 | FrameRate (rational) |
| 62 | Binning |

Tags 10–14 and 62 were identified through the relationship in the next section,
not from documentation.

**In CINE the shutter is set by angle.** Writing `ShutterSpeed` in DataGroup1
is accepted and then silently dropped. Convert: `seconds = angle / (360 × fps)`.

---

## What the camera says it can do

`GetCamCanSetInfo5` (`0x9030`) returns an IFD describing what is settable *right
now* — it changes with body mode, record format, resolution. This is how you
avoid offering a control that does nothing.

**The mapping that unlocks the movie tags:**

```
CanSetInfo5 tag = DataGroupMovie tag + 100
```

So `110` describes tag 10, `162` describes tag 62. That is how the audio fields
were named.

Three things about this structure will mislead you, all measured:

**1. The encoding of the contents is not consistent.** Some entries are lists of
allowed values. Others are not. `DCCropMode` declares `[1, 0, -1]`;
`LOCVignetting` declares `[0, -1]` and does not include the value currently in
effect. Treating every entry as a value list produces controls that refuse legal
settings.

This repo only trusts an entry as a value list when it contains no negatives
**and** contains the value currently in effect. Anything unverifiable is
rejected rather than assumed.

**2. Absence does not mean refusal.** White balance declares `[1..12]`, which
does not include `14` (Color Temp.). The camera accepts 14 and 15 anyway, in
both body modes, and reads them back. Meanwhile `13` — also absent — really is
refused. The declaration is what the body menu offers, not what the API takes.

**3. An empty entry does mean refusal, and that part is reliable.** `810
EImageStab` reads `[0, 1]` under MOV and `[]` under CinemaDNG, which matches the
fp not supporting electronic stabilisation with CinemaDNG. Emptiness is a
trustworthy signal even though the contents are not.

Focus-related entries:

| Tag | Meaning | This body |
|---|---|---|
| 600 | FocusMode | `[MF, AF_C, AF_S]` — no plain `AF` |
| 602 | FaceEyeAF | |
| 610 | FocusArea | |
| 612 | FocusAreaOverallArea | `[682, 1024]` |
| 613 | FocusAreaValidArea | `[85, 597, 96, 928]` |
| 614 | NumOfDMFSizes | `[3]` |
| 615 | DMFSize | `[128,128, 64,64, 32,32]` |
| 616 | DMFMovement | `[32, 16]` |

---

## Focus

`DataGroupFocus` (`0x9031` / `0x9032`), tags decimal:

| Tag | Field |
|---|---|
| 1 | FocusMode |
| 2 | AFLock |
| 3 | FaceEyeAF |
| 4 | FaceEyeAFStatus |
| 10 | FocusArea |
| 11 | OnePointSelection |
| 12 | DMFSize |
| 13 | DMFPos |
| 14 | DMFDetection |
| 51 | PreConstAF |
| 52 | FocusLimit |
| 80 | FocusState |
| 81 | FocusPosition |

### Driving the motor

`FocusPosition` (81) is the lens's internal motor position. On the 45mm F2.8 the
usable range reads `5974 – 11116`; it is lens- and zoom-dependent, so read it
rather than assuming.

Writing a position requires `FocusMode = MF` in the same write, or the camera
takes the focus back immediately. This repo writes `MF`, `AFLock = Off`,
`PreConstAF = Off` alongside every position. **Nothing writes those back** unless
you provide a way to — which is why a focus-mode control is not optional if you
offer a focus slider.

### The AF point

`DMFPos` (13) is `Any8` with count 4: two little-endian `UInt16`, **`(y, x)`**.
The factory value `54 01 00 02` is `(340, 512)` — dead centre of the 682×1024
space, which is how the ordering was confirmed.

The coordinate space is 682×1024, a 3:2 frame. **A 16:9 preview only shows the
middle band of it**, so mapping a click on the preview to a coordinate has to
account for the crop or the marker drifts vertically.

Where the point may go:

```
reachable centre = FocusAreaValidArea inset by half the current DMFSize
```

Measured at all three sizes by writing each of the four corners and reading back:

| Frame | Reachable y | Reachable x |
|---|---|---|
| 128×128 | 149 – 533 | 160 – 864 |
| 64×64 | 117 – 565 | 128 – 896 |
| 32×32 | 101 – 581 | 112 – 912 |

The camera also snaps to a grid — `DMFMovement` `[32, 16]` — so a written
position comes back rounded. That is not a failed write.

**The AF area is genuinely smaller than the frame**: `[85, 597] × [96, 928]` out
of 682×1024 is about 75% × 81%. There is no focus point in the outer band. A UI
that hides this looks broken when clicks near the edge snap inward.

### Modes

`FocusArea` must be `OnePointSelection` for `DMFPos` to mean anything. In
multi-point the camera pins the point to the centre and every click focuses the
same place.

`PreConstAF` (51) is the body's **Pre-AF**, not AF-C. Turning it on for AF-S
makes the camera hunt continuously — single AF that behaves like continuous AF.
Tie it to AF-C only.

**AF-C is refused in CINE.** Write it and it reads back `AF_S`. In STILL it
takes. Measured in both modes.

Moving the point does not focus. Send `SnapCommand` (`0x901B`) with AF-drive-only
afterwards, or nothing moves and it looks like the point did not take.

---

## Live view

`GetViewFrame` (`0x902B`) returns a JPEG. There is no streaming mode: you ask
for a frame, you get a frame. About **24 fps** is achievable on this body over
USB 2.0, at roughly 500 KB per frame.

The rate you get is the rate the camera answers at. Sleeping a fixed interval
*after* the fetch adds to the round-trip rather than bounding it — the
difference measured here was 15.2 fps versus 24.3 fps for the same target
interval.

---

## Stills

```
SnapCommand 0x901B          fire
GetCamCaptStatus 0x9015     poll until the image is ready
GetPictFileInfo2 0x902D     find out what was produced
GetBigPartialPictFile 0x9022  pull it in chunks
ClearImageDBSingle 0x901C   release the entry
```

`GetPictFileInfo2` returns a variable-length structure:

```
DataLength           u32
FileCount            u32
RecordOffset[]       u32 × FileCount    offsets to each record
records:
  FileAddress        u32
  FileSize           u32
  PathNameOffset     u32
  FileNameOffset     u32
  PictureFormat      char[4]
  SizeX, SizeY       u16
```

sigma-ptpy assumes a fixed 12 bytes of unknown header, which is only correct
when `FileCount == 1`. **DNG+JPEG produces two records** and that assumption
puts you at the wrong offset. Walk the offset table.

Both `DNG` and `DNGAndJPEG` work over tethered capture on this body.

---

## Movies

Recording is two opcodes on DataGroupMovie, and the footage lands on the card.

Pulling it back:

```
GetMovieFileInfo    0x9036   parameter = database entry index
GetPartialMovieFile 0x9037   parameters = (0, offset, 0, length)
```

`MovieFileInfo` has the same shape as `PictFileInfo2` but with 64-bit fields and
**no `FileAddress`** — the movie is addressed by database slot, not by memory
address.

Measured behaviour of `0x9037`, all of it load-bearing:

**It serves database slot 0 and nothing else.** The parameter that looks like an
index is not one. Once the database head has advanced past 0, the movie you just
recorded cannot be pulled at all; you have to release and re-acquire so the
database starts from 0 again.

**Calling it when there is no servable movie kills the connection.** Not an
error return: `USBTimeoutError`, and the camera drops off USB entirely. Only a
power cycle brings it back. Restarting your program does not help — it is the
camera's state, not the session's. Guard the call.

**A short read is garbage, not a partial read.** Ask for a chunk near the end
and you can get 122,868 bytes of something that is not your file. Treat
`len(data) != requested` as an error rather than accumulating it, or you will
write a file that looks plausible and is not.

**Do not send it while recording.** Measured twice: once the camera stopped
serving transfers, once it dropped off the bus.

Throughput is around **56 MB/s** with 1 MB chunks.

### Reading during a take

You cannot. The file is not registered in the database until the take ends, so
there is nothing to address. There is no streaming path over PTP.

---

## The image database

Entries occupy `[ImageDBHead, ImageDBTail)`. A shot's entry is numbered by the
**tail read before the shot**, not after.

**An unreleased entry stops the shutter.** Take a picture, do not release the
entry, and the next `SnapCommand` does nothing — with no error. Release with
`ClearImageDBSingle` (`0x901C`) once you have the data.

This is also why the movie download only ever works on a fresh database.

---

## What is not reachable

**UHD 12-bit CinemaDNG at 29.97 fps.** On the body this requires switching USB
into external-SSD mode, and in that mode the camera is a mass-storage device —
the vendor API is not there to ask. Every undocumented opcode from `0x9010` to
`0x9042` was scanned looking for a way to enable it from the API side; none
does. The data path in SSD mode is block writes to the attached volume, not PTP.

**PTP events.** The event endpoint exists and this body never uses it, verified
with a stills capture as a control — the camera reports nothing where an event
would be the obvious mechanism. Poll instead.

---

## Open questions

Written down so nobody re-derives them:

- What puts the camera into the "will not serve transfers" state. Reproducible
  by misusing `0x9037`, but not fully characterised.
- `0x902E` always returns an empty payload, in every state tried.
- `0x9039` returns a small flag; the meaning is unknown.
- DataGroupMovie tag 5 is unidentified.
- Movie tags 11–14 are named from the `+100` relationship but their value
  encodings are unverified.
- Whether 682×1024 maps exactly onto the preview, or only approximately.
