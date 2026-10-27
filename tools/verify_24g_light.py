#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真机验收：2.4G 无线灯口（issue #11）。

跑的是生产代码本身（wireless.WirelessLightPort + dongle.DongleTransport），
只把日志和计时打到控制台，不改任何发送规则。

用法（标准权限即可，不需要管理员）：

    python tools/verify_24g_light.py            # 全流程，约 3 分钟
    python tools/verify_24g_light.py --phase a  # 只测亮/灭，约 50 秒

前置条件：键盘切到 2.4G、**拔掉数据线**（插线后 2.4G 写入会 ACK 但不生效）。

阶段：
  A  亮/灭        键 1 亮绿 → 等限流放行 → 熄灭。人看灯即可。
  B  打字不出帧    持续打字 60 秒，断言「系统空闲 < 15 秒」时一帧都没发。
  C  停手自己回来  停手后键 3 亮绿，断言发帧那一刻空闲 ≥ 15 秒。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from traffic_light import device, dongle, frame  # noqa: E402
from traffic_light.core import Light, SetKeyLight  # noqa: E402
from traffic_light.wireless import (RESEND_S, STEP_S, TYPING_IDLE_S,  # noqa: E402
                                    WirelessLightPort, default_idle_seconds)

COLOR_NAMES = {
    frame.COLORS[Light.FAILED]: "红",
    frame.COLORS[Light.WAITING]: "黄",
    frame.COLORS[Light.IDLE]: "绿",
}


class ReportingTransport(dongle.DongleTransport):
    """只加打印的传输层，发送逻辑完全复用生产实现。"""

    def __init__(self):
        self.sent: list[float] = []
        self.last_buf = None

    def open(self):
        handle = super().open()
        print("  接收器已打开：%s" % handle.path)
        return handle

    def send(self, handle, buf):
        super().send(handle, buf)
        self.sent.append(time.monotonic())
        self.last_buf = bytes(buf)
        print("  [%s] 已发一帧（第 %d 帧）" % (time.strftime("%H:%M:%S"), len(self.sent)))


def describe(buf) -> str:
    """把 378 字节缓冲翻成「键 1=绿」这样的短句，方便对人复述画面。"""
    if buf is None:
        return "（还没发过帧）"
    parts = []
    for key, index in frame.NUMBER_ROW_LED.items():
        rgb = (buf[index], buf[dongle.LED_SLOTS + index],
               buf[dongle.LED_SLOTS * 2 + index])
        if rgb != (0, 0, 0):
            parts.append("%s=%s" % (key, COLOR_NAMES.get(rgb, rgb)))
    return "键 " + "，".join(parts) if parts else "全灭"


def pump_until_frame(port, transport, timeout: float, label: str) -> bool:
    """等一帧发出去；期间每 5 秒报一次进度（含系统空闲时间）。"""
    start = time.monotonic()
    last_note = 0.0
    while time.monotonic() - start < timeout:
        port._step(time.monotonic())
        if transport.sent and transport.sent[-1] >= start:
            print("  %s：一帧已发（等待 %.0f 秒）" % (label, time.monotonic() - start))
            return True
        now = time.monotonic()
        if now - last_note >= 5.0:
            last_note = now
            print("  %s：等 %.0f 秒 / 上限 %.0f 秒（系统空闲 %.0f 秒）"
                  % (label, now - start, timeout, default_idle_seconds()))
        time.sleep(STEP_S)
    print("  %s：超时未发出" % label)
    return False


def warn_if_cable_plugged() -> None:
    try:
        devices = device.list_devices()
    except Exception as exc:
        print("  设备枚举失败（不影响继续）：%s" % exc)
        return
    for info in devices:
        if (info.vid, info.pid) == (dongle.VID_RECEIVER, dongle.PID_RECEIVER) \
                and info.usage_page == 0x01 and info.usage == 0x06 \
                and info.input_length == 9:
            print("  警告：发现第二个 3554:FA09 键盘集合，数据线可能还插着。")
            print("        插线时 2.4G 写入会被 ACK，但键盘不执行，灯不会变。")
            return


def phase_a(port, transport) -> bool:
    print("\nA 亮/灭：键 1 亮绿 → 熄灭（请看键盘）")
    port.apply([SetKeyLight("1", Light.IDLE)])
    if not pump_until_frame(port, transport, TYPING_IDLE_S + 10, "点亮键 1"):
        return False
    print("  画面：%s   ← 人看：键 1 应为绿色" % describe(transport.last_buf))

    port.apply([SetKeyLight("1", Light.OFF)])
    print("  请求熄灭…（限流：距上一帧要满 %.0f 秒）" % RESEND_S)
    if not pump_until_frame(port, transport, RESEND_S + 10, "熄灭键 1"):
        return False
    print("  画面：%s   ← 人看：键 1 应熄灭" % describe(transport.last_buf))
    return True


def phase_b(port, transport) -> bool:
    print("\nB 打字不出帧：现在请开始连续打字 60 秒（内容随意，别长时间停手）")
    port.apply([SetKeyLight("2", Light.FAILED)])
    frames = 0
    violations = 0
    observed_typing = False
    end = time.monotonic() + 60.0
    while time.monotonic() < end:
        idle = default_idle_seconds()
        before = len(transport.sent)
        port._step(time.monotonic())
        if idle < 3.0:
            observed_typing = True
        if len(transport.sent) > before:
            frames += 1
            print("  [%s] 发帧时空闲 %.1f 秒" % (time.strftime("%H:%M:%S"), idle))
            if idle < TYPING_IDLE_S:
                violations += 1
                print("  !! 打字期间发帧了：这条规则不能破")
        time.sleep(STEP_S)
    print("  打字 60 秒：发帧 %d 次；其中空闲 < %.0f 秒时发帧 %d 次"
          % (frames, TYPING_IDLE_S, violations))
    if not observed_typing:
        print("  没检测到打字（系统空闲一直很大）：这一轮无效，请真的敲键盘")
    return observed_typing and violations == 0


def phase_c(port, transport) -> bool:
    print("\nC 停手自己回来：请把手离开键盘，等键 3 亮绿（上限 %.0f 秒）"
          % (RESEND_S + 10))
    port.apply([SetKeyLight("3", Light.IDLE)])
    start = time.monotonic()
    while time.monotonic() - start < RESEND_S + 10:
        before = len(transport.sent)
        idle = default_idle_seconds()
        port._step(time.monotonic())
        if len(transport.sent) > before:
            ok = idle >= TYPING_IDLE_S
            print("  [%s] 停手 %.0f 秒后补发一帧（发帧时空闲 %.1f 秒）"
                  % (time.strftime("%H:%M:%S"), time.monotonic() - start, idle))
            print("  画面：%s   ← 人看：键 3 应为绿色" % describe(transport.last_buf))
            return ok
        time.sleep(STEP_S)
    print("  失败：%.0f 秒内没有补发" % (RESEND_S + 10))
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="2.4G 无线灯口真机验收")
    ap.add_argument("--phase", choices=["a", "b", "c", "all"], default="all")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")
    print("2.4G 无线灯口真机验收（issue #11）")
    warn_if_cable_plugged()

    transport = ReportingTransport()
    port = WirelessLightPort(logging.getLogger("verify"), transport=transport)
    results = {}
    try:
        if args.phase in ("a", "all"):
            results["A 亮/灭"] = phase_a(port, transport)
        if args.phase in ("b", "all"):
            results["B 打字不出帧"] = phase_b(port, transport)
        if args.phase in ("c", "all"):
            results["C 停手回来"] = phase_c(port, transport)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        port.apply([SetKeyLight("1", Light.OFF), SetKeyLight("2", Light.OFF),
                    SetKeyLight("3", Light.OFF)])
        print("\n收尾：请求熄灭…")
        pump_until_frame(port, transport, RESEND_S + 10, "收尾熄灭")
        port.stop()

    print("\n结果：")
    for name, ok in results.items():
        print("  %s  %s" % ("通过" if ok else "失败", name))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())