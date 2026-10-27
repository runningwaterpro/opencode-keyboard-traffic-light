#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""2.4G 灯控 / 键盘输入干扰 验证 —— 只测试一种重发间隔，打字手感对比。

用法：
    python interference_test.py                 # 4 秒间隔
    python interference_test.py --interval 2     # 2 秒间隔
    python interference_test.py --interval 2.5   # 2.5 秒间隔
    python interference_test.py --dry-run         # 试运行

第 1 段保持 25 秒，只发首帧，之后静默 —— 让你观察灯回落速度。
第 2 段持续 40 秒，按 --interval 指定频率重发 —— 请你打字对比手感。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from dongle_led import (open_dongle, make_color_buf, NUMBER_ROW_LED,
                        CMD_GET_DONGLE_STATUS)

GREEN_KEY1 = make_color_buf({NUMBER_ROW_LED["1"]: (0, 255, 0)})
BLACK = make_color_buf({})

LINE = "=" * 60
THIN = "-" * 60

P1_SECS = 25
P2_SECS = 40

_log = None


def say(text=""):
    print(text, flush=True)
    if _log is not None:
        _log.write(text + "\n")
        _log.flush()


def banner(title):
    say()
    say(LINE)
    say("  " + title)
    say(LINE)


def countdown(seconds, label):
    say()
    for n in range(seconds, 0, -1):
        say("    %s：%d" % (label, n))
        time.sleep(1.0)


def wait_keyboard(dongle):
    say("检查键盘是否已连上接收器 …")
    reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
    if reply is not None and reply[4] != 0:
        say("  键盘已连接。")
        return True
    say()
    say("  键盘没有连上（大概率休眠）。请按一下任意键。")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        time.sleep(1.0)
        reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
        if reply is not None and reply[4] != 0:
            say("  键盘已连接，继续。")
            return True
        say("    仍在等待 … 请按一下键盘")
    say("  等了 120 秒还没连上，退出。")
    return False


def phase1(dongle, dry):
    banner("第 1 段 / 共 2 段：只发 1 帧，之后静默 %d 秒" % P1_SECS)
    say("  观察重点：键 1 的灯多久熄灭？（已知大约是 60 秒的空闲关灯）")
    countdown(3, "3 秒后开始")
    say()
    say(">>> 第 1 段开始 —— 盯键 1 的灯 <<<")
    if not dry:
        if not dongle.send_colors(GREEN_KEY1):
            say("    发送失败，中止。")
            return False
    say("    t=0  已发 1 帧，键 1 变绿")
    start = time.monotonic()
    for mark in (5, 10, 15, 20, 25):
        while time.monotonic() - start < mark:
            time.sleep(0.1)
        say("    t=%-4d 灯还绿着吗？" % mark)
    return True


def phase2(dongle, dry, interval: float, seconds: int):
    banner("第 2 段 / 共 2 段：每 %.1f 秒重发一次，持续 %d 秒"
           % (interval, seconds))
    say("  每帧约 0.25 秒占满 2.4G 链路，间隔越短占比越高：")
    say("    间隔 %.1f 秒 → 占用 %.1f%%" % (interval, 0.25 / interval * 100))
    say()
    say("  这 %d 秒内请一直打字，打你熟悉的一段话。" % seconds)
    say("  打完回头看：有没有少字、重复、明显延迟？")
    countdown(5, "请把手放到键盘上，准备打字")
    say()
    say(">>> 第 2 段开始 —— 现在开始打字，持续 %d 秒 <<<" % seconds)
    start = time.monotonic()
    frames = 0
    while time.monotonic() - start < seconds:
        if not dry:
            if not dongle.send_colors(GREEN_KEY1):
                say("    发送失败，中止。")
                return False
        frames += 1
        elapsed = int(time.monotonic() - start)
        say("    已发 %2d 帧   已过 %2d / %d 秒   继续打字" %
            (frames, elapsed, seconds))
        stop = min(time.monotonic() + interval, start + seconds)
        while time.monotonic() < stop:
            time.sleep(0.05)
    say(">>> 第 2 段结束，可以停手了 <<<")
    return True


def main():
    global _log
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=4.0,
                    help="第 2 段的重发间隔，单位秒（默认 4.0）")
    ap.add_argument("--seconds", type=int, default=P2_SECS,
                    help="第 2 段持续秒数，默认 40")
    ap.add_argument("--skip-phase1", action="store_true",
                    help="跳过第 1 段（只测打字干扰，省 30 秒）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default="")
    args = ap.parse_args()
    dry = args.dry_run
    interval = args.interval
    if not 0.5 <= interval <= 120:
        say("--interval 必须在 0.5..120 之间，收到 %.1f" % interval)
        return 1

    if args.log:
        _log = open(args.log, "w", encoding="utf-8", newline="\n")
    else:
        default_log = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs",
            "interference_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(os.path.dirname(default_log), exist_ok=True)
        _log = open(default_log, "w", encoding="utf-8", newline="\n")
    if _log:
        say("（本次输出同时保存到 %s）" % _log.name)

    banner("2.4G 灯控 / 键盘输入干扰 验证（间隔 %.1f 秒）" % interval)
    say("  全程约 %d 秒。第 1 段静默，第 2 段打字。"
        % ((0 if args.skip_phase1 else P1_SECS) + args.seconds + 10))
    say()
    say("  开始前请确认：")
    say("    1. 键盘开关在 2.4G，接收器插在电脑上")
    say("    2. 关掉 LEOBOG ONE 等官方灯光软件")
    say("    3. 窗口保持可见，不要最小化")
    if dry:
        say()
        say("  【试运行】不向键盘发任何东西。")
    say()
    say(THIN)
    say("  本测试会点亮键 1 的绿灯，结束时全灭。")
    say(THIN)

    dongle = None
    if not dry:
        try:
            dongle = open_dongle()
        except RuntimeError as exc:
            say()
            say("  打开设备失败：")
            for line in str(exc).splitlines():
                say("    " + line)
            return 1
        say()
        if not wait_keyboard(dongle):
            dongle.close()
            return 1

    if dry:
        say()
        say("  【试运行】直接往下走，不等待按键。")
    else:
        say()
        say("  准备好后，按 Enter 开始。")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            say("  已取消。")
            if dongle:
                dongle.close()
            return 1

    try:
        ok = True
        if not args.skip_phase1:
            ok = phase1(dongle, dry)
        if ok:
            ok = phase2(dongle, dry, interval, args.seconds)
    except KeyboardInterrupt:
        say()
        say("  被 Ctrl+C 中断，正在熄灭 …")
        ok = False

    banner("收尾")
    if not dry and dongle:
        say("  发送全灭帧 …")
        dongle.send_colors(BLACK)
        dongle.close()
    say("  结束。")

    say()
    say(THIN)
    say("  观察点：")
    if not args.skip_phase1:
        say("    1. 第 1 段：键 1 多久熄灭？")
    say("    2. 第 2 段：间隔 %.1f 秒，打字还有没有滞涩感？" % interval)
    say(THIN)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
