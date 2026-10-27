# -*- coding: utf-8 -*-
"""用户设置读写测试：只碰 tmp_path 里的文件，不碰真实用户目录。"""
import json
import logging

from traffic_light.settings import SETTINGS_FILE_NAME, Settings, SettingsStore

LOG = logging.getLogger("settings-test")


def store(tmp_path):
    return SettingsStore(tmp_path / SETTINGS_FILE_NAME, LOG)


def test_missing_file_means_defaults(tmp_path):
    """第一次运行没有文件：无线接管默认开启，一次性说明还没弹过。"""
    settings = store(tmp_path).load()
    assert settings == Settings()
    assert settings.wireless_light is True
    assert settings.wireless_notice_shown is False


def test_save_then_load_roundtrip(tmp_path):
    """关掉开关 → 读回来还是关的（重启保持）。"""
    s = store(tmp_path)
    s.save(Settings(wireless_light=False, wireless_notice_shown=True))
    assert s.load() == Settings(wireless_light=False, wireless_notice_shown=True)


def test_save_creates_missing_directory(tmp_path):
    s = SettingsStore(tmp_path / "deep" / "dir" / SETTINGS_FILE_NAME, LOG)
    assert s.save(Settings(wireless_light=False)) is True
    assert s.load().wireless_light is False


def test_corrupt_json_falls_back_to_defaults(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    path = tmp_path / SETTINGS_FILE_NAME
    path.write_text("{ 不是 json", encoding="utf-8")
    assert store(tmp_path).load() == Settings()
    assert any("读取失败" in r.getMessage() for r in caplog.records)


def test_non_object_json_falls_back_to_defaults(tmp_path):
    (tmp_path / SETTINGS_FILE_NAME).write_text("[1, 2]", encoding="utf-8")
    assert store(tmp_path).load() == Settings()


def test_wrong_field_types_are_ignored(tmp_path):
    """'false' 是字符串不是 bool：不猜，回默认值（默认开启）。"""
    (tmp_path / SETTINGS_FILE_NAME).write_text(
        json.dumps({"wireless_light": "false", "wireless_notice_shown": 1}),
        encoding="utf-8")
    assert store(tmp_path).load() == Settings()


def test_unknown_fields_are_dropped(tmp_path):
    (tmp_path / SETTINGS_FILE_NAME).write_text(
        json.dumps({"wireless_light": False, "将来加的字段": 1}), encoding="utf-8")
    assert store(tmp_path).load() == Settings(wireless_light=False)


def test_save_leaves_no_temp_file_behind(tmp_path):
    s = store(tmp_path)
    s.save(Settings(wireless_light=False))
    assert [p.name for p in tmp_path.iterdir()] == [SETTINGS_FILE_NAME]