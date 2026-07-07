# Dashcam Device Uploader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An offroad-only daemon in this sunnypilot fork that rsyncs dashcam footage (fcamera.hevc, ecamera.hevc, qlog) to the user's Oracle server whenever the device is parked on the configured home wifi.

**Architecture:** One new module `system/loggerd/dashcam_uploader.py` registered in the process manager with the existing `only_offroad` predicate. Every 30 s it reads `/data/dashcam_upload/config.json`, compares the connected SSID (wpa_supplicant) against the configured one, and pushes not-yet-uploaded segments via `rsync --partial` over SSH with a dedicated key. Uploaded segments are marked with an xattr on the segment directory (same mechanism as the stock uploader). A `.complete` marker is touched remotely after each segment so the future server app knows ingestion is safe.

**Tech Stack:** Python 3 (openpilot style: 2-space indent), rsync + OpenSSH (both ship with AGNOS), pytest.

**Spec:** `docs/superpowers/specs/2026-07-06-dashcam-upload-design.md`

**Environment note:** This repo's Python tests only run on Linux (the `xattr` module and openpilot's Params bindings are Linux-only). On the Windows dev box, run each test step inside WSL if available; otherwise substitute `python -m py_compile <files>` locally and run the pytest commands on-device (or in CI) before calling the task verified. Every "Run:" line below assumes a Linux shell at the repo root.

---

## File map

| File | Action | Responsibility |
|---|---|---|
| `system/loggerd/dashcam_uploader.py` | Create | Config loading, SSID check, segment discovery, rsync/ssh invocation, main loop, SIGTERM handling |
| `system/loggerd/tests/test_dashcam_uploader.py` | Create | Unit tests for all of the above (subprocess and xattr mocked) |
| `system/manager/process_config.py` | Modify | Register the daemon (offroad-only, device-only) |
| `tools/dashcam/device_setup.sh` | Create | One-time on-device setup: keypair, config template, known_hosts |
| `tools/dashcam/README.md` | Create | Setup + smoke-test instructions |

---

### Task 1: Module skeleton with config loading

**Files:**
- Create: `system/loggerd/dashcam_uploader.py`
- Create: `system/loggerd/tests/test_dashcam_uploader.py`

- [ ] **Step 1: Write the failing tests**

Create `system/loggerd/tests/test_dashcam_uploader.py`:

```python
import json

import openpilot.system.loggerd.dashcam_uploader as dcu


class TestUploadConfig:
  def write_config(self, tmp_path, data) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data) if isinstance(data, dict) else data)
    return str(p)

  def test_load_valid_config(self, tmp_path):
    path = self.write_config(tmp_path, {
      "ssid": "HomeNet", "host": "1.2.3.4", "user": "ubuntu", "remote_root": "/srv/dashcam",
    })
    cfg = dcu.UploadConfig.load(path)
    assert cfg is not None
    assert cfg.ssid == "HomeNet"
    assert cfg.host == "1.2.3.4"
    assert cfg.user == "ubuntu"
    assert cfg.remote_root == "/srv/dashcam"
    assert cfg.port == 22
    assert cfg.files == dcu.DEFAULT_FILES

  def test_load_overrides(self, tmp_path):
    path = self.write_config(tmp_path, {
      "ssid": "HomeNet", "host": "1.2.3.4", "user": "ubuntu", "remote_root": "/srv/dashcam",
      "port": 2222, "files": ["fcamera.hevc"],
    })
    cfg = dcu.UploadConfig.load(path)
    assert cfg.port == 2222
    assert cfg.files == ["fcamera.hevc"]

  def test_load_missing_file_returns_none(self, tmp_path):
    assert dcu.UploadConfig.load(str(tmp_path / "nope.json")) is None

  def test_load_malformed_json_returns_none(self, tmp_path):
    assert dcu.UploadConfig.load(self.write_config(tmp_path, "{not json")) is None

  def test_load_missing_required_key_returns_none(self, tmp_path):
    assert dcu.UploadConfig.load(self.write_config(tmp_path, {"ssid": "HomeNet"})) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'openpilot.system.loggerd.dashcam_uploader'`

- [ ] **Step 3: Write the module with config loading**

Create `system/loggerd/dashcam_uploader.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add system/loggerd/dashcam_uploader.py system/loggerd/tests/test_dashcam_uploader.py
git commit -m "feat(dashcam): config loading for dashcam uploader daemon"
```

---

### Task 2: SSH/rsync command builders and SSID lookup

**Files:**
- Modify: `system/loggerd/dashcam_uploader.py`
- Modify: `system/loggerd/tests/test_dashcam_uploader.py`

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
CFG = dcu.UploadConfig(ssid="HomeNet", host="1.2.3.4", user="ubuntu", remote_root="/srv/dashcam", port=2222)


class TestCommands:
  def test_ssh_command_uses_dedicated_identity(self):
    cmd = dcu.ssh_command(CFG)
    assert cmd[0] == "ssh"
    assert dcu.SSH_KEY_PATH in cmd
    assert "-p" in cmd and "2222" in cmd
    assert f"UserKnownHostsFile={dcu.KNOWN_HOSTS_PATH}" in cmd
    assert "BatchMode=yes" in cmd  # never hang on a password prompt

  def test_rsync_command(self):
    files = ["/data/media/0/realdata/00000004--0ac3964c96--3/fcamera.hevc"]
    cmd = dcu.rsync_command(CFG, files, "00000004--0ac3964c96--3")
    assert cmd[0] == "rsync"
    assert "--partial" in cmd
    assert files[0] in cmd
    assert cmd[-1] == "ubuntu@1.2.3.4:/srv/dashcam/incoming/00000004--0ac3964c96--3/"
    # remote dir is created by rsync itself (no separate ssh round-trip)
    rsync_path = cmd[cmd.index("--rsync-path") + 1]
    assert "mkdir -p '/srv/dashcam/incoming/00000004--0ac3964c96--3'" in rsync_path

  def test_complete_marker_command(self):
    cmd = dcu.complete_marker_command(CFG, "00000004--0ac3964c96--3")
    assert cmd[0] == "ssh"
    assert cmd[-2] == "ubuntu@1.2.3.4"
    assert cmd[-1] == "touch '/srv/dashcam/incoming/00000004--0ac3964c96--3/.complete'"


class TestSsid:
  def test_ssid_unavailable_returns_none(self, monkeypatch):
    # on PC / when wpa_supplicant socket is missing, the import-and-query must not raise
    assert dcu.get_current_ssid() is None or isinstance(dcu.get_current_ssid(), str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v -k "Commands or Ssid"`
Expected: FAIL — `AttributeError: ... has no attribute 'ssh_command'`

- [ ] **Step 3: Implement** (append to `dashcam_uploader.py`)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add system/loggerd/dashcam_uploader.py system/loggerd/tests/test_dashcam_uploader.py
git commit -m "feat(dashcam): rsync/ssh command builders and SSID lookup"
```

---

### Task 3: Segment discovery

**Files:**
- Modify: `system/loggerd/dashcam_uploader.py`
- Modify: `system/loggerd/tests/test_dashcam_uploader.py`

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
class TestFindPendingSegments:
  def make_segment(self, root, name, files):
    d = root / name
    d.mkdir(parents=True)
    for f in files:
      (d / f).write_bytes(b"x")
    return d

  def patch_xattrs(self, monkeypatch, uploaded: set[str]):
    monkeypatch.setattr(dcu, "getxattr",
                        lambda path, attr: dcu.UPLOAD_ATTR_VALUE if path in uploaded else None)

  def test_pending_oldest_first_with_present_files_only(self, tmp_path, monkeypatch):
    self.patch_xattrs(monkeypatch, set())
    self.make_segment(tmp_path, "00000004--0ac3964c96--10", ["fcamera.hevc", "qlog.zst"])
    self.make_segment(tmp_path, "00000004--0ac3964c96--2", ["fcamera.hevc", "ecamera.hevc", "qlog.zst"])
    pending = dcu.find_pending_segments(str(tmp_path), dcu.DEFAULT_FILES)
    assert [p[0] for p in pending] == ["00000004--0ac3964c96--2", "00000004--0ac3964c96--10"]
    seg2_files = [f.rsplit("/", 1)[-1] for f in pending[0][1]]
    assert seg2_files == ["fcamera.hevc", "ecamera.hevc", "qlog.zst"]  # only files that exist

  def test_uploaded_segments_skipped(self, tmp_path, monkeypatch):
    d = self.make_segment(tmp_path, "00000004--0ac3964c96--2", ["fcamera.hevc"])
    self.patch_xattrs(monkeypatch, {str(d)})
    assert dcu.find_pending_segments(str(tmp_path), dcu.DEFAULT_FILES) == []

  def test_non_segment_dirs_and_empty_segments_skipped(self, tmp_path, monkeypatch):
    self.patch_xattrs(monkeypatch, set())
    self.make_segment(tmp_path, "boot", ["2024-01-01--00-00-00.zst"])
    self.make_segment(tmp_path, "00000004--0ac3964c96--3", ["rlog"])  # none of the wanted files
    assert dcu.find_pending_segments(str(tmp_path), dcu.DEFAULT_FILES) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v -k FindPending`
Expected: FAIL — `AttributeError: ... has no attribute 'find_pending_segments'`

- [ ] **Step 3: Implement** (append to `dashcam_uploader.py`; add imports `import os` at top and `from openpilot.system.loggerd.uploader import listdir_by_creation` plus `from openpilot.system.loggerd.xattr_cache import getxattr, setxattr` below the existing imports)

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add system/loggerd/dashcam_uploader.py system/loggerd/tests/test_dashcam_uploader.py
git commit -m "feat(dashcam): pending-segment discovery with xattr tracking"
```

---

### Task 4: Subprocess execution, segment upload, SIGTERM safety

**Files:**
- Modify: `system/loggerd/dashcam_uploader.py`
- Modify: `system/loggerd/tests/test_dashcam_uploader.py`

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
class TestUploadSegment:
  def test_success_runs_rsync_then_marker(self, monkeypatch):
    calls = []
    monkeypatch.setattr(dcu, "_run_command", lambda cmd, timeout: calls.append(cmd[0]) or True)
    assert dcu.upload_segment(CFG, "00000004--0ac3964c96--2", ["/x/fcamera.hevc"]) is True
    assert calls == ["rsync", "ssh"]

  def test_rsync_failure_skips_marker(self, monkeypatch):
    calls = []
    monkeypatch.setattr(dcu, "_run_command", lambda cmd, timeout: calls.append(cmd[0]) and False)
    assert dcu.upload_segment(CFG, "00000004--0ac3964c96--2", ["/x/fcamera.hevc"]) is False
    assert calls == ["rsync"]


class TestRunCommand:
  def test_run_command_success(self):
    assert dcu._run_command(["true"], timeout=5) is True

  def test_run_command_failure(self):
    assert dcu._run_command(["false"], timeout=5) is False

  def test_sigterm_handler_sets_exit_and_kills_child(self, monkeypatch):
    class FakeChild:
      terminated = False
      def terminate(self):
        self.terminated = True
    child = FakeChild()
    monkeypatch.setattr(dcu, "_child", child)
    dcu._handle_sigterm(15, None)
    assert dcu._exit_event.is_set()
    assert child.terminated
    dcu._exit_event.clear()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v -k "UploadSegment or RunCommand"`
Expected: FAIL — `AttributeError: ... has no attribute 'upload_segment'`

- [ ] **Step 3: Implement** (append to `dashcam_uploader.py`; add `import subprocess` and `import threading` at top)

```python
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
    cloudlog.warning(f"dashcam_uploader: {cmd[0]} timed out after {timeout}s")
    return False
  except OSError:
    cloudlog.exception("dashcam_uploader: failed to spawn command")
    return False
  finally:
    _child = None


def _handle_sigterm(signum, frame) -> None:
  _exit_event.set()
  child = _child
  if child is not None:
    child.terminate()


def upload_segment(cfg: UploadConfig, logdir: str, local_files: list[str]) -> bool:
  if not _run_command(rsync_command(cfg, local_files, logdir), RSYNC_TIMEOUT_SECONDS):
    return False
  return _run_command(complete_marker_command(cfg, logdir), SSH_TIMEOUT_SECONDS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add system/loggerd/dashcam_uploader.py system/loggerd/tests/test_dashcam_uploader.py
git commit -m "feat(dashcam): segment upload with SIGTERM-safe subprocess handling"
```

---

### Task 5: Main loop

**Files:**
- Modify: `system/loggerd/dashcam_uploader.py`
- Modify: `system/loggerd/tests/test_dashcam_uploader.py`

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
class TestRunCycle:
  def setup_cycle(self, monkeypatch, tmp_path, ssid="HomeNet", cfg=CFG,
                  pending=None, upload_ok=True):
    marked, uploads = [], []
    monkeypatch.setattr(dcu.UploadConfig, "load", classmethod(lambda cls, path=None: cfg))
    monkeypatch.setattr(dcu, "get_current_ssid", lambda: ssid)
    monkeypatch.setattr(dcu, "find_pending_segments", lambda root, files: pending or [])
    monkeypatch.setattr(dcu, "upload_segment", lambda c, ld, lf: uploads.append(ld) or upload_ok)
    monkeypatch.setattr(dcu, "setxattr", lambda path, attr, val: marked.append(path))
    return uploads, marked

  def test_uploads_and_marks_pending_segments(self, monkeypatch, tmp_path):
    pending = [("seg--1", ["/r/seg--1/fcamera.hevc"]), ("seg--2", ["/r/seg--2/fcamera.hevc"])]
    uploads, marked = self.setup_cycle(monkeypatch, tmp_path, pending=pending)
    dcu.run_cycle("/r")
    assert uploads == ["seg--1", "seg--2"]
    assert marked == ["/r/seg--1", "/r/seg--2"]

  def test_wrong_ssid_does_nothing(self, monkeypatch, tmp_path):
    uploads, _ = self.setup_cycle(monkeypatch, tmp_path, ssid="CoffeeShop",
                                  pending=[("seg--1", ["/r/seg--1/fcamera.hevc"])])
    dcu.run_cycle("/r")
    assert uploads == []

  def test_no_config_does_nothing(self, monkeypatch, tmp_path):
    uploads, _ = self.setup_cycle(monkeypatch, tmp_path, cfg=None,
                                  pending=[("seg--1", ["/r/seg--1/fcamera.hevc"])])
    dcu.run_cycle("/r")
    assert uploads == []

  def test_failed_upload_stops_cycle_and_marks_nothing(self, monkeypatch, tmp_path):
    pending = [("seg--1", ["/r/seg--1/fcamera.hevc"]), ("seg--2", ["/r/seg--2/fcamera.hevc"])]
    uploads, marked = self.setup_cycle(monkeypatch, tmp_path, pending=pending, upload_ok=False)
    dcu.run_cycle("/r")
    assert uploads == ["seg--1"]  # gave up after first failure, retry next cycle
    assert marked == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v -k RunCycle`
Expected: FAIL — `AttributeError: ... has no attribute 'run_cycle'`

- [ ] **Step 3: Implement** (append to `dashcam_uploader.py`; add `import signal` at top and `from openpilot.system.hardware.hw import Paths` below the existing imports)

```python
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
      return  # network/server problem or shutdown: retry from scratch next cycle
    try:
      setxattr(os.path.join(root, logdir), UPLOAD_ATTR_NAME, UPLOAD_ATTR_VALUE)
    except OSError:
      cloudlog.exception("dashcam_uploader: setxattr failed")


def main(exit_event: threading.Event | None = None) -> None:
  global _exit_event
  if exit_event is not None:
    _exit_event = exit_event
  signal.signal(signal.SIGTERM, _handle_sigterm)

  cloudlog.info("dashcam_uploader starting")
  while not _exit_event.is_set():
    run_cycle(Paths.log_root())
    _exit_event.wait(CYCLE_SECONDS)


if __name__ == "__main__":
  main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add system/loggerd/dashcam_uploader.py system/loggerd/tests/test_dashcam_uploader.py
git commit -m "feat(dashcam): main loop for dashcam uploader daemon"
```

---

### Task 6: Register the daemon in the process manager

**Files:**
- Modify: `system/manager/process_config.py` (procs list, next to the stock uploader entry at ~line 154)

- [ ] **Step 1: Add the process entry**

In `system/manager/process_config.py`, directly below the line
`PythonProcess("uploader", "system.loggerd.uploader", uploader_ready),` add:

```python
  PythonProcess("dashcam_uploader", "system.loggerd.dashcam_uploader", only_offroad, enabled=TICI),
```

`only_offroad` already exists in this file; `TICI` is already imported. `enabled=TICI` keeps it off PCs entirely.

- [ ] **Step 2: Verify the config still parses and the process is registered**

Run: `python -c "from openpilot.system.manager.process_config import managed_processes; p = managed_processes['dashcam_uploader']; print(p.name, p.module, p.enabled)"`
Expected: `dashcam_uploader system.loggerd.dashcam_uploader False` on PC (False because not TICI), no traceback. (On Windows, where openpilot imports may not work at all, verify on-device or in WSL.)

- [ ] **Step 3: Commit**

```bash
git add system/manager/process_config.py
git commit -m "feat(dashcam): register dashcam_uploader as offroad-only process"
```

---

### Task 7: Device setup script and README

**Files:**
- Create: `tools/dashcam/device_setup.sh`
- Create: `tools/dashcam/README.md`

- [ ] **Step 1: Write the setup script**

Create `tools/dashcam/device_setup.sh`:

```bash
#!/usr/bin/env bash
# One-time setup for the dashcam uploader. Run ON the comma device:
#   ./device_setup.sh <server_host> [ssh_port]
set -euo pipefail

HOST="${1:?usage: device_setup.sh <server_host> [ssh_port]}"
PORT="${2:-22}"
DIR=/data/dashcam_upload

mkdir -p "$DIR"

if [ ! -f "$DIR/id_ed25519" ]; then
  ssh-keygen -t ed25519 -N "" -f "$DIR/id_ed25519" -C "comma-dashcam"
fi

ssh-keyscan -p "$PORT" "$HOST" > "$DIR/known_hosts" 2>/dev/null

if [ ! -f "$DIR/config.json" ]; then
  cat > "$DIR/config.json" <<EOF
{
  "ssid": "CHANGE_ME_WIFI_NAME",
  "host": "$HOST",
  "port": $PORT,
  "user": "CHANGE_ME_SSH_USER",
  "remote_root": "/srv/dashcam",
  "files": ["fcamera.hevc", "ecamera.hevc", "qlog.zst", "qlog"]
}
EOF
  echo "Wrote $DIR/config.json — edit ssid and user before use."
fi

echo ""
echo "Add this public key to the server's ~/.ssh/authorized_keys:"
cat "$DIR/id_ed25519.pub"
```

- [ ] **Step 2: Write the README**

Create `tools/dashcam/README.md`:

```markdown
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
```

- [ ] **Step 3: Make the script executable and verify it parses**

Run: `chmod +x tools/dashcam/device_setup.sh && bash -n tools/dashcam/device_setup.sh && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tools/dashcam/device_setup.sh tools/dashcam/README.md
git commit -m "docs(dashcam): device setup script and README"
```

---

### Task 8: Full-suite check and on-device verification

- [ ] **Step 1: Run the whole new test file plus the neighboring loggerd tests**

Run: `pytest system/loggerd/tests/test_dashcam_uploader.py system/loggerd/tests/test_uploader.py -v`
Expected: all PASS (proves no accidental breakage of the stock uploader module we import from)

- [ ] **Step 2: Lint**

Run: `ruff check system/loggerd/dashcam_uploader.py system/loggerd/tests/test_dashcam_uploader.py system/manager/process_config.py`
Expected: no errors

- [ ] **Step 3: On-device smoke test**

Follow `tools/dashcam/README.md` "Smoke test" with the user's real SSID/host values (obtain from user: wifi SSID, Oracle host, SSH user). This is the final acceptance gate — files visible in `incoming/` with `.complete` markers, no swaglog errors, no re-upload on subsequent cycles.

- [ ] **Step 4: Push to fork**

```bash
GIT_LFS_SKIP_PUSH=1 git push origin Schmud
```

(LFS skip required: `.lfsconfig` points at sunnypilot's GitLab, which we can't auth to; our commits contain no LFS objects.)
