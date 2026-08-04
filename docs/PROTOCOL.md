# The Sigma fp over USB PTP

**The protocol is Sigma's. The measuring is ours.** Everything here was checked
against a real fp; where the SDK and the camera disagree, the camera wins.

Not in the SDK, worked out from behaviour: the `+100` relationship that names
the movie tags, `0x902C`, the single-slot movie download, where the AF point may
go, the fp's colour mode table.

*(The HTTP API this project puts in front of it is [a separate thing](API.md).)*

```
     your code
         │
    sigma-ptpy ──── patched, see GOTCHAS
         │
       ptpy
         │
       pyusb ──── libusb
         │
     ┌───┴────┐
     │Sigma fp│  vendor opcodes 0x90xx
     └────────┘
```

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

Standard USB PTP device, vendor `0x1003`. Any PTP library gets you a session;
everything interesting is a vendor opcode in `0x90xx`.

[sigma-ptpy](https://github.com/makanikai/sigma-ptpy) models the SDK closely but
was written for the SD Quattro — several of its tables are wrong for the fp
([which ones](GOTCHAS.md#3-sigma-ptpy-is-for-a-different-camera)).

On macOS you are [competing for the device](GOTCHAS.md#1-macos-takes-the-camera-first).

---

## The session

```
                    ConfigApi 0x9035
   body controls  ─────────────────────►  body controls DEAD
   work                                   vendor opcodes work
   vendor opcodes                         settings were RESET
   return nothing
                    CloseApplication 0x902F
                  ◄─────────────────────
```

`ConfigApi` is not optional, and it costs two things:

| | |
|---|---|
| Every control on the body goes dead | dial, buttons, touchscreen — until `CloseApplication` |
| Settings are reset | whatever the operator set before you connected is gone |

To hand the camera back and take it again without destroying their setup, read
the settings before closing and write them back after re-opening.

---

## How data is encoded

**There are two encodings, and which one you get depends on the operation.**
Assuming one shape for everything is the first thing that will break your
implementation.

| Encoding | Used by |
|---|---|
| Bitmask struct | DataGroup 1, 2, 3, 4, 5 |
| IFD | ApiConfig, DataGroupFocus, DataGroupMovie, CanSetInfo5 |

### Bitmask struct — DataGroup 1 to 5

```
offset  size  field
0       1     header        on reads: byte count of the two rows below
                            on writes: 0 is accepted
1       2     FieldPresent  bitmask — see below
3       ...   the present fields, in the schema's fixed order
last    1     parity        sum of every preceding byte, mod 256
```

`FieldPresent` says which fields follow. **The fields are not tagged** — they
appear in a fixed order and you work out which is which by walking the bitmask.
Set a bit to include that field in a write; leave it clear and the camera keeps
its current value.

**The first byte of the mask carries fields 1–8, the second carries 9–16, least
significant bit first.** Equivalently: a little-endian uint16 where bit *n* is
the *n*-th field of the schema.

An earlier version of this section called the mask **big-endian**. It is not, and
a read settles it: a payload whose mask is `ff 7f` decodes to exactly fifteen
fields. Little-endian makes that bits 0–14, contiguous, which is what "the camera
sent everything it has" looks like. Big-endian would make it bits 0–6 and 8–15 —
a hole at bit 7, claiming the eighth field is absent while a sixteenth field that
does not exist is present.

Verified encodings, one field at a time and then together:

```
ISOAuto  only    00  08 00  00      00
ISOSpeed only    00  10 00  28      00
both             00  18 00  00 28   00
                 ↑   ↑      ↑       ↑
            header  mask   values  one parity, always last
```

The masks OR together and the values queue up behind them in bit order. There is
only ever **one** parity byte, so two payloads do not concatenate — reading the
tails as separate units is the mistake this example exists to prevent.

Parity is a plain checksum: every byte before it, summed, mod 256. Two reads
confirm it (`0xaf`, `0xe9`). Writes from sigma-ptpy send `0x00` instead and the
camera accepts them, so it is only load-bearing in the read direction.

### IFD — everything else

The same shape as a TIFF directory, little-endian throughout.

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

Settings live in six groups. **One write touches one group**, so batching
changes means sorting them by group first.

Groups 1 to 5 use the bitmask struct; Movie uses an IFD. The bit values below
are the `FieldPresent` mask — set the bit and the field is included.

### DataGroup1 — `0x9012` / `0x9016`

| Bit | Field | Type |
|---|---|---|
| `0x8000` | ABSetting | enum |
| `0x4000` | ABValue | uint8 |
| `0x2000` | ExpComp | uint8, APEX |
| `0x1000` | ISOSpeed | uint8, APEX |
| `0x0800` | ISOAuto | enum |
| `0x0400` | ProgramShift | enum |
| `0x0200` | Aperture | uint8, APEX |
| `0x0100` | ShutterSpeed | uint8, APEX |
| `0x0040` | ExpCompExcludeAB | uint8 |
| `0x0020` | ABShotRemainNumber | uint8 |
| `0x0010` | BatteryState | uint8, read-only |
| `0x0008` | CurrentLensFocalLength | fixed-point uint16, read-only |
| `0x0004` | MediaStatus | uint8, read-only |
| `0x0002` | MediaFreeSpace | uint16, read-only |
| `0x0001` | FrameBufferState | uint8, read-only |

### DataGroup2 — `0x9013` / `0x9017`

| Bit | Field | Type |
|---|---|---|
| `0x0800` | AEMeteringMode | enum |
| `0x0400` | ExposureMode | enum |
| `0x0200` | SpecialMode | enum |
| `0x0100` | DriveMode | enum |
| `0x0080` | ImageQuality | enum |
| `0x0040` | Resolution | enum |
| `0x0020` | WhiteBalance | enum |
| `0x0008` | FlashSetting | enum |
| `0x0004` | FlashMode | enum |
| `0x0001` | FlashType | enum |

### DataGroup3 — `0x9014` / `0x9018`

| Bit | Field | Type |
|---|---|---|
| `0x8000` | LensTeleFocalLength | fixed-point uint16, read-only |
| `0x4000` | LensWideFocalLength | fixed-point uint16, read-only |
| `0x2000` | BatteryKind | enum, read-only |
| `0x1000` | ColorMode | enum |
| `0x0800` | ColorSpace | enum |
| `0x0080` | DestToSave | enum |
| `0x0020` | TimerSound | uint8 |
| `0x0002` | AFBeep | uint8 |
| `0x0001` | AFAuxLight | enum |

### DataGroup4 — `0x9023` / `0x9024`

| Bit | Field | Type |
|---|---|---|
| `0x8000` | ContShootSpeed | enum |
| `0x4000` | HighISOExt | enum |
| `0x2000` | LVMagnifyRatio | enum |
| `0x1000` | DCCropMode | enum |
| `0x0020` | ShutterSound | uint8 |
| `0x0010` | EImageStab | enum |
| `0x0008` | LOC | struct — the four lens corrections together |
| `0x0004` | FillLight | int8 |
| `0x0002` | DNGQuality | enum |
| `0x0001` | HDR | enum |

`LOC` is one bit covering distortion, chromatic aberration, diffraction and
vignetting; they cannot be written separately.

### DataGroup5 — `0x9027` / `0x9028`

| Bit | Field | Type |
|---|---|---|
| `0x2000` | ToneEffect | enum |
| `0x0800` | AspectRatio | enum |
| `0x0200` | ColorTemp | uint16, kelvin |
| `0x0100` | IntervalTimer | struct — seconds and frame count |
| `0x0080` | AFAuxLightEF | enum |

Exposure values are **APEX-encoded**, not raw. f/2.0 is `24`, not `2`.
sigma-ptpy ships the conversion tables and one of its aperture codes is wrong
(`101` is missing, its neighbours being 99 = f/51 and 104 = f/64) — see
[the patches](GOTCHAS.md#3-sigma-ptpy-is-for-a-different-camera).

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

**In CINE, `shutter_unit` (movie tag 6) decides which shutter field is live.**
Measured in CINE with Manual exposure, writing a value taken from the camera's
own declared list:

| `shutter_unit` | `shutter_speed` | `shutter_angle` |
|---|---|---|
| 1 — speed | 61 choices, writes land | no list |
| 2 — angle | no list | 18 choices, writes land |

An earlier version of this section said the shutter is *always* set by angle in
CINE and that `ShutterSpeed` in DataGroup1 is silently dropped. That is only true
in angle mode. In speed mode DataGroup1 carries the shutter exactly as it does in
STILL.

The claim came from comparing STILL against CINE and seeing `shutter_speed` offer
61 choices in one and none in the other — but `shutter_unit` was in angle mode on
the CINE side. Two variables moved and the result was credited to the wrong one.

**No list is not the same as not writable.** They fail identically from the
outside: the write returns 0x2001 and nothing happens. Check which field has a
list before deciding a write is being refused.

**`shutter_unit` also changes what tag 7 holds.** In angle mode it is the
angle × 10. In speed mode the numerator is the **shutter's APEX code** — read back
as `(112, 3600)` while DataGroup1 independently reported code 112 = 1/125.
Decoding tag 7 as an angle regardless of the unit produces nonsense (11.2°); the
bridge currently does this.

**Manual (or Shutter Priority) is a precondition for setting the shutter at
all.** In P or A mode the camera owns it: the write returns 0x2001 and the value
never changes. See the exposure-mode table below for which field each mode
leaves to you. `ConfigApi` resets the camera to its defaults, so a fresh session
is *not* in Manual — configure inside the session.

**Frame rate and shutter overwrite each other.** Writing the frame rate coerces
the shutter to suit it (1/60 became 1/25 after a 29.97 write); writing the
shutter knocks the frame rate back to 23.98. Six orderings were tried through the
raw protocol without getting both to stick; the bridge's settings layer manages it
by sending back the camera's own declared encoding from `CanSetInfo5` rather than
deriving one (`movie_settings._encode_preferring_camera_form`). Do not hand-roll
these writes.

⚠️ `CanSetInfo5` declares no capability tag for the shutter (107) or for
`shutter_unit` (106), so there is no allowed-value list to send back for those
two — which is why they are the two that resist.

**The shutter APEX table has a hole**: code 100 decodes to `None`, the same class
of gap as the missing aperture code 101.

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

Three ways this will mislead you, all measured:

| | Example | What to do |
|---|---|---|
| Contents are **not** a consistent value list | `DCCropMode` → `[1, 0, -1]`; `LOCVignetting` → `[0, -1]`, missing the value in effect | trust it only when it has no negatives **and** contains the current value |
| **Absence ≠ refusal** | WB declares `[1..12]`; `14` Color Temp. is accepted anyway. `13` really is refused. | it lists the body menu, not what the API takes |
| **Empty = refusal**, reliably | `810 EImageStab` → `[0,1]` under MOV, `[]` under CinemaDNG | grey it out |

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

The coordinate space is 682×1024, a 3:2 frame:

```
  x=0                                            x=1024
y=0 ┌──────────────────────────────────────────────┐
    │                                              │
    │        ┌────────────────────────────┐        │ ← AF area
 85 │        │                            │        │   613
    │        │      ┌──────────────┐      │        │
    │        │      │              │      │        │ ← where the
    │        │      │   ● 340,512  │      │        │   point may go
    │        │      │              │      │        │   (64×64 frame)
    │        │      └──────────────┘      │        │
597 │        │      117          565      │        │
    │        └────────────────────────────┘        │
    │        96   128        896   928             │
y=682 ──────────────────────────────────────────────┘
```

**A 16:9 preview only shows the middle band**, so mapping a click has to account
for the crop or the marker drifts vertically.

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

## What each command costs

Measured through ImageCaptureCore, 30 calls each, camera in CINE / CinemaDNG /
FHD. The libusb path lands within 0.4 ms of these, so the numbers are the
camera's, not the transport's.

| Command | Median | p90 | Returns |
|---|---|---|---|
| `SetCamDataGroupFocus` 0x9032 | **0.42 ms** | 0.48 | — |
| `GetCamCaptStatus` 0x9015 | 0.53 ms | 0.68 | 8 B |
| `GetDeviceInfo` 0x1001 | 0.59 ms | 1.41 | 277 B |
| `GetCamDataGroupFocus` 0x9031 | 0.90 ms | 1.15 | 177 B |
| `GetCamDataGroup1..5` | ~1.0 ms | ~1.6 | 14–21 B |
| `GetCamDataGroupMovie` 0x9033 | 1.06 ms | 2.26 | 221 B |
| `GetCamCanSetInfo5` 0x9030 | 1.67 ms | 2.45 | 1311 B |
| `SetCamDataGroup1` 0x9016 | **4.42 ms** | 5.60 | — |
| `GetViewFrame` 0x902B | **29.7 ms** | 31.2 | 600–644 KB |

Three things worth taking from this.

**Reads cost the same regardless of size.** 8 bytes and 1439 bytes are both about
a millisecond, so the cost is per-transaction overhead, not payload.

**Writes are not all alike.** Changing exposure (`SetCamDataGroup1`) takes 4.4 ms
— ten times a focus write. Driving the lens (`SetCamDataGroupFocus`) is 0.42 ms
and does not depend on whether the motor actually moves (0.41 ms alternating
between two positions, 0.42 ms rewriting the same one), so the command returns
before the motor does.

**A frame is 30× everything else**, which is why the priority queue puts live
view last: running the 1 ms job first delays the frame by 1 ms, running the frame
first delays control by 30 ms.

At 30 fps live view plus a focus command and readback every frame, the line needs
891 + 13 + 27 = **931 ms per second** — tight, but not over.

---

## Every setting, and where its legal values come from

Generated from `camera_settings.SETTINGS` and `movie_settings.MOVIE_SETTINGS` —
if this table and the code disagree, the code is right.

**DataGroup 1–5** (bitmask encoding, opcodes 0x9012–0x9028):

| Setting | Group | Field | Type | Enum / converter | Writable |
|---|---|---|---|---|---|
| `aperture` | 1 | `Aperture` | apex | APEX converter | yes |
| `exposure_compensation` | 1 | `ExpComp` | apex | APEX converter | yes |
| `iso` | 1 | `ISOSpeed` | apex | APEX converter | yes |
| `iso_auto` | 1 | `ISOAuto` | enum | ISOAuto | yes |
| `shutter_speed` | 1 | `ShutterSpeed` | apex | APEX converter | yes |
| `drive_mode` | 2 | `DriveMode` | enum | DriveMode | yes |
| `exposure_mode` | 2 | `ExposureMode` | enum | ExposureMode | yes |
| `image_quality` | 2 | `ImageQuality` | enum | ImageQuality | yes |
| `metering_mode` | 2 | `AEMeteringMode` | enum | AEMeteringMode | yes |
| `resolution` | 2 | `Resolution` | enum | Resolution | yes |
| `white_balance` | 2 | `WhiteBalance` | enum | WhiteBalance | yes |
| `color_mode` | 3 | `ColorMode` | enum | FpColorMode | yes |
| `color_space` | 3 | `ColorSpace` | enum | ColorSpace | yes |
| `dest_to_save` | 3 | `DestToSave` | enum | DestToSave | yes |
| `cont_shoot_speed` | 4 | `ContShootSpeed` | enum | ContShootSpeed | yes |
| `dc_crop_mode` | 4 | `DCCropMode` | enum | DCCropMode | yes |
| `dng_quality` | 4 | `DNGQuality` | enum | DNGQuality | yes |
| `electronic_stabilization` | 4 | `EImageStab` | enum | EImageStab | yes |
| `fill_light` | 4 | `FillLight` | int | — | yes |
| `hdr` | 4 | `HDR` | enum | HDR | yes |
| `high_iso_ext` | 4 | `HighISOExt` | enum | HighISOExt | yes |
| `loc_chromatic_aberration` | 4 | `LOCChromaticAberration` | enum | LOCChromaticAberration | yes |
| `loc_diffraction` | 4 | `LOCDiffraction` | enum | LOCDiffraction | yes |
| `loc_distortion` | 4 | `LOCDistortion` | enum | LOCDistortion | yes |
| `loc_vignetting` | 4 | `LOCVignetting` | enum | LOCVignetting | yes |
| `aspect_ratio` | 5 | `AspectRatio` | enum | AspectRatio | yes |
| `color_temp` | 5 | `ColorTemp` | int | — | yes |
| `interval_timer_frames` | 5 | `IntervalTimerFrame` | int | — | yes |
| `interval_timer_seconds` | 5 | `IntervalTimerSecond` | int | — | yes |
| `tone_effect` | 5 | `ToneEffect` | enum | ToneEffect | yes |

**DataGroupMovie** (IFD encoding, 0x9033 / 0x9034):

| Setting | Tag | Capability tag | Type | Meaning |
|---|---|---|---|---|
| `capture_mode` | 1 | 101 | UInt8 | int |
| `shutter_unit` | 6 | 106 | UInt8 | int |
| `shutter_angle` | 7 | 107 | URational | angle |
| `audio_record` | 10 | 110 | UInt8 | int |
| `voice_channels` | 11 | 111 | UInt8 | int |
| `gain_adjust_method` | 12 | 112 | Int8 | int |
| `manual_gain_ev` | 13 | 113 | Int8 | int |
| `wind_noise_canceller` | 14 | 114 | UInt8 | int |
| `record_format` | 50 | 150 | UInt8 | int |
| `cinema_dng_quality` | 51 | 151 | UInt8 | int |
| `mov_image_quality` | 52 | 152 | UInt8 | int |
| `movie_resolution` | 60 | 160 | UInt8 | int |
| `frame_rate` | 61 | 161 | URational | rational |

Legal values come from `GetCamCanSetInfo5` (0x9030), where **the capability tag
is the movie tag plus 100**. Two settings have no capability tag at all —
`shutter_unit` (106) and the shutter (107) — so for those there is no declared
list, and `movie_settings.FALLBACK_CHOICES` supplies `[1, 2]` for
`shutter_unit` and `capture_mode` from observation instead.

---

## Settings that change other settings

Writing one field can move another. Everything below was observed on the camera;
where it was not, it says so. Read this before hand-rolling any write sequence —
six orderings were tried through the raw protocol before the pattern was clear.

### What restricts what

Measured by writing each setting and reading the camera's declared choice lists
back. The schema endpoint re-reads capabilities before answering, so these lists
are live, not cached.

**Recording format and resolution each restrict the frame rate, and they stack:**

| Format | Resolution | Frame rates offered | DNG bit depth |
|---|---|---|---|
| CinemaDNG | FHD | 23.98, 25, 29.97, 50, 59.94 | 12 / 10 / 8 |
| CinemaDNG | UHD | 23.98, 25 | **not adjustable** |
| MOV | FHD | 23.98, 25, 29.97, 50, 59.94, **100, 111.98** | 12 / 10 / 8 |
| MOV | UHD | 23.98, 25, 29.97 | 12 / 10 / 8 |

UHD is stricter than FHD, CinemaDNG is stricter than MOV, and the two combine —
CinemaDNG at UHD leaves only 23.98 and 25. The high-speed rates exist only in
MOV at FHD. (MOV still reports a DNG bit depth; it means nothing in that format.)

**Choosing a rate that the next mode cannot offer loses it.** Set 59.94 at FHD,
switch to UHD, and the camera does not fall back to the nearest legal rate
(29.97) — it drops to 23.98.

**STILL ↔ CINE swaps the whole instrument.** Eighteen capability lists change:

```
STILL only   aspect_ratio(7) color_space(3) drive_mode(4) hdr(6) image_quality(5)
             resolution(3) dng_quality(2) tone_effect(2) high_iso_ext(3)
             cont_shoot_speed(3)   shutter_speed(61)
CINE only    frame_rate(5) movie_resolution(2) record_format(2)
             cinema_dng_quality(3) shutter_unit(2)
both, differ exposure_compensation 19 → 31,  shutter_angle 18 → 10
```

The camera also rewrites five values on the way across: `aspect_ratio`
(16:9 → 3:2), `resolution`, `shutter_speed`, `shutter_unit` (2 → 1) and, with ISO
on auto, the ISO itself.

`shutter_speed` offering **61 choices in STILL and none in CINE** is the cleanest
statement of the rule that cost the most time to find: in CINE the shutter is not
in DataGroup1 at all, and the camera does not even claim it is.

**Exposure mode decides who owns the shutter and the aperture.** Tested by
writing a value taken from the camera's own list, in CINE:

| Exposure mode | shutter | aperture | ISO |
|---|---|---|---|
| Manual | ✓ | ✓ | ✓ |
| ShutterPriority | ✓ | ✗ | ✓ |
| AperturePriority | ✗ | ✓ | ✓ |
| ProgramAuto | ✗ | ✗ | ✓ |

Exactly what the mode names promise. Writes to a field the camera owns return
0x2001 and change nothing.

**`shutter_unit` decides which shutter field has a list at all.** In angle mode
`shutter_angle` has choices and `shutter_speed` has none; in speed mode it is the
other way round. *No list* and *not writable* are different failures, and
confusing them is why hand-rolled shutter writes kept failing — they were aimed
at the field that had no list.

### Corrected: ISO is not owned by the camera

An earlier version of this document said `ISOSpeed` is only writable when
`iso_auto` is Manual, and that the camera overrides it otherwise. **Both claims
are wrong.** Writing ISO 400 landed and stayed in all four combinations of
exposure mode × ISO auto, including after forcing a full stop of exposure change
by moving the shutter angle — which would have made an active auto-ISO recompute:

| Exposure mode | `iso_auto` | after write | after 8 s | after forced re-meter |
|---|---|---|---|---|
| ProgramAuto | Auto | 400 | 400 | — |
| ProgramAuto | Manual | 400 | 400 | — |
| Manual | Auto | 400 | 400 | 400 |
| Manual | Manual | 400 | 400 | 400 |

What the earlier claim actually rested on was an ISO of 32 in one probe run and
43 in the next. Those were **two different sessions**, and `ConfigApi` resets the
settings when a session opens — so that difference is the reset, not the camera
re-metering. A gap between runs was read as a change within one.

No case has been seen where the camera takes back a written ISO. If it happens,
it needs a different provocation than any tried here.

### The full dependency map

Every setting with two or more choices was swept: change it, then diff **all**
other values and **all** other choice lists. Run in Manual exposure with Manual
ISO so the camera is not re-metering underneath, and preceded by a null control
— snapshot twice while changing nothing — which came back empty, so anything the
sweep reports is caused by the write.

**Restrictions — X changes what Y is allowed to be:**

| Setting | Restricts |
|---|---|
| `capture_mode` | eighteen lists; STILL and CINE are different instruments |
| `record_format` | `frame_rate`, `cinema_dng_quality`, `mov_image_quality` |
| `movie_resolution` | `frame_rate` |
| `shutter_unit` | which of `shutter_angle` / `shutter_speed` has a list at all |
| `exposure_mode` | `exposure_compensation`; and who owns shutter and aperture |
| `electronic_stabilization` | **`iso`: 100–25600 becomes 100–6400 when on** |

The stabiliser one has a physical reading: EIS crops and adds processing, and the
sensor readout modes that reach 12800 and 25600 are not available in that state.

**Coercions — X changes Y's value:**

| Setting | Changes |
|---|---|
| `exposure_mode` | `shutter_angle`, `shutter_speed` |
| `iso_auto` | `iso` |
| `shutter_unit` | `shutter_speed` |
| `capture_mode` | `aspect_ratio`, `resolution`, `shutter_speed`, `shutter_unit`, `iso` |
| `movie_resolution`, `record_format` | `frame_rate`, when the current rate is no longer legal |

**Not a dependency: `exposure_compensation` in Manual is a meter reading.**

It appeared to move with twelve different settings, which is what a dependency
looks like from a diff. It is not one. In Manual the field reports the deviation
between the metered exposure and the one dialled in — the needle. Closing the
aperture drives it negative, raising ISO drives it back:

```
f/1.4 ISO 125   −3.30
f/2.0 ISO 125   −4.00      stop down → more negative
f/2.8 ISO 125   −4.30      clamped near the end of the range
f/2.8 ISO 200   −4.00      ISO up → back toward zero
f/2.8 ISO 400   −3.30
```

Anything that changes exposure moves it. Reading that as "twelve settings depend
on each other" would have been wrong in a way no amount of care with the
measurement would catch — only knowing what the field means catches it.

### Not verified

- Whether the camera eventually overrides a written ISO while `iso_auto` is Auto.
  Four seconds of stable light was not enough to provoke it, and there is no way
  to force a metering change on demand from here.
- Whether `frame_rate` restricts the available shutter values. `CanSetInfo5`
  declares no tag for the shutter (107) or for `shutter_unit` (106), so there is
  no list to compare.

---

## Call sequences that matter

Taken from what the bridge actually does, not from the SDK docs.

### Connecting

```
SigmaPTPy()                  open the USB device
  cam.session().__enter__()  open the PTP session
  ConfigApi          0x9035  open the vendor API   ← resets settings to defaults
read focus range             GetCamCanSetInfo5 → travel, point sizes, step
read capabilities            CanSetInfo5 + DataGroup1..5 + DataGroupMovie
```

`ConfigApi` is not optional: vendor opcodes before it are not answered, and it
locks the body controls.

**Settings live inside the session.** When it closes, the camera returns to the
values on its own menus, so every connection has to configure again. Calling
`ConfigApi` a second time *within* a session resets nothing — six fields were
watched across a repeat call and none moved — so the reset people describe is
what happens on close, not on open.

This is worth holding onto because it makes cross-session comparisons
meaningless: two probe runs reporting different ISOs look like the camera
re-metering, and are actually just the session boundary.

Closing is the mirror: `CloseApplication` (0x902F), then the session, then USB.
Skipping it leaves the body locked.

### Driving focus manually

```
SetCamDataGroupFocus 0x9032  FocusMode=MF, AFLock=Off, PreConstAF=Off, FocusPosition
GetCamDataGroupFocus 0x9031  read back
  position did not land? → write again   (only when leaving AF; see GOTCHAS 1b)
```

All four fields go in **one** transaction. Any of the three switches left on and
the camera takes focus back: `PreConstAF` defaults to On and will override the
position within a frame or two.

### Handing focus back to the camera

```
SetCamDataGroupFocus 0x9032  FocusMode=AF_S (PreConstAF as wanted)
SetCamDataGroupFocus 0x9032  FaceEyeAF / FocusArea / DMFPos as wanted
SnapCommand          0x901B  AFDriveOnly   ← without this nothing moves
```

Changing the target is not enough. The settings land, the camera reports it can
see a face, and the lens stays where it was — the AF engine only runs when
something triggers it. Every change of target needs the trigger.

### Changing settings

```
GetCamCanSetInfo5    0x9030  what the camera says it accepts, in its own encoding
SetCamDataGroup1..5  0x9016/17/18/24/28   bitmask groups
SetCamDataGroupMovie 0x9034  IFD, sparse — untouched tags are left alone
read back                    always
```

Send back **the camera's own encoding** from the capability list rather than
deriving one (`movie_settings._encode_preferring_camera_form`). Hand-derived
encodings are where the resistant writes come from: the frame rate is declared as
`(2997, 100)`, and sending an equivalent-but-different rational is not the same
thing to the camera.

Order, when several settings change together: **mode first, format second,
frame rate third, shutter last.** The earlier ones move the later ones.

---

## Live view

`GetViewFrame` (`0x902B`) returns a JPEG. There is no streaming mode: you ask
for a frame, you get a frame. About **24 fps** was measured on this body over
USB 2.0, at roughly 500 KB per frame; through a Thunderbolt hub it reaches
**33 fps at 623 KB** (29.7 ms per frame, 20 MB/s), and both transports tested —
libusb and ImageCaptureCore — land within 0.4 ms of each other, so that ceiling
is the camera's.

**There is no size or quality control for it.** `ConfigApi` (`0x9035`) declares
only identity — name `SIGMA fp`, serial, firmware `V91`, API version 1.24 — and
nothing about the view frame. So a frame costs 29.7 ms of the single PTP line
whatever else is waiting, which is 89% of the line at 30 fps. Every control
command therefore waits an average of 15 ms and at worst 30 ms behind the frame
already in flight. That is structural: PTP has one transaction at a time, and
the frame cannot be made smaller.

The rate you get is the rate the camera answers at. Sleeping a fixed interval
*after* the fetch adds to the round-trip rather than bounding it — the
difference measured here was 15.2 fps versus 24.3 fps for the same target
interval.

---

## Stills

```
  read database tail          ← this is the entry number
         │
  SnapCommand           0x901B      fire
         │
  GetCamCaptStatus      0x9015      poll ──┐
         │                    ▲            │ until ready
         │                    └────────────┘
  GetPictFileInfo2      0x902D      what was produced
         │
  GetBigPartialPictFile 0x9022      pull it, in chunks
         │
  ClearImageDBSingle    0x901C      release, or the shutter stops
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
  GetMovieFileInfo     0x9036   param = database index
         │
         ▼
  is there a movie at index 0?
         │
    no ──┴── yes
     │        │
     ▼        ▼
  DO NOT   GetPartialMovieFile 0x9037  (0, offset, 0, length)
  CALL              │
  (kills the        └── len(data) != requested → error, not a partial read
   connection)
```

`MovieFileInfo` has the same shape as `PictFileInfo2` but with 64-bit fields and
**no `FileAddress`** — the movie is addressed by database slot, not by memory
address.

Measured behaviour of `0x9037`, all load-bearing:

| | |
|---|---|
| Serves **slot 0 only** | the index-looking parameter is not one. Head past 0 → your take is unreachable; release + acquire to reset. |
| **Nothing there → the camera dies** | `USBTimeoutError`, drops off USB, power cycle only. Restarting your program does nothing. |
| **Short read = garbage** | 122,868 bytes of something else. Treat `len != requested` as an error, never accumulate. |
| **Never during recording** | measured twice: once stopped serving, once dropped off the bus. |

~56 MB/s with 1 MB chunks.

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

**UHD 12-bit CinemaDNG at 29.97 fps.** It needs USB in external-SSD mode, and in
that mode the camera is a mass-storage device — there is no vendor API to ask.
Every undocumented opcode `0x9010`–`0x9042` was scanned for a way in; none. The
data path there is block writes to the volume, not PTP.

**PTP events.** The endpoint exists; this body never uses it. Verified with a
stills capture as a control. Poll instead.

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
