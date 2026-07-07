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

  def test_load_files_not_a_list_returns_none(self, tmp_path):
    path = self.write_config(tmp_path, {
      "ssid": "HomeNet", "host": "1.2.3.4", "user": "ubuntu", "remote_root": "/srv/dashcam",
      "files": "fcamera.hevc",
    })
    assert dcu.UploadConfig.load(path) is None

  def test_load_missing_required_key_returns_none(self, tmp_path):
    assert dcu.UploadConfig.load(self.write_config(tmp_path, {"ssid": "HomeNet"})) is None


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
  def test_ssid_unavailable_returns_none(self):
    # on PC / when wpa_supplicant socket is missing, the import-and-query must not raise
    ssid = dcu.get_current_ssid()
    assert ssid is None or isinstance(ssid, str)


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
