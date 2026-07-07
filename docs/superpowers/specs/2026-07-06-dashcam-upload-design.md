# Dashcam Footage Auto-Upload + Web Viewer — Design

**Date:** 2026-07-06
**Status:** Approved

## Goal

When the car is parked at home (offroad + connected to a specific wifi SSID), the comma
device automatically uploads dashcam footage to the user's Oracle Cloud server. A
web-accessible UI on that server (behind a Cloudflare Tunnel) lets the user browse, play,
download, and delete footage. The server keeps a 30 GB rolling window, deleting the
oldest drives first.

Two sub-projects, built in order:

1. **Device uploader** — new process in this sunnypilot fork.
2. **Server app** — new Docker Compose service on the Oracle server.

## Decisions made during brainstorming

| Decision | Choice |
|---|---|
| Files uploaded per segment | `fcamera.hevc` (narrow), `ecamera.hevc` (wide), `qlog.zst` |
| Upload trigger | Offroad AND connected to configured SSID (device auto-joins known networks, so "in range" ≡ "connected") |
| Transport | `rsync --partial` over SSH, device pushes (device is behind home NAT; server cannot pull) |
| Auth | Dedicated ed25519 keypair generated for the device; public key added to the Oracle server's `authorized_keys`. The user's existing PC key (available to VS Code) is used only to bootstrap that setup. |
| Retention | Server-side: ingest worker trims library to 30 GB, oldest drive first (~5 hours of driving at ~100 MB/min for both cameras) |
| Web access | New Cloudflare Tunnel hostname → app on localhost; simple username/password login in the app (Cloudflare Access may be layered on top, matching the user's existing services) |
| UI scope (v1) | Drive list, in-browser playback with narrow/wide toggle, speed/GPS from qlog, download, delete, storage meter. No map view in v1. |
| Server deployment | Docker Compose service, matching existing services on the box |
| Device disk rotation | Unchanged — existing `system/loggerd/deleter.py` keeps managing local storage |

## Sub-project 1: Device uploader (fork change)

### New file: `system/loggerd/dashcam_uploader.py`

A daemon registered in `system/manager/process_config.py` as an **offroad-only** Python
process. The process manager starts it when the car turns off and SIGTERMs it when
onroad — this alone enforces "parked only." The SIGTERM handler must also terminate any
in-flight rsync child process.

### Main loop (every ~30 s)

1. Read the currently connected SSID (wpa_supplicant `STATUS`, as
   `system/hardware/tici/hardware.py` already does). No match → sleep.
2. Walk `Paths.log_root()` (`/data/media/0/realdata`). Because the daemon only runs
   offroad, loggerd is not recording and every segment on disk is complete. For each
   segment dir not marked uploaded:
   - `rsync --partial --timeout` each configured file to
     `<remote_root>/incoming/<route>/<segment>/` on the server.
   - Missing files (e.g. a camera didn't record) are skipped, not errors.
   - When all present files have transferred, create a `.complete` marker file in the
     remote segment dir (SSH `touch`), then mark the local segment uploaded via xattr
     (reusing `system/loggerd/xattr_cache.py`, same pattern as the stock uploader).
3. Any failure (wifi drop, server unreachable, rsync nonzero) → log and retry on the
   next cycle. `--partial` resumes interrupted files.

### Configuration: `/data/dashcam_upload/config.json`

Lives outside the openpilot install so it survives reinstalls/branch switches. Fields:

```json
{
  "ssid": "<home network name>",
  "host": "<oracle server IP or hostname>",
  "port": 22,
  "user": "<ssh user>",
  "remote_root": "/srv/dashcam",
  "files": ["fcamera.hevc", "ecamera.hevc", "qlog.zst"]
}
```

Missing or malformed config → daemon logs once and idles (never crashes the manager).

The SSH identity lives at `/data/dashcam_upload/id_ed25519` with a pinned
`known_hosts` beside it. rsync is invoked with
`-e "ssh -i <key> -o UserKnownHostsFile=<known_hosts> -o BatchMode=yes"`.

### Setup steps (one-time, scripted or manual)

1. Generate keypair on device; append public key to server user's `authorized_keys`
   (bootstrapped from the PC over the user's existing SSH access).
2. Write `config.json` with real values (SSID, host, user).
3. Create `/srv/dashcam/incoming` on the server, owned by the SSH user.

## Sub-project 2: Server app (Oracle, Docker Compose)

One container: Python (FastAPI) + ffmpeg + static single-page frontend. Host directory
`/srv/dashcam/` mounted into the container; `cloudflared` (already running on the box)
gets a new tunnel hostname routed to the app's localhost port.

Directory layout on the host:

```
/srv/dashcam/
  incoming/<route>/<segment>/   # rsync lands here (plain SSH to host, no container)
  library/<route>/<segment>/    # served by the app after ingest
```

### Ingest worker (background thread in the app)

Polls `incoming/` for segment dirs containing `.complete`:

1. Remux each `.hevc` → `.mp4` with `ffmpeg -c copy` (lossless container change,
   near-instant; raw `.hevc` is not browser-playable, HEVC-in-mp4 is).
2. Parse `qlog.zst` → `telemetry.json` (timestamped GPS trace + speed curve). Requires
   the openpilot log schema (capnp) vendored into the image. Parse failure is non-fatal:
   segment is kept, telemetry shows as unavailable.
3. Move results to `library/<route>/<segment>/`, delete the raw `.hevc` (the mp4 holds
   the identical stream) and the `incoming` dir.
4. Trim `library/` to 30 GB: delete whole oldest routes until under the cap.

### API + UI

- Login: username/password (bcrypt hash in app config), session cookie. All other
  routes require the session.
- Drive list: segments grouped by route, grouped by date; shows duration, size,
  thumbnail (first-frame extract at ingest).
- Drive view: video player streaming mp4 with HTTP range support, narrow/wide camera
  toggle, sequential playback across segments, speed chart from telemetry, per-file
  download links, delete-drive button.
- Storage meter: library size vs 30 GB cap.

### Known risk: HEVC browser support

HEVC-in-mp4 plays in Safari and in Chrome/Edge where hardware decode exists (most
2016+ devices). If the user's browsers can't play it, fallback plan (not built in v1
unless needed): on-demand H.264 720p transcode endpoint.

## Error handling summary

| Failure | Behavior |
|---|---|
| Wifi drops mid-upload | rsync exits nonzero; `--partial` keeps partial file; retry next cycle |
| Car started mid-upload | Process manager SIGTERMs daemon; handler kills rsync; server never ingests (no `.complete`) |
| Server full / unreachable | Upload fails, footage stays on device until its normal local rotation |
| Malformed qlog | Telemetry marked unavailable; video still served |
| App container down | rsync still lands in `incoming/` (host SSH); ingest catches up on restart |

## Testing

- **Device:** unit-test segment discovery / xattr marking / config parsing with a fake
  log dir; integration-test rsync against a local SSH target. On-device smoke test:
  park on home wifi, verify files appear in `incoming/` and xattrs set.
- **Server:** unit-test ingest (fixture segment with real short hevc/qlog), retention
  trim, and auth. Manual: play footage through the tunnel URL in the user's browsers
  (this validates the HEVC risk early).

## Out of scope (v1)

- Map view of GPS traces (candidate v2)
- H.264 transcode fallback (only if HEVC playback fails)
- Uploading rlog, dcamera, or qcamera files (config list makes adding trivial)
- Any change to on-device retention/deleter behavior
