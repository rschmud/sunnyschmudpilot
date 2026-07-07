#!/usr/bin/env python3
"""Upload dashcam footage to a personal server while parked on a configured wifi network.

Offroad-only (see system/manager/process_config.py). Configuration lives in
/data/dashcam_upload/config.json so it survives openpilot reinstalls; if the config is
missing or invalid the daemon idles. Uploaded segments are marked with an xattr on the
segment directory. See tools/dashcam/README.md for setup.
"""
import json
import os
from dataclasses import dataclass, field

from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr, setxattr

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
      files = raw.get("files", DEFAULT_FILES)
      if not isinstance(files, list):
        raise TypeError(f"files must be a list, got {type(files).__name__}")
      return cls(ssid=raw["ssid"], host=raw["host"], user=raw["user"], remote_root=raw["remote_root"],
                 port=int(raw.get("port", 22)), files=list(files))
    except FileNotFoundError:
      return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
      cloudlog.exception("dashcam_uploader: invalid config, idling")
      return None


def ssh_command(cfg: UploadConfig) -> list[str]:
  return ["ssh", "-i", SSH_KEY_PATH, "-p", str(cfg.port),
          "-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}",
          "-o", "BatchMode=yes",
          "-o", "ConnectTimeout=10"]


def _remote_dir(cfg: UploadConfig, logdir: str) -> str:
  return f"{cfg.remote_root}/incoming/{logdir}"


def rsync_command(cfg: UploadConfig, local_files: list[str], logdir: str) -> list[str]:
  remote_dir = _remote_dir(cfg, logdir)
  return ["rsync", "-t", "--partial", "--timeout=30",
          "--rsync-path", f"mkdir -p '{remote_dir}' && rsync",
          "-e", " ".join(ssh_command(cfg)),
          *local_files,
          f"{cfg.user}@{cfg.host}:{remote_dir}/"]


def complete_marker_command(cfg: UploadConfig, logdir: str) -> list[str]:
  return [*ssh_command(cfg), f"{cfg.user}@{cfg.host}", f"touch '{_remote_dir(cfg, logdir)}/.complete'"]


def get_current_ssid() -> str | None:
  try:
    from openpilot.system.hardware.tici.hardware import wpa_supplicant_cmd  # lazy: TICI-only module
    return wpa_supplicant_cmd("STATUS").get("ssid") or None
  except Exception:
    return None


def find_pending_segments(root: str, files: list[str]) -> list[tuple[str, list[str]]]:
  """(logdir, [absolute paths of wanted files present]) for un-uploaded segments, oldest first."""
  pending = []
  for logdir in listdir_by_creation(root):
    if "--" not in logdir:  # boot/, crash/, etc.
      continue
    path = os.path.join(root, logdir)
    try:
      if getxattr(path, UPLOAD_ATTR_NAME) == UPLOAD_ATTR_VALUE:
        continue
    except OSError:
      continue  # deleter may have removed the segment mid-walk
    present = [os.path.join(path, f) for f in files if os.path.exists(os.path.join(path, f))]
    if present:
      pending.append((logdir, present))
  return pending
