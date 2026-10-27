#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单帧保持 / 键盘休眠 观察测试 v2。

比 v1 多三件事：
  1. 后台每 30 秒查一次 GetDongleStatus，记录 2.4G 链路还在不在
     —— 用来区分「灯效回落」和「键盘休眠断链」，这两者看起来都像灯灭了
  2. 检查点问的是「按 Enter 之前看到的」，因为按 Enter 会唤醒键盘、把灯重新点亮
  3. 全部输出自动落盘，程序退出也丢不了

用法：
    python hold_test.py                      # 最多 30 分钟
    python hold_test.py --max-seconds 600     # 最多 10 分钟
    python hold_test.py --dry-run --tick 1
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


def ask_before_enter():
    """问：按 Enter 之前看到什么。True = 还绿着，False = 已经灭了。"""
    say()
    say("  请回想：按下 Enter 之前，键 1 是什么状态？")
    say("  （按 Enter 会唤醒键盘、把灯重新点亮，所以要按按下之前的印象）")
    say("    直接回车      = 还是绿的")
    say("    输入 n 再回车 = 已经灭了")
    try:
        answer = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        say("  （没读到输入，记为「还是绿的」）")
        drain_console()
        return True
    drain_console()
    return not (answer.startswith("n") or answer.startswith("灭"))


_suppress_enter_until = 0.0


def drain_console():
    """清掉控制台输入缓冲里残留的 Enter。

    `input()` 只消费到换行，回车可能还留字符在缓冲里。
    只靠 drain 不够（kbhit 对 readline 留下的字符不可靠），
    所以再配一个时间抑制窗口：刚回答完的 1.5 秒内不认新的 Enter。
    """
    global _suppress_enter_until
    _suppress_enter_until = time.monotonic() + 1.5
    try:
        import msvcrt
    except ImportError:
        return
    try:
        while msvcrt.kbhit():
            msvcrt.getwch()
    except Exception:
        pass


def enter_pressed():
    """非阻塞检测 Enter（需要真实控制台）。"""
    if time.monotonic() < _suppress_enter_until:
        return False
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        if not msvcrt.kbhit():
            return False
        ch = msvcrt.getwch()
        if ch in ("\r", "\n", " "):
            while msvcrt.kbhit():
                msvcrt.getwch()
            return True
        return False
    except Exception:
        return False


def check_link(dongle, dry):
    """返回 True = 键盘仍和接收器连着。"""
    if dry:
        return True
    reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
    if reply is None:
        return None            # 接收器都没回包
    return reply[4] != 0


def run(dongle, max_seconds, tick, poll_every, dry, skip_gate):
    banner("单帧保持 / 键盘休眠 观察（v2）")
    say("  这次要分清两件长得一样的事：")
    say("    A. 灯效自己回落  → 2.4G 链路仍然正常")
    say("    B. 键盘休眠断链  → 链路断开，醒来后会把灯恢复")
    say()
    say("  做法：发 1 帧之后完全不再发颜色帧；")
    say("        后台每 %d 秒查一次链路状态，只查链路，不碰灯。" % poll_every)
    say()
    say("  ！观察要点：回来后先远看键盘，再按 Enter。")
    say("    按 Enter 会唤醒键盘，灯会重新亮，那一瞬间的状态不能算数。")
    say("    检查点问的是「按下 Enter 之前」看到的状态。")
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

    sent_at = stamp()
    say()
    say(LINE)
    say("  t=0  已发送 1 帧，键 1 现在应该是绿色（%s）" % sent_at)
    say("  之后不再发颜色帧。可以走开，回来看灯、按 Enter 记录。")
    say(LINE)

    t0 = time.monotonic()
    checkpoints = []          # (秒, 按Enter前是否绿, 当时链路)
    fallback_at = None
    link_lost_at = None
    next_tick = tick
    next_poll = poll_every

    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= max_seconds:
            break

        if enter_pressed():
            link = check_link(dongle, dry)
            say()
            say(THIN)
            say("  检查点 %d   %s   %s" % (len(checkpoints) + 1, fmt(elapsed),
                                          stamp()))
            say("  链路：%s" % {True: "正常", False: "已断开（键盘休眠）",
                               None: "接收器无回包"}[link])
            say(THIN)
            was_green = ask_before_enter()
            checkpoints.append((elapsed, was_green, link))
            if was_green:
                say("  记录：%s 时 仍为绿" % fmt(elapsed))
            else:
                fallback_at = elapsed
                say("  记录：%s 时 已灭" % fmt(elapsed))
                break
            say("  继续等待，程序不再碰灯。")

        if elapsed >= next_poll:
            link = check_link(dongle, dry)
            say("    %s   已过 %-10s 链路：%s"
                % (stamp(), fmt(elapsed),
                   {True: "正常", False: "已断开", None: "无回包"}[link]))
            if link is not True and link_lost_at is None:
                link_lost_at = elapsed
            next_poll += poll_every

        if elapsed >= next_tick:
            next_tick += tick

        time.sleep(0.05)

    if fallback_at is None:
        say()
        say(THIN)
        say("  到时间上限，最后确认一次（同样按「按 Enter 之前」的状态回答）。")
        say(THIN)
        link = check_link(dongle, dry)
        say("  链路：%s" % {True: "正常", False: "已断开", None: "无回包"}[link])
        was_green = ask_before_enter()
        checkpoints.append((time.monotonic() - t0, was_green, link))
        if not was_green:
            fallback_at = checkpoints[-1][0]

    banner("结果")
    say("  发送时刻：%s" % sent_at)
    say("  发送后未再发送任何颜色帧。")
    say()
    for i, (secs, green, link) in enumerate(checkpoints, 1):
        say("  检查点 %d  %-10s  %-8s  链路=%s"
            % (i, fmt(secs), "仍为绿" if green else "已灭",
               {True: "正常", False: "断开", None: "无回包"}[link]))

    valid = [c for c in checkpoints if c[0] >= 5.0]
    if not valid:
        say()
        say("  这次没有采到有效观察点（检查点都在 5 秒以内），无法下结论。")
        say("  可能是开始时的回车被重复识别了。重跑一次，看灯时再按 Enter。")
        if _log_path:
            say()
            say("  本次记录已保存：%s" % _log_path)
        return 0

    say()
    if fallback_at is None:
        last = checkpoints[-1][0] if checkpoints else 0
        say("  结论：单帧至少保持 %s，没有观察到回落。" % fmt(last))
        say("        可以按「不需要保活」处理。")
    else:
        idx = [c for c in checkpoints if not c[1]]
        link_then = idx[-1][2] if idx else None
        prev_green = [c[0] for c in checkpoints if c[1]]
        if link_then is False:
            say("  结论：灯灭的时候 2.4G 链路已经断了 —— 是键盘休眠，不是灯效回落。")
            if prev_green:
                say("        链路断开前，最后一次确认仍为绿的时间是 %s。"
                    % fmt(prev_green[-1]))
            say("        单帧本身没测到保持上限。")
        elif link_then is None:
            say("  结论：灯灭时接收器无回包，无法判断是休眠还是回落。")
        else:
            say("  结论：链路正常但灯灭了 → 单帧保持约 %s 后回落。"
                % fmt(fallback_at))

    if link_lost_at is not None:
        say()
        say("  注：首次观察到链路断开是 %s 左右。" % fmt(link_lost_at))

    if _log_path:
        say()
        say("  本次记录已保存：%s" % _log_path)
    return 0


def main():
    global _log, _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=int, default=1800)
    ap.add_argument("--tick", type=int, default=300,
                    help="每隔多少秒提示一次（默认 300，仅提示，不影响灯）")
    ap.add_argument("--poll", type=int, default=60,
                    help="每隔多少秒查一次链路状态，默认 60（查得太密可能反过来拖住键盘不休眠）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="跳过开始确认（自动验证用）")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    _log_path = args.log or os.path.join(
        LOG_DIR, "hold_test_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
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
        say()
        say("检查键盘是否已连上接收器 …")
        reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
        if reply is None or reply[4] == 0:
            say("  键盘没连上（休眠）。请按一下键盘任意键。")
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
        code = run(dongle, args.max_seconds, args.tick, args.poll,
                   args.dry_run, args.yes)
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