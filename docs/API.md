# HTTP and WebSocket API

**This API is this project's own design**, not Sigma's. It is a layer over the
camera's PTP protocol, which is [documented separately](PROTOCOL.md). Everything
the browser page does, it does through this — nothing here is private to the UI.

Default base URL: `http://localhost:1025/`. Override the port with
`SIGMA_BRIDGE_PORT`.

Requests that need the camera answer `503 {"error": "not connected"}` when it is
not. Bad input is `400`. A camera operation that does not come back is `504`
with the step it stalled on.

```
GET   /api/status              never blocks, answers even when PTP is wedged
GET   /liveview.mjpeg          MJPEG stream, ~24 fps

GET   /api/settings/schema     what is settable right now, with legal values
GET   /api/settings            current values
POST  /api/settings            write, read back, report what was ignored

GET   /api/focus               focus state
POST  /api/focus               drive the motor      {"position": N}
GET   /api/focus/bounds        coordinate space and options
POST  /api/focus/mode          mode / subject / area / point / frame size

POST  /api/record/start|stop   recording → the camera's card
GET   /api/record/movies       what is on the card
GET   /api/record/download     pull index 0 back, ~56 MB/s
POST  /api/record/clear        release a database entry

POST  /api/capture             shoot and download

POST  /api/release             give the body its controls back
POST  /api/acquire             take them again, restoring settings

WS    /ws                      live state + commands

      /api/probe/*  /api/dump/*    research only
```

- [Status](#status) · [Live view](#live-view) · [Settings](#settings) ·
  [Focus](#focus) · [Recording](#recording) · [Stills](#stills) ·
  [Handing the camera back](#handing-the-camera-back) ·
  [WebSocket](#websocket) · [Research endpoints](#research-endpoints)

---

## Status

### `GET /api/status`

Never blocks on the camera — it answers from cached state, so it still works
when a PTP call is wedged.

```json
{
  "connected": true,
  "stuck": null,
  "capture_step": null,
  "movie_progress": null,
  "camera_status": {
    "media_free_space": 19126,
    "battery_state": 1,
    "lens_focal_mm": 40.0
  },
  "released": false,
  "camera_mode": "movie",
  "recording": false,
  "focus_position": 8950,
  "focus_state": 0,
  "focal_length_mm": 40.0,
  "focus_range": [5974, 11116],
  "ws_clients": 1
}
```

`stuck` names the operation that stopped answering. `capture_step` and
`movie_progress` report where a long operation has got to.

`focus_range` is read from the mounted lens; it is not a constant.

---

## Live view

### `GET /liveview.mjpeg`

`multipart/x-mixed-replace` JPEG stream. Point an `<img>` at it.

About 24 fps, ~500 KB per frame. Frames are only fetched from the camera while
someone is listening.

**It is one long-lived connection.** Restart the bridge and it dies; the
consumer has to notice and reconnect, because an `<img>` that already has a
`src` will sit there blank forever otherwise.

---

## Settings

### `GET /api/settings/schema`

Metadata for every setting: kind, unit, current legal values, whether it is
settable right now. Re-reads the camera's capabilities first, because they move
with body mode and recording format.

```json
{
  "settings": [
    {
      "name": "color_mode",
      "kind": "enum",
      "group": 3,
      "writable": true,
      "note": "",
      "choices": ["Standard", "Vivid", "TealAndOrange", "..."]
    },
    {
      "name": "electronic_stabilization",
      "writable": false,
      "note": "（相機在目前的格式／模式下不開放調整）",
      "choices": ["On", "Off"]
    }
  ],
  "capabilities": {"iso": {"min": 100, "max": 25600}},
  "camera_mode": "movie",
  "shutter_unit": 2
}
```

`writable: false` with choices present means the camera declared the field
unsettable in the current combination — see
[PROTOCOL.md](PROTOCOL.md#what-the-camera-says-it-can-do).

### `GET /api/settings`

Current values, read from the camera.

### `POST /api/settings`

```bash
curl -X POST http://localhost:1025/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"iso": 400, "color_mode": "TealAndOrange", "aperture": 2.8}'
```

Writes are grouped by data group and sent as few times as possible, then read
back and compared:

```json
{
  "ok": false,
  "applied": {"iso": 400},
  "rejected": {
    "iso": {"requested": 400, "actual": 6400,
            "hint": "曝光模式目前是 ProgramAuto，自動曝光會覆蓋手動設的 iso。"}
  },
  "settings": { "...": "everything, after the write" }
}
```

`rejected` is the useful part. The camera accepts writes it then ignores; this
is how you find out. See
[the list of cases](GOTCHAS.md#5-writes-that-are-accepted-and-ignored).

Enum values may be given as names or as raw numbers. Numbers matter because the
camera's list of legal values is sometimes larger than any library's enum.

Notable names — the full table is in `camera_settings.py`:

`exposure_mode` `aperture` `shutter_speed` `shutter_angle` `iso` `iso_auto`
`exposure_compensation` `metering_mode` `white_balance` `color_temp`
`image_quality` `dng_quality` `resolution` `aspect_ratio` `color_mode`
`color_space` `tone_effect` `dest_to_save` `electronic_stabilization`
`dc_crop_mode` `hdr` `fill_light` `loc_distortion` `loc_chromatic_aberration`
`loc_diffraction` `loc_vignetting` `drive_mode` `capture_mode` `record_format`
`movie_resolution` `frame_rate` `audio_record`

`shutter_angle` and `shutter_speed` are the same setting expressed two ways;
pass one. In CINE only the angle takes effect.

---

## Focus

### `GET /api/focus`

```json
{
  "focus_position": 8950,
  "focus_state": 0,
  "focus_mode": "AF_S",
  "face_eye_af": "Off",
  "face_eye_detected": "NonDetection",
  "focus_area": "OnePointSelection",
  "focus_point": [565, 896],
  "point_size": 1,
  "continuous_af": "Off",
  "af_lock": "Off"
}
```

### `POST /api/focus`

Drive the motor to an absolute position.

```bash
curl -X POST http://localhost:1025/api/focus \
  -H 'Content-Type: application/json' -d '{"position": 8500}'
```

```json
{"ok": true, "position": 8500, "requested": 8500,
 "applied": true, "clamped": false}
```

Out-of-range values are clamped and reported, not refused.

**Repeated calls coalesce.** While a write is in flight, later ones replace each
other and only the newest is sent. Positions are absolute, so dropping the
middle ones changes nothing — which is what makes a continuously-sending slider
viable over a one-transaction bus.

**This forces the camera into manual focus** (see
[the trap](GOTCHAS.md#10-a-position-write-forces-mf)).
`POST /api/focus/mode` is the way back.

### `GET /api/focus/bounds`

The coordinate space and the available options, all from the camera:

```json
{
  "point": {
    "height": 682, "width": 1024,
    "top": 85, "bottom": 597, "left": 96, "right": 928,
    "point_sizes": [[128,128],[64,64],[32,32]],
    "point_step": [32, 16]
  },
  "focus_modes": ["MF", "AF_C", "AF_S"],
  "focus_areas": ["OnePointSelection", "MultiAutoFocusPoints"],
  "face_eye_options": ["Off", "FaceOnly", "FaceEyeAuto"]
}
```

The AF point's reachable range is this valid area **inset by half the current
frame size** — see [PROTOCOL.md](PROTOCOL.md#the-af-point) for the measurements.

### `POST /api/focus/mode`

Focus mode, subject detection, area, point, frame size. Any subset.

```bash
# back to autofocus after using the slider
curl -X POST http://localhost:1025/api/focus/mode \
  -H 'Content-Type: application/json' -d '{"mode": "AF_S"}'

# focus there
curl -X POST http://localhost:1025/api/focus/mode \
  -H 'Content-Type: application/json' -d '{"point": [300, 500]}'
```

| Field | Values |
|---|---|
| `mode` | `MF` `AF_S` `AF_C` |
| `continuous_af` | Pre-AF on/off. Defaults to on for `AF_C` only. |
| `face_eye_af` | `Off` `FaceOnly` `FaceEyeAuto` |
| `focus_area` | `OnePointSelection` `MultiAutoFocusPoints` |
| `point` | `[y, x]` |
| `point_size` | index into `point_sizes` |
| `af_trigger` | default `true` when a point is given |

**Giving a point means "focus there"**, so the endpoint also switches the area
to `OnePointSelection`, turns face/eye detection off, and moves MF back to
`AF_S`. Each of those would otherwise make the point silently do nothing. Pass
the field explicitly to override any of them.

It then triggers an AF drive, because moving the point does not focus by itself.

The response is the state read back after the write.

---

## Recording

Footage is recorded **to the camera's card**. Nothing here writes to your disk
unless you ask for a download.

### `POST /api/record/start`, `POST /api/record/stop`

```json
{"ok": true, "recording": true}
```

`409` if it is already recording.

### `GET /api/record/movies`

What is in the image database:

```json
{"movies": [{"index": 0, "name": "SIGM0001.MOV", "size": 88221350}]}
```

### `GET /api/record/download`

Pulls index 0 to the machine running the bridge, in 1 MB chunks, at around
56 MB/s.

```json
{"ok": true, "saved_to": "/Users/you/.sigma-fp-bridge/movies/SIGM0001.MOV",
 "bytes": 88221350}
```

`?save=0` streams without writing a file.

Refused with `409` while recording, and with `404` when there is nothing at
index 0. Those guards are not politeness: the underlying opcode takes the camera
off the USB bus when misused, and only a power cycle brings it back. See
[the trap](GOTCHAS.md#8-movie-download-has-one-slot).

### `POST /api/record/clear?image_id=N`

Release a database entry. With no `image_id`, releases all.

---

## Stills

### `POST /api/capture`

Shoots and pulls the image back.

```bash
curl -X POST 'http://localhost:1025/api/capture'          # shoot and download
curl -X POST 'http://localhost:1025/api/capture?fetch=0'  # shoot only
curl -X POST 'http://localhost:1025/api/capture?af=1'     # autofocus first
```

```json
{"ok": true, "files": [
  {"name": "SDIM0001.DNG", "bytes": 27262976,
   "saved_to": "/Users/you/.sigma-fp-bridge/photos/SDIM0001.DNG"}
]}
```

DNG, JPEG and DNG+JPEG all work. Two files come back for DNG+JPEG.

`dest_to_save` decides whether the card gets a copy as well — `InCamera` and
`Both` write to the card, `InComputer` and `Null` do not. It does not affect
whether you can download the image; all four were tested.

The database entry is released afterwards, without which
[the shutter stops working](GOTCHAS.md#7-an-unreleased-entry-stops-the-shutter).

---

## Handing the camera back

### `POST /api/release`

Sends `CloseApplication` and gives the body its controls back. The bridge keeps
the USB handle, so no live view and no control until you take it again.

Settings are snapshotted first, because taking control resets them.

### `POST /api/acquire`

Takes control and restores the snapshot. A no-op if it is already connected.

Anything changed on the body while released is lost — opening the API resets the
camera and there is no way around it.

---

## WebSocket

`ws://host:1025/ws`. The page uses this for everything that needs to be live.

### Sent by the server

| `type` | When |
|---|---|
| `hello` | on connect — server version, connected flag, focus range |
| `state` | ~10 Hz — connection, mode, recording, focus, lens |
| `settings` | after a settings read |
| `settings_schema` | after `describe_settings` |
| `ack` | result of a command, including `rejected` |
| `error` | with a message |

`state` carries: `connected` `released` `camera_mode` `recording`
`recording_seconds` `focus_position` `focus_state` `focus_mode` `face_eye_af`
`face_eye_detected` `focus_area` `focus_point` `point_size` `continuous_af`
`focal_length_mm` `focus_range` `frame_rate`.

### Sent by the client

| `cmd` | Notes |
|---|---|
| `get_settings` | |
| `describe_settings` | schema |
| `set_settings` | `{"settings": {...}}`, same shape as the HTTP endpoint |
| `set_position` | `{"position": N}` |
| `set_frame_rate` | for shutter-angle conversion |
| `record_start`, `record_stop` | |
| `release`, `acquire` | |
| `capture_status` | camera capture state |
| `set_active_lens` | |

Include `"id"` in a command and the matching `ack` carries it back.

---

## Research endpoints

Not needed to use the bridge. They exist because the alternative loop —
edit code → restart → look — is slow enough that you start guessing instead of
measuring.

### `GET /api/probe/ptp`

Lists the opcodes the library knows.

### `POST /api/probe/ptp`

Sends an arbitrary opcode and hands back the raw bytes.

```bash
curl -X POST http://localhost:1025/api/probe/ptp \
  -H 'Content-Type: application/json' \
  -d '{"opcode": "SigmaGetCamCanSetInfo5", "params": []}'

curl -X POST http://localhost:1025/api/probe/ptp \
  -H 'Content-Type: application/json' \
  -d '{"opcode": "0x902c", "params": []}'
```

Returns `bytes`, `raw_hex`, `ascii` and `uint32_le` — several views, because you
do not know which one will show the structure. Names or hex both work, which is
what makes scanning undocumented opcodes possible.

> Sending unknown opcodes can leave the camera in a state only a power cycle
> clears. `SigmaGetPartialMovieFile` during recording is blocked here for that
> reason.

### `GET /api/dump/{which}`

`info5`, `movie` or `pict` — the raw IFD as JSON, parsed by this repo's own
parser rather than the library's.

### `POST /api/probe/movie`

Writes one DataGroupMovie tag and reports the whole group before and after. This
is how the audio tags were identified: with no documentation, "write it and see
what moves" is the only way, and body-side changes are erased by `ConfigApi`.

### `GET /api/probe/events`

Drains the PTP event queue. Sends nothing to the camera, so it is safe during
recording.

This body appears never to emit events — verified with a stills capture as a
control.
