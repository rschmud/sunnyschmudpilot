#!/usr/bin/env python3
"""Upload dashcam footage to a personal server while parked on a configured wifi network.

Offroad-only (see system/manager/process_config.py). Configuration lives in
/data/dashcam_upload/config.json so it survives openpilot reinstalls; if the config is
missing or invalid the daemon idles. Uploaded segments are marked with an xattr on the
segment directory. See tools/dashcam/README.md for setup.
"""
import json
from dataclasses import dataclass, field

from openpilot.common.swaglog import cloudlog

CONFIG_DIR = "/data/dashcam_upload"
CONFIG_PATH = CONFIG_DIR + "/config.json"
SSH_KEY_PATH = CONFIG_DIR + "/id_ed25519"
KNOWN_HOSTS_PATH = CONFIG_DIR + "/known_hosts"

UPLOAD_ATTR_NAME = 'user.dashcam_upload'
UPLOAD_ATTR_VALUE = b'1'

# qlog vs qlog.zst depends on loggerd version; list both, missing files are skipped
DEFAULT_FILES = ["fcamera.hevc", "ecamera.hevc", "qlog.zst", "qlog"]

CYCLE_SECONDS = 30
RSYNC_TIMEOUT_SECONDS = 600
SSH_TIMEOUT_SECONDS = 30


@dataclass
class UploadConfig:
  ssid: str
  host: str
  user: str
  remote_root: str
  port: int = 22
  files: list[str] = field(default_factory=lambda: list(DEFAULT_FILES))

  @classmethod
  def load(cls, path: str = CONFIG_PATH) -> "UploadConfig | None":
    try:
      with open(path) as f:
        raw = json.load(f)
      return cls(ssid=raw["ssid"], host=raw["host"], user=raw["user"], remote_root=raw["remote_root"],
                 port=int(raw.get("port", 22)), files=list(raw.get("files", DEFAULT_FILES)))
    except FileNotFoundError:
      return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
      cloudlog.exception("dashcam_uploader: invalid config, idling")
      return None
