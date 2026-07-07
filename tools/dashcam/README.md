# Dashcam auto-upload

Uploads `fcamera.hevc`, `ecamera.hevc`, and the qlog for every drive segment to a
personal server over rsync/SSH, whenever the device is offroad and connected to the
configured wifi network. Daemon: `system/loggerd/dashcam_uploader.py` (managed process
`dashcam_uploader`, offroad-only). Segments already sent are marked with the
`user.dashcam_upload` xattr and skipped.

## Setup

1. On the server: create the landing directory, owned by the SSH user:
   `sudo mkdir -p /srv/dashcam/incoming && sudo chown $USER /srv/dashcam -R`
2. On the device (`ssh comma@<device-ip>`):
   `cd /data/openpilot/tools/dashcam && ./device_setup.sh <server_host> [port]`
3. Append the printed public key to `~/.ssh/authorized_keys` on the server.
4. Edit `/data/dashcam_upload/config.json` on the device: set `ssid` (exact wifi
   name) and `user` (server SSH user).
5. Reboot the device (or restart openpilot).

## Smoke test

1. With the car off and the device on the configured wifi, wait ~1 minute.
2. On the server: `ls /srv/dashcam/incoming/` — segment directories should appear,
   each eventually containing the video files, the qlog, and a `.complete` marker.
3. On the device, confirm no errors: `grep dashcam_uploader /data/log/swaglog* | tail`
4. Uploaded segments are marked: re-listing should show no re-uploads on later cycles.

## Config reference (`/data/dashcam_upload/config.json`)

| Key | Meaning | Default |
|---|---|---|
| `ssid` | Exact wifi network name that permits uploading | required |
| `host` | Server hostname or IP | required |
| `user` | SSH user on the server | required |
| `remote_root` | Server directory; files land in `<remote_root>/incoming/` | required |
| `port` | SSH port | 22 |
| `files` | Per-segment files to upload (missing ones skipped) | fcamera, ecamera, qlog(.zst) |

Delete `/data/dashcam_upload/config.json` to disable uploading; the daemon idles
without it. Local disk cleanup is unchanged (openpilot's `deleter` still rotates).
