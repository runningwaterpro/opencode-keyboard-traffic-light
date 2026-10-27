#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补帧丢键验证：多久补一帧，打字时就开始出现重复字符？

做法是两段对照，同一个键盘、同一篇文字：

  A 段（对照）：打字 60 秒，全程不发帧
  B 段（测试）：打字 120 秒，每 8 秒发一帧

两段各记一次「出现几次重复字符 / 少字」，就能算出丢键概率。
8 秒间隔是故意放大的——30 秒太稀疏，一次也遇不上。

用法：
    python keydrop_test.py
    python keydrop_test.py --interval 8 --seconds 120
    python keydrop_test.py --dry-run --interval 1 --seconds 4
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


def stamp():
    return time.strftime("%H:%M:%S")


def fmt(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%d 秒" % seconds
    return "%d 分 %02d 秒" % (seconds // 60, seconds % 60)


def drain_console():
    global _suppress_until
    _suppress_until = time.monotonic() + 1.5


def countdown(seconds, label):
    say()
    for n in range(seconds, 0, -1):
        say("    %s：%d" % (label, n))
        time.sleep(1.0)


def ask_count(question):
    say()
    say("  " + question)
    say("    直接回车 = 0 次；填个数字也行")
    try:
        answer = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        drain_console()
        return None
    drain_console()
    if not answer:
        return 0
    try:
        return int(answer)
    except ValueError:
        return None


def run_control(dry):
    banner("A 段（对照）：打字 60 秒，全程不发帧")
    say("  这一段用来确认：不发帧的时候，键盘本身会不会丢键。")
    say("  请连续打字 60 秒，打你熟悉的内容。")
    countdown(5, "准备")
    say()
    say(">>> 开始打字，持续 60 秒 <<<")
    start = time.monotonic()
    last_note = 0
    while time.monotonic() - start < 60:
        elapsed = int(time.monotonic() - start)
        if elapsed // 10 != last_note:
            say("    已过 %2d / 60 秒" % elapsed)
            last_note = elapsed // 10
        time.sleep(0.2)
    say(">>> 停止打字 <<<")
    return ask_count("这一段（不发帧）出现几次重复字符或明显丢字？")


def run_frames(dongle, interval, seconds, dry):
    banner("B 段（测试）：打字 %d 秒，每 %s 发一帧" % (seconds, fmt(interval)))
    say("  这一段每 %s 会发一帧颜色。发帧的那一瞬间最容易丢键。" % fmt(interval))
    say("  屏幕上出现 [第 N 帧] 的那一行，就是刚发帧。")
    say("  灯会跟着闪，别管它，专心打字。")
    countdown(5, "准备")
    say()
    say(">>> 开始打字，持续 %d 秒 <<<" % seconds)
    start = time.monotonic()
    frames = 0
    next_send = 0.0
    while time.monotonic() - start < seconds:
        elapsed = time.monotonic() - start
        if elapsed >= next_send:
            ok = True
            if not dry:
                ok = dongle.send_colors(GREEN_KEY1)
            frames += 1
            say("    %s   [第 %d 帧]%s" % (stamp(), frames,
                                          "" if ok else "  发送失败"))
            next_send += interval
        time.sleep(0.05)
    say(">>> 停止打字 <<<")
    count = ask_count(
        "这一段（每 %s 一帧，共 %d 帧）出现几次重复字符或明显丢字？"
        % (fmt(interval), frames))
    return count, frames


def run(dongle, interval, seconds, dry, skip_gate):
    banner("补帧丢键验证")
    say("  要回答：补帧到底会不会让打字出错，频率多高。")
    say()
    say("  两个阶段，都需要你连续打字：")
    say("    A 段 60 秒，不发帧（对照）")
    say("    B 段 %s，每 %s 发一帧（共约 %d 帧）"
        % (fmt(seconds), fmt(interval), max(1, int(seconds / interval))))
    say()
    say("  ！两段之间会问你「出现几次重复字符」，凭印象回答即可。")
    say("  ！请在 Hi86 上打字，不要用别的键盘。")
    if dry:
        say()
        say("  【试运行】不向键盘发数据，只走流程。")
    say()
    say(THIN)
    say("  按 Enter 开始 A 段。")
    if not skip_gate:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            say("  已取消。")
            return 1
    drain_console()

    a_count = run_control(dry)
    b_count, frames = run_frames(dongle, interval, seconds, dry)

    banner("结果")
    say("  A 段（不发帧，60 秒）：%s" % ("未回答" if a_count is None else "%d 次" % a_count))
    say("  B 段（每 %s 一帧，%d 帧）：%s"
        % (fmt(interval), frames,
           "未回答" if b_count is None else "%d 次" % b_count))
    say()
    if a_count is None or b_count is None:
        say("  没拿到完整回答，无法下结论。")
    elif b_count == 0:
        say("  结论：每 %s 一帧，这次没有出现丢键。" % fmt(interval))
        say("        可以再跑一次，或者把 --interval 调小到 4 秒加大压力。")
    elif a_count == 0 and b_count > 0:
        say("  结论：不发帧不出错，发帧就出错 —— 丢键是补帧引起的。")
        say("        按每帧一次估算，出现概率约 %.0f%%（%d 次 / %d 帧）。"
            % (min(100.0, 100.0 * b_count / max(1, frames)), b_count, frames))
        say("        所以：打字时必须暂停补帧。")
    else:
        say("  A 段也有出错（%d 次），说明键盘本身偶尔也会丢键，不能全算在补帧头上。")
        say("  但 B 段出错 %d 次 / %d 帧，仍可认为补帧加重了问题。" % (b_count, frames))

    if _log_path:
        say()
        say("  本次记录已保存：%s" % _log_path)
    return 0


def main():
    global _log, _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=8,
                    help="B 段补帧间隔秒数，默认 8（故意放大丢键概率）")
    ap.add_argument("--seconds", type=int, default=120,
                    help="B 段持续秒数，默认 120")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="跳过开始确认（自动验证用）")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    _log_path = args.log or os.path.join(
        LOG_DIR, "keydrop_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
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
        code = run(dongle, args.interval, args.seconds, args.dry_run, args.yes)
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