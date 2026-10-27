# -*- coding: utf-8 -*-
"""多键盘调度测试：假档案 + 假端口，不碰真设备。"""
import logging
import threading
from types import SimpleNamespace

from traffic_light.core import Light, SetKeyLight
from traffic_light.keyboard_hub import KeyboardHub


class FakePort:
    def __init__(self, path, profile):
        self.path = path
        self.profile = profile
        self.state = "waiting"
        self.failure_kind = None
        self.applied = []
        self.started = False
        self.stopped = False

    def apply(self, effects):
        self.applied.extend(effects)

    def start(self):
        self.started = True
        self.state = "open"

    def stop(self):
        self.stopped = True
        self.state = "closed"


class FakeProfile:
    capability = SimpleNamespace(name="fake", verified=True, note="")

    def __init__(self, infos):
        self.infos = list(infos)
        self.mode = "none"
        self.detect_exc = None

    def list_channels(self, devices=None):
        return list(self.infos)

    def detect_mode(self, devices=None):
        if self.detect_exc is not None:
            raise self.detect_exc
        return self.mode


class FakeWirelessPort:
    """无线灯口的替身：只记外部可见的动作。"""

    def __init__(self):
        self.state = "waiting"
        self.applied = []
        self.started = False
        self.stopped = False
        self.failure_kind = None

    def apply(self, effects):
        self.applied.extend(effects)

    def start(self):
        self.started = True
        self.state = "open"

    def stop(self):
        self.stopped = True
        self.state = "closed"


def info(path):
    return SimpleNamespace(path=path)


def make_hub(profile_list, clock=None):
    ports = {}

    def factory(path, profile):
        port = FakePort(path, profile)
        ports[path] = port
        return port

    hub = KeyboardHub(logging.getLogger("test-hub"), profile_list=profile_list,
                      port_factory=factory, clock=clock or (lambda: 0.0))
    return hub, ports


def make_wireless_hub(profile, clock=None, **kwargs):
    """带假无线灯口的 hub；返回 (hub, 有线端口表, 创建过的无线端口列表)。"""
    ports = {}
    wireless = []

    def port_factory(path, prof):
        port = FakePort(path, prof)
        ports[path] = port
        return port

    def wireless_factory():
        port = FakeWirelessPort()
        wireless.append(port)
        return port

    hub = KeyboardHub(logging.getLogger("test-hub"), profile_list=[profile],
                      port_factory=port_factory,
                      wireless_factory=wireless_factory,
                      clock=clock or (lambda: 0.0), **kwargs)
    return hub, ports, wireless


def test_no_devices_no_ports():
    hub, ports = make_hub([FakeProfile([])])
    hub._reconcile(0.0)
    assert ports == {}
    assert hub.open_count == 0


def test_hotplug_adds_port_and_replays_current_lights():
    hub, ports = make_hub([FakeProfile([info("p1")])])
    hub.apply([SetKeyLight("1", Light.RUNNING)])
    hub._reconcile(0.0)
    port = ports["p1"]
    assert port.started
    assert port.applied == [SetKeyLight("1", Light.RUNNING)]
    assert hub.open_count == 1


def test_two_keyboards_both_driven():
    hub, ports = make_hub([FakeProfile([info("p1"), info("p2")])])
    hub._reconcile(0.0)
    assert set(ports) == {"p1", "p2"}
    assert hub.open_count == 2


def test_open_port_survives_enumeration_loss():
    """独占句柄让重建探测必然失败：枚举看不到已持有的路径，不得移除端口。

    回归：曾按「枚举不到 = 拔线」处理，实机上每 4 秒断开重连一次（日志 155 次）。
    真拔线由端口自己的发帧失败/重连逻辑反映，不靠枚举判定。
    """
    profile = FakeProfile([info("p1")])
    hub, ports = make_hub([profile])
    hub._reconcile(0.0)
    port = ports["p1"]
    profile.infos = []  # 模拟 _probe 撞上 share=0，路径从枚举里消失
    for t in (2.0, 4.0, 6.0, 8.0):
        hub._reconcile(t)
    assert not port.stopped
    assert hub._ports.get("p1") is port
    assert hub.open_count == 1


def test_apply_forwards_to_all_ports():
    hub, ports = make_hub([FakeProfile([info("p1"), info("p2")])])
    hub._reconcile(0.0)
    effect = SetKeyLight("2", Light.WAITING)
    hub.apply([effect])
    assert ports["p1"].applied[-1] == effect
    assert ports["p2"].applied[-1] == effect


def test_off_removes_from_replay_table():
    hub, _ports = make_hub([FakeProfile([])])
    hub.apply([SetKeyLight("1", Light.RUNNING)])
    hub.apply([SetKeyLight("1", Light.OFF)])
    assert hub._lights == {}


def test_validate_failure_blocks_until_unplug():
    profile = FakeProfile([info("p1")])
    hub, ports = make_hub([profile])
    hub._reconcile(0.0)
    ports["p1"].state = "disabled"
    ports["p1"].failure_kind = "validate"
    hub._reconcile(0.0)
    assert "p1" not in hub._ports
    assert "p1" in hub._blocked
    hub._reconcile(10.0)
    assert "p1" not in hub._ports
    profile.infos = []
    hub._reconcile(11.0)
    profile.infos = [info("p1")]
    hub._reconcile(12.0)
    assert "p1" in hub._ports


def test_write_failure_retries_after_backoff():
    hub, ports = make_hub([FakeProfile([info("p1")])])
    hub._reconcile(0.0)
    ports["p1"].state = "disabled"
    ports["p1"].failure_kind = "write"
    hub._reconcile(0.0)
    assert "p1" not in hub._ports
    hub._reconcile(4.9)
    assert "p1" not in hub._ports
    hub._reconcile(5.1)
    assert "p1" in hub._ports


def test_three_consecutive_failures_block_until_unplug():
    profile = FakeProfile([info("p1")])
    hub, ports = make_hub([profile])
    for t in (0.0, 5.0, 20.0):
        hub._reconcile(t)
        ports["p1"].state = "disabled"
        ports["p1"].failure_kind = "write"
        hub._reconcile(t)
    assert "p1" in hub._blocked
    hub._reconcile(100.0)
    assert "p1" not in hub._ports


def test_unverified_profile_is_never_driven():
    hub = KeyboardHub(logging.getLogger("test-hub"), profile_names=["m75v2"])
    assert hub._profiles() == []
    hub._reconcile(0.0)
    assert hub._ports == {}


# ---- 无线通道（2.4G） ----

def test_24g_only_creates_wireless_port_and_replays_lights():
    """只插接收器：建无线灯口、启动、并把当前灯态重放过去。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    hub.apply([SetKeyLight("1", Light.FAILED)])
    hub._reconcile(0.0)
    assert len(wireless) == 1
    port = wireless[0]
    assert port.started
    assert port.applied == [SetKeyLight("1", Light.FAILED)]
    assert hub.open_count == 1


def test_wireless_gets_later_effects():
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    hub._reconcile(0.0)
    effect = SetKeyLight("2", Light.WAITING)
    hub.apply([effect])
    assert wireless[0].applied == [effect]


def test_wireless_is_created_once_not_every_reconcile():
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    for t in (0.0, 2.0, 4.0, 6.0):
        hub._reconcile(t)
    assert len(wireless) == 1


def test_wired_channel_takes_over_and_stops_wireless():
    """有线优先：配置通道一出现就停掉无线灯口（2.4G 写入此时会假成功）。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, ports, wireless = make_wireless_hub(profile)
    hub._reconcile(0.0)

    profile.mode = "wired"
    profile.infos = [info("p1")]
    hub._reconcile(2.0)
    assert wireless[0].stopped
    assert hub._wireless is None
    assert "p1" in ports and ports["p1"].started


def test_wireless_stays_off_while_wired_is_present():
    profile = FakeProfile([info("p1")])
    profile.mode = "wired"
    hub, ports, wireless = make_wireless_hub(profile)
    for t in (0.0, 2.0, 4.0):
        hub._reconcile(t)
    assert wireless == []
    assert set(ports) == {"p1"}


def test_wired_takes_over_and_replays_the_current_lights():
    """#13 有线出现 → 切走：无线停、有线起，且拿到的是当前灯态（不错灯）。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, ports, wireless = make_wireless_hub(profile)
    hub.apply([SetKeyLight("1", Light.FAILED)])
    hub._reconcile(0.0)
    assert wireless[0].applied == [SetKeyLight("1", Light.FAILED)]

    hub.apply([SetKeyLight("2", Light.WAITING)])
    profile.mode = "wired"
    profile.infos = [info("p1")]
    hub._reconcile(2.0)

    assert wireless[0].stopped
    assert hub._wireless is None
    assert ports["p1"].started
    assert ports["p1"].applied == [SetKeyLight("1", Light.FAILED),
                                   SetKeyLight("2", Light.WAITING)]


def test_cable_out_returns_to_wireless_with_the_latest_lights():
    """#13 有线消失 → 切回：有线口挂掉后无线回来，并重放最新灯态。"""
    profile = FakeProfile([info("p1")])
    profile.mode = "wired"
    hub, ports, wireless = make_wireless_hub(profile)
    hub.apply([SetKeyLight("1", Light.IDLE)])
    hub._reconcile(0.0)
    assert ports["p1"].started

    ports["p1"].state = "suspended"          # 拔线后发帧失败的样子
    hub.apply([SetKeyLight("3", Light.FAILED)])
    profile.mode = "2.4g"
    profile.infos = []
    hub._reconcile(2.0)

    assert len(wireless) == 1
    assert wireless[0].started
    assert wireless[0].applied == [SetKeyLight("1", Light.IDLE),
                                  SetKeyLight("3", Light.FAILED)]


def test_set_wireless_enabled_false_stops_and_blocks_the_port():
    """托盘关掉：立刻停；之后不再建。再打开：下一次调度就建回来。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    hub.apply([SetKeyLight("1", Light.FAILED)])
    hub._reconcile(0.0)
    assert len(wireless) == 1

    hub.set_wireless_enabled(False)
    assert wireless[0].stopped
    assert hub._wireless is None
    applied_before = list(wireless[0].applied)
    hub.apply([SetKeyLight("2", Light.WAITING)])       # 关掉后不许再往无线口送
    assert wireless[0].applied == applied_before
    for t in (2.0, 4.0):
        hub._reconcile(t)
    assert len(wireless) == 1                # 关了就不建

    hub.set_wireless_enabled(True)
    hub._reconcile(6.0)
    assert len(wireless) == 2
    assert wireless[1].started


def test_toggle_off_while_wireless_port_is_being_created():
    """建灯口是慢动作：建到一半被关掉，不能留下一个在跑的灯口。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    started = threading.Event()
    release = threading.Event()
    created = []

    def slow_factory():
        started.set()
        release.wait(2.0)                    # 模拟枚举 + 握手
        port = FakeWirelessPort()
        created.append(port)
        return port

    hub = KeyboardHub(logging.getLogger("test-hub"), profile_list=[profile],
                      wireless_factory=slow_factory, clock=lambda: 0.0)
    worker = threading.Thread(target=hub._reconcile, args=(0.0,))
    worker.start()
    assert started.wait(1.0)
    hub.set_wireless_enabled(False)          # 建到一半时关掉
    release.set()
    worker.join(2.0)

    assert created and not created[0].started
    assert hub._wireless is None


def test_wireless_active_callback_fires_when_port_takes_over():
    profile = FakeProfile([])
    profile.mode = "2.4g"
    calls = []
    hub, _ports, _wireless = make_wireless_hub(
        profile, on_wireless_active=lambda: calls.append(1))
    hub._reconcile(0.0)
    hub._reconcile(2.0)
    assert calls == [1]                      # 每个灯口只报一次

    hub.set_wireless_enabled(False)
    hub._reconcile(4.0)
    hub.set_wireless_enabled(True)
    hub._reconcile(6.0)
    assert calls == [1, 1]                   # 重新接管算新的一次


def test_detect_error_does_not_tear_down_wireless():
    """枚举失败不是设备事实：已建好的无线灯口不许拆，否则每 2 秒重建一次。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    hub._reconcile(0.0)
    profile.detect_exc = RuntimeError("枚举失败")
    for t in (2.0, 4.0):
        hub._reconcile(t)
    assert len(wireless) == 1
    assert not wireless[0].stopped
    assert hub._wireless is wireless[0]


def test_no_device_tears_down_wireless():
    """接收器拔掉（模式变 none）：无线灯口关掉，不空转重试。"""
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    hub._reconcile(0.0)
    profile.mode = "none"
    hub._reconcile(2.0)
    assert wireless[0].stopped
    assert hub._wireless is None


def test_switch_off_keeps_old_behaviour(caplog):
    """无线接管关掉：不建灯口，退回「只提示一次请换有线」。"""
    caplog.set_level(logging.INFO)
    asked = []
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(
        profile, wireless_enabled=False, on_wired_needed=lambda: asked.append(1))
    for t in (0.0, 2.0, 4.0):
        hub._reconcile(t)
    assert wireless == []
    assert asked == [1]
    assert sum("只检测到 2.4G 接收器" in r.getMessage()
               for r in caplog.records) == 1


def test_suspended_wired_port_does_not_block_wireless():
    """拔线后有线端口进入 suspended：这不是「有线还在」，否则 2.4G 永远不亮。"""
    profile = FakeProfile([info("p1")])
    profile.mode = "wired"
    hub, ports, wireless = make_wireless_hub(profile)
    hub._reconcile(0.0)
    assert wireless == []

    ports["p1"].state = "suspended"
    profile.mode = "2.4g"
    profile.infos = []
    hub._reconcile(2.0)
    assert len(wireless) == 1
    assert wireless[0].started
    assert not wireless[0].stopped
    assert hub.open_count == 1


def test_stop_closes_wireless_port():
    profile = FakeProfile([])
    profile.mode = "2.4g"
    hub, _ports, wireless = make_wireless_hub(profile)
    hub._reconcile(0.0)
    hub.stop()
    assert wireless[0].stopped