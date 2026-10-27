#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""保活验证：只发颜色帧、完全不碰键盘，看灯能不能一直亮着。

背景（2026-10-27 实测）：
  键盘空闲约 60 秒会自己关灯，但 2.4G 链路仍然正常
  （GetDongleStatus 到 2 分钟时仍回 byte4 != 0）。
  所以灯灭是「固件空闲关灯」，不是休眠断链。

本脚本回答一个问题：
  每隔 N 秒发一帧颜色，能不能把灯一直维持住？
  如果灯能被重新点亮，则「2.4G 常亮」只要 1 帧 / N 秒，链路占用可忽略。

观察方式（可以不守着）：
  - 过程中看到灯灭了，按 n → 记录灭的时刻
  - 走开也行，程序照样按间隔补帧，只打印时间
  - 结束时回答一个问题：这段时间灯是怎么表现的

用法：
    python keepalive_test.py                     # 每 45 秒补一帧，跑 6 分钟
    python keepalive_test.py --interval 30 --minutes 5
    python keepalive_test.py --dry-run --interval 1 --minutes 0.1
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

LINE = "=" * 64
THIN = "-" * 64

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
_log = None
_log_path = None


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


def fmt(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%d 秒" % seconds
    return "%d 分 %02d 秒" % (seconds // 60, seconds % 60)


def stamp():
    return time.strftime("%H:%M:%S")


def drain_console():
    try:
        import msvcrt
    except ImportError:
        return
    try:
        while msvcrt.kbhit():
            msvcrt.getwch()
    except Exception:
        pass


def link_state(dongle, dry):
    if dry:
        return True
    reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
    if reply is None:
        return None
    return reply[4] != 0


def ask_verdict():
    say()
    say("  （现在灯可能已经亮了 —— 你敲键盘唤醒的。请按刚才那几分钟里的情况回答）")
    say("  这段时间灯是怎么表现的？")
    say("    1 或直接回车 = 偶尔灭过，但每次补帧都被点回来（有闪）")
    say("    2            = 一直亮着，没灭过")
    say("    3            = 大部分时间是灭的（补帧点不亮）")
    try:
        answer = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        drain_console()
        return "未回答"
    drain_console()
    if answer.startswith("2"):
        return "一直亮"
    if answer.startswith("3"):
        return "点不亮"
    if answer.startswith("1"):
        return "灭过又点亮"
    return "未回答"


def run(dongle, interval, minutes, dry, skip_gate, link_every):
    banner("保活验证：只发颜色帧，不碰键盘")
    say("  要回答的问题：")
    say("    键盘空闲约 60 秒会自己关灯，链路却还活着。")
    say("    那每隔 %s 补一帧颜色，能不能让它一直亮着？" % fmt(interval))
    say()
    say("  过程：")
    say("    1. 先发 1 帧，键 1 变绿")
    say("    2. 之后不做任何操作，每隔 %s 自动补一帧" % fmt(interval))
    say("    3. 总计 %s" % fmt(minutes * 60))
    say()
    say("  你只需要看。全程不要碰 Hi86 键盘 ——")
    say("    敲它任何一个键都会唤醒它、把灯点亮，观测就废了。")
    say("    要打字请用另一把键盘或鼠标。")
    say()
    say("  ！观察重点：")
    say("    - 1 分钟左右灯会不会灭（这是键盘自己的空闲关灯）")
    say("    - 灭了之后，下一次补帧会不会把它点回来")
    say("    - 补帧会不会让键盘一直不休眠（看链路状态）")
    if dry:
        say()
        say("  【试运行】不向键盘发任何数据。")
    say()
    say(THIN)
    say("  按 Enter 开始。")
    if not skip_gate:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            say("  已取消。")
            return 1
    drain_console()

    if not dry:
        if not dongle.send_colors(GREEN_KEY1):
            say("  发送失败，中止。")
            return 1

    t0 = time.monotonic()
    say()
    say(LINE)
    say("  第 1 帧已发（%s），键 1 应该绿了。" % stamp())
    say("  接下来每次补帧只打印一行。你可以走开。")
    say(LINE)

    sends = 1
    send_log = []          # (第几帧, 秒)
    link_notes = []
    next_send = interval
    next_link = link_every

    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= minutes * 60:
            break

        if elapsed >= next_send:
            ok = True
            if not dry:
                ok = dongle.send_colors(GREEN_KEY1)
            sends += 1
            send_log.append((sends, elapsed))
            say("  %s   第 %d 帧已发%s"
                % (stamp(), sends, "" if ok else "  ← 发送失败"))
            next_send += interval

        if elapsed >= next_link:
            st = link_state(dongle, dry)
            say("  %s   已过 %-10s 链路：%s"
                % (stamp(), fmt(elapsed), {True: "正常", False: "已断开",
                                           None: "无回包"}[st]))
            link_notes.append((elapsed, st))
            next_link += link_every

        time.sleep(0.05)

    verdict = "试运行" if dry else ask_verdict()

    banner("结果")
    say("  补帧间隔：%s" % fmt(interval))
    say("  补帧次数：%d" % sends)
    say("  结束时回答：%s" % verdict)

    say()
    if verdict == "一直亮":
        say("  结论：%s 的补帧足够维持常亮，灯没有灭过。" % fmt(interval))
    elif verdict == "灭过又点亮":
        say("  结论：补帧能把灯点回来，但两次补帧之间会灭 —— 会看到闪。")
        say("        把 --interval 调到 %s 以下（比如 %s）再跑一次。" % (
            fmt(interval), fmt(max(10, interval // 2))))
    elif verdict == "点不亮":
        say("  结论：关灯后接收器发的颜色不再生效，2.4G 无法维持常亮。")
        say("        需要换策略（插线供电、或接受灯会灭）。")
    elif verdict == "试运行":
        say("  试运行，不评估结论。")
    else:
        say("  没有拿到明确回答，无法下结论。")

    if link_notes:
        worst = [i for i, st in link_notes if st is not True]
        say()
        if worst:
            say("  注：首次链路异常出现在 %s。" % fmt(worst[0]))
        else:
            say("  注：全程链路正常，键盘没有休眠。")

    if _log_path:
        say()
        say("  本次记录已保存：%s" % _log_path)
    return 0


def main():
    global _log, _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=45,
                    help="补帧间隔秒数，默认 45（键盘约 60 秒空闲关灯）")
    ap.add_argument("--minutes", type=float, default=6,
                    help="总时长分钟数，默认 6")
    ap.add_argument("--link-every", type=int, default=15,
                    help="每隔多少秒查一次链路，默认 15（用来定位断链时刻）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="跳过开始确认（自动验证用）")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    _log_path = args.log or os.path.join(
        LOG_DIR, "keepalive_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)
    _log = open(_log_path, "w", encoding="utf-8", newline="\n")
    say("（本次输出同时保存到 %s）" % _log_path)

    dongle = None
    if not args.dry_run:
        try:
            dongle = open_dongle()
        except RuntimeError as exc:
            say()
            say("  打开设备失败：")
            for line in str(exc).splitlines():
                say("    " + line)
            return 1
        reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
        if reply is None or reply[4] == 0:
            say("  键盘没连上。请按一下键盘任意键。")
            deadline = time.monotonic() + 120
            last_note = 0.0
            while time.monotonic() < deadline:
                time.sleep(1.0)
                reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
                if reply is not None and reply[4] != 0:
                    break
                if time.monotonic() - last_note >= 5:
                    say("    仍在等待 … 请按一下键盘")
                    last_note = time.monotonic()
            if reply is None or reply[4] == 0:
                say("  120 秒还没连上，退出。")
                dongle.close()
                return 1
        say("  键盘已连接。")

    try:
        code = run(dongle, args.interval, args.minutes, args.dry_run,
                   args.yes, args.link_every)
    except KeyboardInterrupt:
        say()
        say("  被 Ctrl+C 中断。")
        code = 1
    finally:
        if dongle is not None:
            say()
            say("  收尾：发送全灭帧 …")
            dongle.send_colors(BLACK)
            dongle.close()
            say("  已全灭。")
        if _log is not None:
            _log.close()
    return code


if __name__ == "__main__":
    sys.exit(main())