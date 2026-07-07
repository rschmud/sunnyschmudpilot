#!/usr/bin/env python3
"""Upload dashcam footage to a personal server while parked on a configured wifi network.

Offroad-only (see system/manager/process_config.py). Configuration lives in
/data/dashcam_upload/config.json so it survives openpilot reinstalls; if the config is
missing or invalid the daemon idles. Uploaded segments are marked with an xattr on the
segment directory. See tools/dashcam/README.md for setup.
"""
import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field

from openpilot.common.swaglog import cloudlog
from openpilot.system.hardware.hw import Paths
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


_config_warned = False


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
    global _config_warned
    try:
      with open(path) as f:
        raw = json.load(f)
      files = raw.get("files", DEFAULT_FILES)
      if not isinstance(files, list):
        raise TypeError(f"files must be a list, got {type(files).__name__}")
      cfg = cls(ssid=raw["ssid"], host=raw["host"], user=raw["user"], remote_root=raw["remote_root"],
                port=int(raw.get("port", 22)), files=list(files))
      _config_warned = False
      return cfg
    except FileNotFoundError:
      if not _config_warned:
        cloudlog.info(f"dashcam_uploader: no config at {path}, idling")
        _config_warned = True
      return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
      if not _config_warned:
        cloudlog.exception("dashcam_uploader: invalid config, idling")
        _config_warned = True
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


_exit_event = threading.Event()
_child: subprocess.Popen | None = None


def _run_command(cmd: list[str], timeout: float) -> bool:
  global _child
  try:
    _child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _, stderr = _child.communicate(timeout=timeout)
    if _child.returncode != 0:
      cloudlog.warning(f"dashcam_uploader: {cmd[0]} failed rc={_child.returncode}: {stderr.decode(errors='replace')[:500]}")
    return _child.returncode == 0
  except subprocess.TimeoutExpired:
    _child.kill()
    _child.wait()
    cloudlog.warning(f"dashcam_uploader: {cmd[0]} timed out after {timeout}s")
    return False
  except OSError:
    cloudlog.exception("dashcam_uploader: failed to spawn command")
    return False
  finally:
    _child = None


def _handle_stop_signal(signum, frame) -> None:
  _exit_event.set()
  child = _child
  if child is not None:
    child.terminate()


def upload_segment(cfg: UploadConfig, logdir: str, local_files: list[str]) -> bool:
  if not _run_command(rsync_command(cfg, local_files, logdir), RSYNC_TIMEOUT_SECONDS):
    return False
  return _run_command(complete_marker_command(cfg, logdir), SSH_TIMEOUT_SECONDS)


def run_cycle(root: str) -> None:
  cfg = UploadConfig.load()
  if cfg is None:
    return
  if get_current_ssid() != cfg.ssid:
    return

  for logdir, local_files in find_pending_segments(root, cfg.files):
    if _exit_event.is_set():
      return
    if not upload_segment(cfg, logdir, local_files):
      # network/server problem or shutdown: retry from scratch next cycle (oldest-first; a permanently
      # failing segment blocks newer ones until deleter rotates it)
      return
    try:
      setxattr(os.path.join(root, logdir), UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
    except OSError:
      cloudlog.exception("dashcam_uploader: setxattr failed")


def main(exit_event: threading.Event | None = None) -> None:
  global _exit_event
  if exit_event is not None:
    _exit_event = exit_event
  # the process manager stops us with SIGINT (see system/manager/process.py); SIGTERM kept for manual kills
  signal.signal(signal.SIGINT, _handle_stop_signal)
  signal.signal(signal.SIGTERM, _handle_stop_signal)

  cloudlog.info("dashcam_uploader starting")
  while not _exit_event.is_set():
    run_cycle(Paths.log_root())
    _exit_event.wait(CYCLE_SECONDS)


if __name__ == "__main__":
  main()
