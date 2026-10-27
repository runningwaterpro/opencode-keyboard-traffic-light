#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打字能否维持灯亮：一直打字的话，灯会不会自己灭？

要确认的假设（整套 2.4G 设计都压在它上面）：
  键盘的空闲关灯计时是「按键重置」的。
  所以人在打字时灯不会灭，完全不需要补帧。

如果这个假设不成立（打字时灯也会在 60 秒后灭），
那么「空闲时才补帧」这条规则就废了，必须换方案。

做法：
  发 1 帧 → 你连续打字 3 分钟，中间一次都不按 Enter →
  t=0 之后程序只打印时间，不发送任何东西 →
  3 分钟后问一句：灯是一直亮着吗

用法：
    python typing_hold_test.py                 # 打 3 分钟
    python typing_hold_test.py --seconds 240   # 打 4 分钟
    python typing_hold_test.py --dry-run --seconds 5
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


def link_state(dongle, dry):
    if dry or dongle is None:
        return True
    reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
    if reply is None:
        return None
    return reply[4] != 0


def ask_verdict():
    say()
    say("  刚才这 %s 里，键 1 的灯是怎么表现的？" % "几分钟")
    say("    1 或直接回车 = 一直亮着，没灭过")
    say("    2            = 灭过，后来又亮了")
    say("    3            = 大部分时间是灭的")
    try:
        answer = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return "未回答"
    if answer.startswith("2"):
        return "灭过"
    if answer.startswith("3"):
        return "一直灭"
    return "一直亮"


def run(dongle, seconds, dry, skip_gate):
    banner("打字能否维持灯亮")
    say("  要确认：一直打字的时候，灯会不会自己灭。")
    say()
    say("  做法：")
    say("    1. 先发 1 帧，键 1 变绿")
    say("    2. 你连续打字 %s，中间不要停超过半分钟" % fmt(seconds))
    say("    3. 程序这期间不发任何东西，只打印时间")
    say("    4. 结束后回答一句：灯一直亮着吗")
    say()
    say("  ！在记事本里打字，控制台窗口放旁边。")
    say("  ！别用 Hi86 以外的键盘，不然等于没测。")
    say("  ！中途不要按 Ctrl+C，也不要切到别的输入法狂按。")
    if dry:
        say()
        say("  【试运行】不向键盘发数据。")
    say()
    say(THIN)
    say("  按 Enter 开始，然后立刻开始打字。")
    if not skip_gate:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            say("  已取消。")
            return 1

    if not dry:
        if not dongle.send_colors(GREEN_KEY1):
            say("  发送失败，中止。")
            return 1

    say()
    say(LINE)
    say("  t=0  已发 1 帧（%s），现在开始打字，别停" % stamp())
    say(LINE)

    t0 = time.monotonic()
    next_mark = 30
    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= seconds:
            break
        if elapsed >= next_mark:
            say("  %s   已打 %-10s 链路：%s"
                % (stamp(), fmt(elapsed),
                   {True: "正常", False: "断开", None: "无回包"}[link_state(dongle, dry)]))
            next_mark += 30
        time.sleep(0.1)

    final_link = link_state(dongle, dry)
    verdict = "试运行" if dry else ask_verdict()

    banner("结果")
    say("  打字时长：%s" % fmt(seconds))
    say("  结束时链路：%s" % {True: "正常", False: "断开",
                            None: "无回包"}[final_link])
    say("  你看到：%s" % verdict)
    say()
    if verdict == "一直亮":
        say("  结论：打字能维持灯亮，空闲计时是按键重置的。")
        say("        所以补帧只需要在「手停下来」之后做，打字期间完全不用发帧。")
    elif verdict == "灭过":
        say("  结论：打字也没能维持住，灯在中间灭过。")
        say("        说明关灯计时不是单纯按按键重置的，")
        say("        「空闲才补帧」这条规则不成立，要改方案。")
    elif verdict == "一直灭":
        say("  结论：打字反而让灯灭了 —— 可能是按键触发了键盘自己的灯效。")
        say("        这种情况很麻烦，需要单独查。")
    elif verdict == "试运行":
        say("  试运行，不评估结论。")
    else:
        say("  没拿到回答，无法下结论。")

    if _log_path:
        say()
        say("  本次记录已保存：%s" % _log_path)
    return 0


def main():
    global _log, _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180,
                    help="打字持续秒数，默认 180（3 分钟，远超 60 秒关灯线）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="跳过开始确认（自动验证用）")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    _log_path = args.log or os.path.join(
        LOG_DIR, "typing_hold_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
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
            _log.close()
            return 1
        reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
        if reply is None or reply[4] == 0:
            say("  键盘没连上。请按一下键盘任意键，再重跑。")
            dongle.close()
            _log.close()
            return 1
        say("  键盘已连接。")

    try:
        code = run(dongle, args.seconds, args.dry_run, args.yes)
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