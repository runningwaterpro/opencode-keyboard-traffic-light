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
  D  常亮十分钟    补帧长跑（issue #12）：每 35 秒一帧、链路不掉、灯不灭。
                  单独跑：--phase d [--minutes 10]，'all' 不含 D（太长）。
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
        self.handle = None

    def open(self):
        handle = super().open()
        self.handle = handle
        print("  接收器已打开：%s" % handle.path)
        return handle

    def send(self, handle, buf):
        super().send(handle, buf)
        self.sent.append(time.monotonic())
        self.last_buf = bytes(buf)
        print("  [%s] 已发一帧（第 %d 帧）" % (time.strftime("%H:%M:%S"), len(self.sent)))

    def close(self, handle):
        super().close(handle)
        self.handle = None

    def link_ok(self) -> bool | None:
        """只读的链路检查：接收器有回包且键盘已连。None = 手里没有句柄。"""
        handle = self.handle
        if handle is None:
            return None
        try:
            reply = handle.simple_command(dongle.CMD_GET_DONGLE_STATUS)
        except Exception:
            return False
        return bool(reply and reply[4])


def link_text(ok: bool | None) -> str:
    if ok is None:
        return "句柄未打开"
    return "正常" if ok else "无回包/键盘未连"


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


def cable_plugged() -> bool:
    """数据线是否还插着。

    插线时键盘走有线通道，2.4G 写入会被接收器 ACK 但键盘不执行。
    判据：出现了第二个 3554:FA09 键盘集合（接收器自己的那个是 21 字节输入）。
    枚举失败按「没插」处理，不拦着用户跑。
    """
    try:
        devices = device.list_devices()
    except Exception:
        return False
    return any((info.vid, info.pid) == (dongle.VID_RECEIVER, dongle.PID_RECEIVER)
               and info.usage_page == 0x01 and info.usage == 0x06
               and info.input_length == 9 for info in devices)


def warn_if_cable_plugged() -> None:
    if cable_plugged():
        print("  警告：发现第二个 3554:FA09 键盘集合，数据线可能还插着。")
        print("        插线时 2.4G 写入会被 ACK，但键盘不执行，灯不会变。")


def wait_until_unplugged(max_wait: float) -> bool:
    """等数据线拔掉再开始计时；超时返回 False。"""
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        if not cable_plugged():
            return True
        print("  数据线还插着，等它拔掉（最多等 %.0f 秒）…" % max_wait, flush=True)
        time.sleep(2.0)
    return False


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


def phase_d(port, transport, minutes: float) -> bool:
    """补帧长跑：每 35 秒一帧，帧间间隔始终小于灯 60 秒的寿命。"""
    print("\nD 常亮：请离开键盘，别碰键盘和鼠标（%.0f 分钟）" % minutes)
    print("  预期：键 1 常绿；每 35 秒黑约 1 秒后自己回来；不能整块灭掉")
    if not wait_until_unplugged(600.0):
        print("  数据线一直没拔：插线时 2.4G 写入不生效，本轮无法验收")
        return False
    port.apply([SetKeyLight("1", Light.IDLE), SetKeyLight("3", Light.OFF)])
    initial = len(transport.sent)
    start = time.monotonic()
    last_report = start
    last_send = None
    seen = initial
    gaps: list[float] = []
    link_failures = 0
    while time.monotonic() - start < minutes * 60.0:
        port._step(time.monotonic())
        now = time.monotonic()
        while seen < len(transport.sent):          # 用真实发送时刻算间隔
            sent_at = transport.sent[seen]
            if last_send is not None:
                gaps.append(sent_at - last_send)
            last_send = sent_at
            seen += 1
        if now - last_report >= 60.0:
            last_report = now
            link = transport.link_ok()
            if link is not True:
                time.sleep(1.0)        # 可能正好撞上补帧，再确认一次
                link = transport.link_ok()
            if link is not True:
                link_failures += 1
            print("  [%s] %4.0f 分钟：补帧 %d 次，最大间隔 %.0f 秒，链路 %s"
                  % (time.strftime("%H:%M:%S"), (now - start) / 60.0,
                     len(transport.sent) - initial, max(gaps or [0]),
                     link_text(link)))
        time.sleep(STEP_S)

    frames = len(transport.sent) - initial
    gap_max = max(gaps or [0.0])
    need = max(1, int(minutes * 60.0 / RESEND_S) - 2)
    print("  %.0f 分钟：补帧 %d 次（至少应有 %d 次），最大间隔 %.0f 秒，"
          "链路异常 %d 次" % (minutes, frames, need, gap_max, link_failures))
    print("  ← 人看：键 1 全程亮，只在补帧时黑一下（约 1 秒）")
    return frames >= need and gap_max <= 45.0 and link_failures == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2.4G 无线灯口真机验收")
    ap.add_argument("--phase", choices=["a", "b", "c", "d", "all"], default="all")
    ap.add_argument("--minutes", type=float, default=10.0,
                    help="D 阶段跑多久（分钟，默认 10）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(message)s")
    print("2.4G 无线灯口真机验收（issue #11 / #12）")
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
        if args.phase == "d":
            results["D 常亮 %.0f 分钟" % args.minutes] = phase_d(
                port, transport, args.minutes)
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