"""键盘灯光口测试：全离线，假设备模块，绝不碰真 device.py / 真设备。

技巧：先把假模块塞进 sys.modules["traffic_light.device"]，再导入 keyboard；
导入后立即复原全局 sys.modules，避免把假模块泄漏给同进程的其他测试文件。
假模块的 HidDeviceInfo 用命名元组：keyboard.py 只用到字段存在性/相等性。
"""
import collections
import logging
import sys
import time
import types

import pytest

import traffic_light
from traffic_light import frame
from traffic_light.core import Light, SetKeyLight

# ---- 假设备模块（可脚本化：mode / oemdrv / channel_gone / validate_ok / write_fail） ----

_fake = types.ModuleType("traffic_light.device")
_fake.MODE_WIRED = "wired"
_fake.MODE_24G = "2.4g"
_fake.MODE_NONE = "none"
_fake.HidDeviceInfo = collections.namedtuple(
    "HidDeviceInfo", "path vid pid usage_page usage feature_length")


class _FakeDeviceError(Exception):
    pass


_fake.DeviceError = _FakeDeviceError
_fake.INFO = _fake.HidDeviceInfo(
    path=r"\\?\hid#vid_258a&pid_010c&mi_04&col06#7&2c4b1e5c&0&0000#fake",
    vid=0x258A, pid=0x010C, usage_page=0xFF00, usage=0x0001, feature_length=520)


def _reset_fake():
    """恢复默认脚本：none、不折腾、不失败、清空调用记录。"""
    _fake.mode = "none"
    _fake.oemdrv = False
    _fake.oemdrv_exc = None
    _fake.detect_exc = None
    _fake.channel_gone = False
    _fake.validate_ok = True
    _fake.validate_why = None
    _fake.open_fail = False
    _fake.write_fail = False
    _fake.write_exc = None
    _fake.calls = []
    _fake.writes = []
    _fake.closed = []
    _fake.handle_seq = 0


def _detect_mode(devices=None):
    _fake.calls.append("detect_mode")
    if _fake.detect_exc is not None:
        raise _fake.detect_exc
    return _fake.mode


def _find_config_channel(devices=None):
    _fake.calls.append("find_config_channel")
    if _fake.channel_gone or _fake.mode != "wired":
        return None
    return _fake.INFO


def _validate_channel(info):
    _fake.calls.append("validate_channel")
    if info is None:
        return False, "info 为 None"
    return _fake.validate_ok, _fake.validate_why


def _open_exclusive(info):
    _fake.calls.append("open_exclusive")
    if _fake.open_fail:
        raise _FakeDeviceError("独占打开失败：桩")
    _fake.handle_seq += 1
    return 1000 + _fake.handle_seq


def _write_feature(handle, payload):
    _fake.calls.append("write_feature")
    if _fake.write_fail:
        raise _fake.write_exc or _FakeDeviceError("HidD_SetFeature 失败：桩")
    _fake.writes.append((handle, bytes(payload)))


def _close(handle):
    _fake.calls.append("close")
    _fake.closed.append(handle)


def _oemdrv_running():
    _fake.calls.append("oemdrv_running")
    if _fake.oemdrv_exc is not None:
        raise _fake.oemdrv_exc
    return _fake.oemdrv


_fake.detect_mode = _detect_mode
_fake.find_config_channel = _find_config_channel
_fake.validate_channel = _validate_channel
_fake.open_exclusive = _open_exclusive
_fake.write_feature = _write_feature
_fake.close = _close
_fake.oemdrv_running = _oemdrv_running

_reset_fake()


# ---- 装桩、导入 keyboard、立即复原全局 ----

_prev_module = sys.modules.get("traffic_light.device")
_prev_attr = traffic_light.__dict__.get("device")
sys.modules["traffic_light.device"] = _fake
traffic_light.device = _fake                        # 让 from . import device 命中桩
sys.modules.pop("traffic_light.keyboard", None)     # 确保在桩上重新导入
import traffic_light.keyboard as _keyboard_module
KeyboardLightPort = _keyboard_module.KeyboardLightPort
assert _keyboard_module.device is _fake, "假设备模块没有生效"
sys.modules.pop("traffic_light.keyboard", None)
traffic_light.__dict__.pop("keyboard", None)
if _prev_module is not None:
    sys.modules["traffic_light.device"] = _prev_module
else:
    sys.modules.pop("traffic_light.device", None)
if _prev_attr is not None:
    traffic_light.device = _prev_attr
else:
    traffic_light.__dict__.pop("device", None)


# ---- 测试辅助 ----

LOG = logging.getLogger("keyboard-test")
LED1 = frame.PAYLOAD_OFFSET + frame.NUMBER_ROW_LED["1"] * frame.BYTES_PER_LED
LED2 = frame.PAYLOAD_OFFSET + frame.NUMBER_ROW_LED["2"] * frame.BYTES_PER_LED


@pytest.fixture()
def fake():
    _reset_fake()
    yield _fake
    _reset_fake()


def make_port(**kwargs):
    kwargs.setdefault("device_check_interval", 2.0)
    kwargs.setdefault("oem_check_interval", 5.0)
    return KeyboardLightPort(LOG, **kwargs)


def msgs(caplog):
    return [record.getMessage() for record in caplog.records]


def payload_of(fake_module, index=-1):
    return fake_module.writes[index][1]


def test_wired_opens_once_and_writes_full_frame(fake):
    """wired：打开恰一次；520 字节、帧头正确、键 1 亮紫、其余 LED 全灭。"""
    fake.mode = "wired"
    port = make_port()
    port.apply([SetKeyLight("1", Light.RUNNING)])
    port._step(0.0)
    assert port._state == "open"
    assert fake.calls.count("open_exclusive") == 1
    for t in (0.05, 0.10, 0.15):
        port._step(t)
    assert fake.calls.count("open_exclusive") == 1   # 已打开，不重复打开
    assert len(fake.writes) == 3
    handle, payload = fake.writes[-1]
    assert isinstance(handle, int) and handle > 0
    assert len(payload) == frame.FRAME_LENGTH == 520
    assert payload[:8] == frame.HEADER
    assert tuple(payload[LED1:LED1 + 3]) == frame.COLORS[Light.RUNNING]
    rest = payload[frame.PAYLOAD_OFFSET:LED1] + payload[LED1 + 3:]
    assert rest == bytes(len(rest))


def test_24g_asks_for_wired_once(fake, caplog):
    """2.4G：不打开、回调恰一次、日志恰一次，状态保持 waiting。"""
    caplog.set_level(logging.INFO)
    fake.mode = "2.4g"
    asked = []
    port = make_port(on_wired_needed=lambda: asked.append(1))
    for t in (0.0, 1.0, 2.0, 3.0, 4.0, 5.0):
        port._step(t)
    assert "open_exclusive" not in fake.calls
    assert asked == [1]
    assert port._state == "waiting"
    assert sum("2.4G" in m for m in msgs(caplog)) == 1


def test_none_does_not_open_or_ask(fake):
    """none：只等待，不打开、不回调。"""
    fake.mode = "none"
    asked = []
    port = make_port(on_wired_needed=lambda: asked.append(1))
    for t in (0.0, 2.0, 4.0):
        port._step(t)
    assert "open_exclusive" not in fake.calls
    assert asked == []
    assert port._state == "waiting"


def test_oemdrv_blocks_open_until_exit(fake):
    """OemDrv 起初在跑：不打开；退出后自动打开。"""
    fake.mode = "wired"
    fake.oemdrv = True
    port = make_port()
    port._step(0.0)
    port._step(1.0)
    assert port._state == "hold_oem"
    assert "open_exclusive" not in fake.calls
    fake.oemdrv = False
    port._step(6.0)
    assert port._state == "open"
    assert fake.calls.count("open_exclusive") == 1


def test_oemdrv_closes_open_handle_and_recovers(fake, caplog):
    """OemDrv 在开灯后启动：关句柄、暂停发帧、日志只一次；退出后自动恢复。"""
    caplog.set_level(logging.INFO)
    fake.mode = "wired"
    port = make_port()
    port.apply([SetKeyLight("1", Light.RUNNING)])
    port._step(0.0)
    port._step(0.1)
    assert len(fake.writes) == 1
    fake.oemdrv = True
    port._step(5.0)
    port._step(5.5)
    assert port._state == "hold_oem"
    assert port._handle is None
    assert len(fake.closed) == 1
    assert len(fake.writes) == 1          # 避让期间不写帧
    assert sum("OemDrv.exe 在运行" in m for m in msgs(caplog)) == 1
    fake.oemdrv = False
    port._step(10.0)
    port._step(10.1)
    assert port._state == "open"
    assert fake.calls.count("open_exclusive") == 2
    assert len(fake.writes) == 2


def test_write_failure_channel_gone_suspends_then_recovers(fake, caplog):
    """写失败且通道消失：suspended；插回后自动重新打开并继续写。"""
    caplog.set_level(logging.INFO)
    fake.mode = "wired"
    port = make_port()
    port.apply([SetKeyLight("1", Light.RUNNING)])
    port._step(0.0)
    port._step(0.1)
    assert len(fake.writes) == 1
    fake.write_fail = True
    fake.channel_gone = True
    port._step(0.2)
    assert port._state == "suspended"
    assert port._handle is None
    assert len(fake.closed) == 1
    port._step(0.3)
    assert port._state == "suspended"     # 断线期间保持等待
    assert len(fake.writes) == 1
    fake.mode = "none"                    # 线拔着：设备报 none
    port._step(2.0)
    assert port._state != "disabled"      # 拔线不是故障，不能停灯
    assert fake.calls.count("open_exclusive") == 1
    fake.mode = "wired"                   # 插回
    fake.write_fail = False
    fake.channel_gone = False
    port._step(4.0)
    assert port._state == "open"
    assert fake.calls.count("open_exclusive") == 2
    port._step(4.1)
    assert len(fake.writes) == 2
    assert tuple(payload_of(fake)[LED1:LED1 + 3]) == frame.COLORS[Light.RUNNING]
    assert sum("键盘离线，等待恢复" in m for m in msgs(caplog)) == 1


@pytest.mark.parametrize("exc_kind", ["device", "generic"])
def test_write_failure_channel_present_disables_forever(fake, caplog, exc_kind):
    """写失败且通道仍在：disabled，之后不再检测、不再打开、不再写。"""
    caplog.set_level(logging.INFO)
    fake.mode = "wired"
    port = make_port()
    port.apply([SetKeyLight("1", Light.RUNNING)])
    port._step(0.0)
    port._step(0.1)
    fake.write_fail = True
    fake.write_exc = (_FakeDeviceError("HidD_SetFeature 失败：桩")
                      if exc_kind == "device" else RuntimeError("驱动炸了"))
    port._step(0.2)
    assert port._state == "disabled"
    assert port._handle is None
    assert len(fake.closed) == 1
    frozen = len(fake.calls)
    for t in (3.0, 5.0, 7.0, 10.0):
        port._step(t)
    assert len(fake.calls) == frozen      # disabled 后连设备检测都不再调
    assert port._state == "disabled"
    assert sum("发帧失败，停止灯光功能" in m for m in msgs(caplog)) == 1


def test_validate_failure_disables_without_opening(fake, caplog):
    """通道复核不过：disabled、不打开，且永不重试。"""
    caplog.set_level(logging.INFO)
    fake.mode = "wired"
    fake.validate_ok = False
    fake.validate_why = "VID 不符：期望 0x258A，实际 0x0000"
    port = make_port()
    port._step(0.0)
    assert port._state == "disabled"
    assert "open_exclusive" not in fake.calls
    frozen = len(fake.calls)
    port._step(10.0)
    assert len(fake.calls) == frozen
    found = msgs(caplog)
    assert sum("校验不符" in m for m in found) == 1
    assert any("VID 不符" in m for m in found)


def test_wired_without_config_channel_disables(fake):
    """detect 说 wired 却找不到配置通道：按复核不符处理，disabled。"""
    fake.mode = "wired"
    fake.channel_gone = True
    port = make_port()
    port._step(0.0)
    assert port._state == "disabled"
    assert "open_exclusive" not in fake.calls


def test_open_failure_disables(fake):
    """独占打开失败（可能被官方程序占用）：disabled，不重试。"""
    fake.mode = "wired"
    fake.open_fail = True
    port = make_port()
    port._step(0.0)
    assert port._state == "disabled"
    assert port._handle is None
    frozen = len(fake.calls)
    port._step(10.0)
    assert len(fake.calls) == frozen


def test_apply_off_removes_light_and_ignores_junk(fake):
    """OFF 把该键从灯表移除（帧里回全 0）；非 SetKeyLight 效果忽略。"""
    fake.mode = "wired"
    port = make_port()
    port.apply([SetKeyLight("1", Light.RUNNING)])
    port._step(0.0)
    port._step(0.1)
    assert tuple(payload_of(fake)[LED1:LED1 + 3]) == frame.COLORS[Light.RUNNING]
    port.apply(["不是效果", object()])
    port._step(0.2)
    assert tuple(payload_of(fake)[LED1:LED1 + 3]) == frame.COLORS[Light.RUNNING]
    port.apply([SetKeyLight("1", Light.OFF)])
    assert "1" not in port._lights
    port._step(0.3)
    assert payload_of(fake)[LED1:LED1 + 3] == b"\x00\x00\x00"


def test_waiting_light_renders_breathing_yellow(fake):
    """等待=黄色呼吸：红绿分量 > 0、蓝恒 0（宽松断言，不比波形）。"""
    fake.mode = "wired"
    port = make_port()
    port.apply([SetKeyLight("2", Light.WAITING)])
    port._step(0.0)
    port._step(0.1)
    r, g, b = payload_of(fake)[LED2:LED2 + 3]
    assert r > 0 and g > 0 and b == 0


def test_stop_closes_handle_and_is_idempotent(fake):
    """stop() 关句柄；重复 stop 不报错、不再关。"""
    fake.mode = "wired"
    port = make_port()
    port._step(0.0)
    assert port._handle is not None
    port.stop()
    assert port._handle is None
    assert fake.closed == [1001]
    port.stop()
    assert len(fake.closed) == 1


def test_start_runs_background_loop_and_stop_joins(fake):
    """start/stop：后台线程真的发帧；stop 后线程退出、句柄关闭。"""
    fake.mode = "wired"
    port = make_port(clock=lambda: 0.0)
    port.apply([SetKeyLight("1", Light.RUNNING)])
    port.start()
    try:
        deadline = time.monotonic() + 3.0
        while not fake.writes and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        port.stop()
    assert len(fake.writes) >= 1
    assert port._handle is None
    assert len(fake.closed) == 1


def test_detect_error_does_not_disable_and_retries(fake):
    """枚举失败不是设备事实：不改状态，下一轮到期间隔后重试。"""
    fake.mode = "wired"
    fake.detect_exc = _FakeDeviceError("枚举失败")
    port = make_port()
    port._step(0.0)
    assert "open_exclusive" not in fake.calls
    assert port._state != "disabled"
    fake.detect_exc = None
    port._step(2.0)
    assert port._state == "open"


def test_oemdrv_check_error_is_treated_as_running(fake):
    """OemDrv 查询异常按「在跑」处理：不确定就不碰设备（不抢占）。"""
    fake.mode = "wired"
    fake.oemdrv_exc = _FakeDeviceError("进程快照失败")
    port = make_port()
    port._step(0.0)
    assert port._state == "hold_oem"
    assert "open_exclusive" not in fake.calls
    fake.oemdrv_exc = None
    port._step(5.0)
    assert port._state == "open"


def test_on_wired_needed_error_does_not_break_step(fake):
    """回调抛异常不能拖垮状态机。"""
    fake.mode = "2.4g"

    def boom():
        raise RuntimeError("通知口挂了")

    port = make_port(on_wired_needed=boom)
    port._step(0.0)
    assert port._state == "waiting"


def test_unverified_profile_stays_waiting_and_never_touches_device(fake):
    """未验证档案：只等待，不探测、不打开、不写（L2 通过前绝不碰设备）。"""
    from traffic_light.profiles import M75V2Profile
    port = make_port(profile=M75V2Profile())
    for t in (0.0, 2.0, 5.0, 10.0):
        port._step(t)
    assert port._state == "waiting"
    assert fake.calls == []