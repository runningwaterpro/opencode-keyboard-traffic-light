# -*- coding: utf-8 -*-
"""机型档案：把“不同键盘的协议差异”关在这一层。

设计原则：
  1. 核心只认 Capability 声明（能驱动哪些键、什么帧率、是否已验证）；
  2. 未验证的档案（verified=False）绝不写入设备，只允许只读探测；
  3. 新机型 = 新增一个档案，不改状态机、槽位、通知。

Hi86 行为与改造前逐字节一致：本类只是把 device/frame 的调用转接一层，
且用函数内延迟导入，保证测试里注入的假 device 模块仍然生效。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

MODE_WIRED = "wired"
MODE_24G = "2.4g"
MODE_NONE = "none"

NUMBER_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")


@dataclass(frozen=True)
class Capability:
    """一个机型能做什么的显式声明。核心只按这个声明渲染。"""

    name: str
    keys: tuple[str, ...]
    rgb: bool
    breathing: bool
    max_fps: int
    verified: bool
    note: str = ""


class Profile(Protocol):
    capability: Capability

    def candidate_ids(self) -> set[tuple[int, int]]: ...
    def detect_mode(self, devices=None) -> str: ...
    def find_channel(self, devices=None): ...
    def list_channels(self, devices=None) -> list: ...
    def validate(self, info) -> tuple[bool, str | None]: ...
    def open(self, info) -> int: ...
    def render(self, lights, now_ms: int) -> bytes: ...
    def write(self, handle: int, payload: bytes) -> None: ...
    def close(self, handle: int) -> None: ...
    def oemdrv_running(self) -> bool: ...


class Hi86Profile:
    """Hi86（258A:010C，0xFF00/0x0001，520 字节特征报告）。已验证。

    device_module / frame_module 显式注入：测试用它注入假设备层；
    生产环境不传，方法内部延迟导入真实模块。
    """

    capability = Capability(name="hi86", keys=NUMBER_KEYS, rgb=True,
                            breathing=True, max_fps=20, verified=True)

    def __init__(self, device_module=None, frame_module=None):
        self._device_module = device_module
        self._frame_module = frame_module

    def _dev(self):
        if self._device_module is not None:
            return self._device_module
        from . import device
        return device

    def _frame(self):
        if self._frame_module is not None:
            return self._frame_module
        from . import frame
        return frame

    def candidate_ids(self):
        device = self._dev()
        return {(device.VID_WIRED, device.PID_WIRED),
                (device.VID_RECEIVER, device.PID_RECEIVER)}

    def detect_mode(self, devices=None):
        return self._dev().detect_mode(devices)

    def find_channel(self, devices=None):
        return self._dev().find_config_channel(devices)

    def list_channels(self, devices=None):
        """所有通过完整校验的配置通道（同型号多把键盘 = 多条路径）。"""
        device = self._dev()
        if devices is None:
            devices = device.list_devices()
        out = []
        for info in devices:
            ok, _ = device.validate_channel(info)
            if ok:
                out.append(info)
        return out

    def validate(self, info):
        return self._dev().validate_channel(info)

    def open(self, info):
        return self._dev().open_exclusive(info)

    def render(self, lights, now_ms):
        frame = self._frame()
        breath = frame.breath_brightness(int(now_ms))
        return frame.build_frame(frame.render_slots(lights, breath))

    def write(self, handle, payload):
        self._dev().write_feature(handle, payload)

    def close(self, handle):
        self._dev().close(handle)

    def oemdrv_running(self):
        return self._dev().oemdrv_running()


class M75V2Profile:
    """新盟 M75V2（预研：有线 0C45:8006/8007，2.4G dongle 05AC:024F）。

    状态：**未验证**。协议来自官方驱动静态分析（docs/m75v2-preresearch.md），
    尚未在真机确认。因此 validate() 恒为 False，render/open/write 直接抛错：
    在 L2 只读探测通过前，绝不向这把键盘发一个字节。
    """

    capability = Capability(name="m75v2", keys=NUMBER_KEYS, rgb=True,
                            breathing=True, max_fps=20, verified=False,
                            note="协议未验证（L2 未通过）：仅探测，不写入")

    VID_WIRED = 0x0C45
    PIDS_WIRED = (0x8006, 0x8007)
    VID_RECEIVER = 0x05AC
    PID_RECEIVER = 0x024F

    def candidate_ids(self):
        return ({(self.VID_WIRED, pid) for pid in self.PIDS_WIRED}
                | {(self.VID_RECEIVER, self.PID_RECEIVER)})

    def detect_mode(self, devices=None):
        from . import device
        if devices is None:
            devices = device.list_devices()
        if any(d.vid == self.VID_WIRED and d.pid in self.PIDS_WIRED for d in devices):
            return MODE_WIRED
        if any(d.vid == self.VID_RECEIVER and d.pid == self.PID_RECEIVER for d in devices):
            return MODE_24G
        return MODE_NONE

    def find_channel(self, devices=None):
        from . import device
        if devices is None:
            devices = device.list_devices()
        for info in devices:
            if info.vid == self.VID_WIRED and info.pid in self.PIDS_WIRED:
                return info
        return None

    def list_channels(self, devices=None):
        return []   # 未验证：绝不让任何设备进入驱动路径

    def validate(self, info):
        return False, self.capability.note

    def open(self, info):
        raise RuntimeError(self.capability.note)

    def render(self, lights, now_ms):
        raise RuntimeError(self.capability.note)

    def write(self, handle, payload):
        raise RuntimeError(self.capability.note)

    def close(self, handle):
        from . import device
        device.close(handle)

    def oemdrv_running(self):
        return False


PROFILES = {"hi86": Hi86Profile(), "m75v2": M75V2Profile()}


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError("未知机型档案 %r（可选：%s）"
                         % (name, ", ".join(sorted(PROFILES)))) from None


def verified_profiles() -> list[Profile]:
    """所有已验证档案；未验证档案永远不会进入驱动路径。"""
    return [p for p in PROFILES.values() if p.capability.verified]


def select_profile(devices=None):
    """选第一个“已验证且当前存在”的档案；没有返回 None。

    未验证档案永不入选：探测阶段不允许任何写入路径被接上。
    """
    for name in ("hi86", "m75v2"):
        profile = PROFILES[name]
        if not profile.capability.verified:
            continue
        try:
            mode = profile.detect_mode(devices)
        except Exception:
            continue
        if mode != MODE_NONE:
            return profile
    return None


def probe_rows(devices, include_all: bool = False):
    """探测脚本用的纯筛选：候选 VID/PID，或（include_all）全部厂商自定义集合。"""
    if include_all:
        return [d for d in devices if d.usage_page >= 0xFF00]
    ids = set()
    for profile in PROFILES.values():
        ids |= profile.candidate_ids()
    return [d for d in devices if (d.vid, d.pid) in ids]