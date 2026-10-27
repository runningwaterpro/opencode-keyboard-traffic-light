#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""观测插拔线时的通道选择（issue #13 的真机验证）。

只做只读枚举：有线/无线两个端口都换成假端口，**不打开任何设备、不写一帧灯**，
所以不会点亮任何灯。

注意：如果有真守护正在跑，它独占有线配置通道，这里会枚举不到。先退出托盘里的守护。

用法：

    python tools/probe_channel_switch.py

使用真端口测两个方向（会真的点亮键 1，只拨开关、不用拔线）：

    python tools/probe_channel_switch.py --real-wired

  1. 拨杆拨到有线 → 有线通道出现 → 无线灯口关闭、真端口独占打开并点亮键 1
  2. 拨杆拨回 2.4G → 有线口写失败 → suspended → 无线灯口回来

不传 --real-wired 时两侧都是假端口，只做只读枚举，**不打开任何设备、不写一帧灯**。
这种模式只能测插线方向：假端口不发帧，永远不会「写失败」，进不了 suspended。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from traffic_light import device, profiles  # noqa: E402
from traffic_light.core import Light, SetKeyLight  # noqa: E402
from traffic_light.keyboard import KeyboardLightPort  # noqa: E402
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
    """一行状态。有线端口是「轮到它说话」的那个：真端口独占后枚举会看不到通道。"""
    states = "/".join(sorted({p.state for p in hub._ports.values()})) or "无"
    return "枚举=%-2s 有线端口=%-16s 模式=%-5s 无线灯口=%s" % (
        "有" if wired_channel_present() else "无", states, mode_now(),
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
    import argparse

    ap = argparse.ArgumentParser(description="观测插线/拨杆时的通道选择")
    ap.add_argument("--real-wired", action="store_true",
                    help="有线侧用真端口：会点亮键 1，用来测拨回 2.4G 后的恢复")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="  [%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("probe")
    print("通道切换观测（%s）" % ("真有线端口，会点亮键 1" if args.real_wired
                                else "只读枚举，不开设备、不写灯"))

    def wired_factory(path, profile):
        if not args.real_wired:
            return FakePort("wired:" + path)
        return KeyboardLightPort(log, profile=profile, bound_path=path)

    hub = KeyboardHub(
        log,
        profile_list=[profiles.Hi86Profile()],
        port_factory=wired_factory,
        wireless_factory=lambda: FakePort("wireless"),
    )
    hub.apply([SetKeyLight("1", Light.IDLE)])   # 有灯态，端口才会被建

    print("\n第一步：")
    if wired_channel_present():
        print("  有线通道已经在，请先拨回 2.4G（最多等 %.0f 秒）" % WAIT_GONE_S)
        if not watch_until(hub, lambda: not wired_channel_present(), WAIT_GONE_S,
                           "请拨回 2.4G"):
            print("  失败：等不到有线通道消失")
            return 1
    else:
        print("  当前没有有线通道")
    print("  请现在把拨杆拨到有线（最多等 %.0f 秒）" % WAIT_WIRED_S)
    if not watch_until(hub, wired_channel_present, WAIT_WIRED_S, "等拨到有线"):
        print("\n结论：拨到有线后没有出现 258A:010C 配置通道。")
        return 1

    wireless_off = hub._wireless is None
    print("  拨到有线后：%s" % snapshot(hub))
    print("  ← 人看：键 1 应该亮绿（真端口真的发帧了）")

    if not args.real_wired:
        hub.stop()
        print("\n结果：")
        print("  %s 有线出现 → 无线灯口关闭、有线接口接入"
              % ("通过" if wireless_off else "失败"))
        print("  （拨回 2.4G 的方向要加 --real-wired：假端口不发帧，模拟不出 suspended）")
        return 0 if wireless_off else 1

    print("\n第二步：请把拨杆拨回 2.4G（最多等 60 秒）")
    if not watch_until(hub, lambda: hub._wireless is not None, 60.0,
                       "等无线灯口回来"):
        print("  失败：拨回 2.4G 后无线灯口没有回来")
        hub.stop()
        return 1
    back = snapshot(hub)
    print("  拨回 2.4G 后：%s" % back)
    hub.stop()

    print("\n结果：")
    print("  %s 有线出现 → 无线灯口关闭、有线接口接入"
          % ("通过" if wireless_off else "失败"))
    print("  %s 有线消失 → 无线灯口回来（%s）" % ("通过", back))
    return 0 if wireless_off else 1


if __name__ == "__main__":
    sys.exit(main())