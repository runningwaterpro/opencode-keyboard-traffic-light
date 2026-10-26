# -*- coding: utf-8 -*-
"""多键盘调度测试：假档案 + 假端口，不碰真设备。"""
import logging
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

    def list_channels(self, devices=None):
        return list(self.infos)

    def detect_mode(self, devices=None):
        return "none"


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