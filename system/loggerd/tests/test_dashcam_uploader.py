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
