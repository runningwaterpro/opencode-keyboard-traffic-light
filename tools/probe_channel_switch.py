#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""观测插拔线时的通道选择（issue #13 的真机验证）。

只做只读枚举：有线/无线两个端口都换成假端口，**不打开任何设备、不写一帧灯**，
所以不会点亮任何灯。

注意：如果有真守护正在跑，它独占有线配置通道，这里会枚举不到。先退出托盘里的守护。

用法：

    python tools/probe_channel_switch.py

脚本等「有线配置通道出现」，打印 hub 的选择，然后结束。它验证的是**插线方向**：
有线一出现，无线灯口关闭、有线接口接入。

**拔线方向不在这里测**：假端口不发帧，永远不会「写失败」，也就不会进入 suspended。
真端口每 0.5 秒写一帧，拔线后立刻失败 → suspended → 无线重建；这条路径由两个单测
分别覆盖（test_keyboard.py 的写失败→suspended，test_keyboard_hub.py 的 suspended→无线回来）。

如果插上线 3 分钟内都没看到「有线配置通道出现」，说明键盘没切到有线模式
（85/86 这类键盘有 2.4G/有线拨杆，只插线不拨开关不会切）。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from traffic_light import device, profiles  # noqa: E402
from traffic_light.core import Light, SetKeyLight  # noqa: E402
from traffic_light.keyboard_hub import KeyboardHub  # noqa: E402

WAIT_WIRED_S = 180.0
WAIT_GONE_S = 120.0
TICK_S = 1.0


class FakePort:
    """假端口：只记录 start/stop，绝不碰设备。"""

    def __init__(self, label: str):
        self.label = label
        self.state = "waiting"

    def apply(self, effects) -> None:
        pass

    def start(self) -> None:
        self.state = "open"

    def stop(self) -> None:
        self.state = "closed"

    @property
    def failure_kind(self):
        return None


def wired_channel_present() -> bool:
    """和守护同一套判据：能不能找到有线配置通道。"""
    try:
        return device.find_config_channel() is not None
    except Exception as exc:
        print("  枚举失败（按「没有」处理）：%s" % exc)
        return False


def mode_now() -> str:
    try:
        return device.detect_mode()
    except Exception:
        return "unknown"


def snapshot(hub) -> str:
    return "模式=%-5s 有线通道=%-2s 无线灯口=%s" % (
        mode_now(), "有" if wired_channel_present() else "无",
        "开" if hub._wireless is not None else "关")


def tick(hub) -> None:
    hub._reconcile(time.monotonic())


def watch_until(hub, predicate, timeout: float, prompt: str) -> bool:
    start = time.monotonic()
    last_prompt = 0.0
    last_beat = 0.0
    last_line = None
    while time.monotonic() - start < timeout:
        tick(hub)
        line = snapshot(hub)
        now = time.monotonic()
        if line != last_line:
            print("  [%s] %s" % (time.strftime("%H:%M:%S"), line), flush=True)
            last_line = line
            last_beat = now
        elif now - last_beat >= 5.0:
            # 5 秒一次心跳：证明有线通道是「一直在」还是「闪一下就没了」
            print("  [%s] %s" % (time.strftime("%H:%M:%S"), line), flush=True)
            last_beat = now
        if predicate():
            return True
        if now - last_prompt >= 15.0:
            last_prompt = now
            print("  %s（还剩 %.0f 秒）" % (prompt, timeout - (now - start)), flush=True)
        time.sleep(TICK_S)
    return False


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="  [%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("probe")
    print("通道切换观测（只读枚举，不开设备、不写灯）")

    hub = KeyboardHub(
        log,
        profile_list=[profiles.Hi86Profile()],
        port_factory=lambda path, profile: FakePort("wired:" + path),
        wireless_factory=lambda: FakePort("wireless"),
    )
    hub.apply([SetKeyLight("1", Light.IDLE)])   # 有灯态，端口才会被建

    print("\n第一步：")
    if wired_channel_present():
        print("  有线通道已经在，请先拔掉数据线（最多等 %.0f 秒）" % WAIT_GONE_S)
        if not watch_until(hub, lambda: not wired_channel_present(), WAIT_GONE_S,
                           "请拔掉数据线"):
            print("  失败：等不到拔线")
            return 1
    else:
        print("  当前没有有线通道")
    print("  请现在插上数据线（最多等 %.0f 秒）" % WAIT_WIRED_S)
    if not watch_until(hub, wired_channel_present, WAIT_WIRED_S, "等插线"):
        print("\n结论：插线后没有出现有线配置通道，#13 的触发条件在这台机器上不成立。")
        return 1
    seen = snapshot(hub)
    print("  有线出现后的状态：%s" % seen)
    wireless_off = hub._wireless is None
    hub.stop()

    print("\n结果：")
    print("  %s 有线出现 → 无线灯口关闭、有线接口接入"
          % ("通过" if wireless_off else "失败"))
    print("  拔线方向由单测覆盖（真端口写失败 → suspended → 无线重建），"
          "假端口在这里模拟不出来。")
    return 0 if wireless_off else 1


if __name__ == "__main__":
    sys.exit(main())