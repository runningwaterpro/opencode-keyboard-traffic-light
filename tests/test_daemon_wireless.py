# -*- coding: utf-8 -*-
"""无线开关的守护接线测试：回调、存盘、一次性说明，全部用假件。

不启动 daemon.run（它要连服务、建托盘），只测那两段闭包逻辑。
"""
import logging

from traffic_light.core import Light
from traffic_light.daemon import make_wireless_notice, make_wireless_toggle
from traffic_light.notify import NotifyRequest
from traffic_light.settings import Settings

LOG = logging.getLogger("daemon-wireless-test")


class FakeStore:
    def __init__(self):
        self.saved = []

    def save(self, settings):
        self.saved.append(Settings(**vars(settings)))
        return True


class FakeLight:
    """只记录开关，不碰设备。"""

    def __init__(self):
        self.enabled = []

    def set_wireless_enabled(self, enabled):
        self.enabled.append(enabled)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, req):
        self.sent.append(req)


def test_toggle_persists_and_forwards():
    """点一下：新状态存盘、转发给灯口、勾选状态可读回。"""
    settings = Settings()
    store = FakeStore()
    light = FakeLight()
    notices = []
    toggle = make_wireless_toggle(settings, store, light, lambda: notices.append(1))

    toggle(False)
    assert settings.wireless_light is False
    assert store.saved[-1].wireless_light is False
    assert light.enabled == [False]
    assert notices == []                  # 关掉时不弹说明

    toggle(True)
    assert settings.wireless_light is True
    assert store.saved[-1].wireless_light is True
    assert light.enabled == [False, True]
    assert notices == [1]                 # 打开时补一次说明


def test_toggle_survives_light_without_switch():
    """干跑灯口没有 set_wireless_enabled：开关只存盘，不炸。"""
    settings = Settings()
    store = FakeStore()
    toggle = make_wireless_toggle(settings, store, object(), lambda: None)
    toggle(False)
    assert store.saved[-1].wireless_light is False


def test_toggle_saves_before_switching():
    """存盘在切通道之前：切换抛异常也不丢用户的选择。"""
    settings = Settings()
    store = FakeStore()

    class Boom:
        def set_wireless_enabled(self, enabled):
            raise RuntimeError("灯口炸了")

    toggle = make_wireless_toggle(settings, store, Boom(), lambda: None)
    try:
        toggle(False)
    except RuntimeError:
        pass
    assert store.saved[-1].wireless_light is False


def test_notice_is_sent_only_once_and_persisted():
    """「打字时不发帧」只说一次；记进设置后重启也不再弹。"""
    settings = Settings()
    store = FakeStore()
    notifier = FakeNotifier()
    notice = make_wireless_notice(settings, store, notifier)

    notice()
    assert len(notifier.sent) == 1
    assert isinstance(notifier.sent[0], NotifyRequest)
    assert notifier.sent[0].kind == "wireless_tip"
    assert store.saved[-1].wireless_notice_shown is True

    notice()
    assert len(notifier.sent) == 1        # 第二次不再弹

    # 模拟重启：从「存过盘的内容」读回来，仍然不再弹
    restarted = store.saved[-1]
    again = FakeNotifier()
    make_wireless_notice(restarted, FakeStore(), again)()
    assert again.sent == []


def test_notice_does_not_repeat_after_a_failed_send():
    """通知口出错不该让说明反复弹：先记「弹过」再发。"""
    settings = Settings()
    store = FakeStore()

    class Boom:
        def send(self, req):
            raise RuntimeError("通知口挂了")

    notice = make_wireless_notice(settings, store, Boom())
    try:
        notice()
    except RuntimeError:
        pass
    assert settings.wireless_notice_shown is True
    assert store.saved and store.saved[-1].wireless_notice_shown is True


# ---- daemon.run 的接线（用假托盘/假键盘中枢，不连服务、不碰设备） ----

def test_run_wires_persisted_setting_into_hub_and_tray(monkeypatch, tmp_path):
    from traffic_light import daemon, runtime
    from traffic_light.settings import SETTINGS_FILE_NAME, Settings, SettingsStore

    SettingsStore(tmp_path / SETTINGS_FILE_NAME, LOG).save(
        Settings(wireless_light=False))

    monkeypatch.setattr(runtime, "DEFAULT_LOG_DIR", tmp_path)
    monkeypatch.setattr(runtime, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "acquire_single_instance", lambda: True)
    monkeypatch.setattr(daemon, "_discover", lambda *a: None)

    hub_kwargs: dict = {}

    class FakeHub:
        def __init__(self, *args, **kwargs):
            hub_kwargs.update(kwargs)
            hub_kwargs["instance"] = self
            self.enabled = []
            self.stopped = False

        def start(self):
            hub_kwargs["started"] = True

        def stop(self):
            self.stopped = True

        def apply(self, effects):
            pass

        @property
        def open_count(self):
            return 0

        def set_wireless_enabled(self, enabled):
            self.enabled.append(enabled)

    monkeypatch.setattr(daemon, "KeyboardHub", FakeHub)

    tray_kwargs: dict = {}

    class FakeTray:
        def __init__(self, *args, **kwargs):
            tray_kwargs.update(kwargs)
            self.available = True
            self.balloons = []

        def start(self):
            pass

        def close(self):
            pass

        def update(self, *args, **kwargs):
            pass

        def balloon(self, title, body, level="info"):
            self.balloons.append((title, body))

        def set_on_wireless_toggle(self, callback):
            tray_kwargs["toggle"] = callback

    monkeypatch.setattr(daemon.tray_mod, "TrayIcon", FakeTray)

    class FakeNotifier:
        def __init__(self):
            self.sent = []

        def send(self, req):
            self.sent.append(req)

    notifier = FakeNotifier()
    monkeypatch.setattr(daemon.notify_mod, "create_notifier",
                        lambda *a, **k: notifier)

    assert daemon.run(tmp_path / "service.json") == 0

    # 存盘里的「关」必须原样交给两条通道，否则重启后开关就是假的
    assert hub_kwargs["wireless_enabled"] is False
    assert tray_kwargs["wireless_light"] is False
    assert hub_kwargs["started"] is True
    assert callable(tray_kwargs["toggle"])

    # 点开：写盘 + 通知中枢 + 弹一次说明
    tray_kwargs["toggle"](True)
    assert hub_kwargs["instance"].enabled == [True]
    assert SettingsStore(tmp_path / SETTINGS_FILE_NAME, LOG).load().wireless_light is True
    assert [r.kind for r in notifier.sent] == ["wireless_tip"]