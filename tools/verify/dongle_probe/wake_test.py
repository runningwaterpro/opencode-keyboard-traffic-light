#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""休眠与唤醒验证：键盘睡下去之后，光靠发颜色帧能不能把它叫醒？

要回答两件事：
  1. 不碰键盘、也不发帧时，多久彻底断链（GetDongleStatus 无回包）
  2. 断链之后连续发 3 帧颜色，灯会不会亮
     （如果亮 = 帧能唤醒；不亮 = 只能靠按键唤醒，必须靠物理接触）

对产品的意义：
  - 帧能唤醒 → 2.4G 可以「熄屏省电 + 需要时点火」，不用一直补帧
  - 只有按键能唤醒 → 状态牌必须持续补帧，或者改成插线

用法：
    python wake_test.py
    python wake_test.py --max-wait 300 --frames 3
    python wake_test.py --dry-run
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


def link_state(dongle):
    """True=已连   False=接收器回了包但键盘没连   None=无回包"""
    if dongle is None:          # 试运行
        return None
    reply = dongle.simple_command(CMD_GET_DONGLE_STATUS)
    if reply is None:
        return None
    return reply[4] != 0


def link_text(st):
    return {True: "正常", False: "回了包但键盘未连", None: "无回包（已断链）"}[st]


def ask_yes_no(question, default=True):
    say()
    say("  " + question)
    say("    直接回车 = %s" % ("是" if default else "否"))
    say("    输入 y / n 也行")
    try:
        answer = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not answer:
        return default
    return answer.startswith("y") or answer.startswith("是")


def phase_wait_sleep(dongle, max_wait, dry):
    banner("第 1 段：等它自己睡过去（期间不发任何帧）")
    say("  请不要碰键盘。程序每 5 秒查一次链路，只查不发。")
    say("  最多等 %s。" % fmt(max_wait))
    say()
    say("  ！这段时间别按键盘，也别切当前窗口。")
    t0 = time.monotonic()
    next_check = 0.0
    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= max_wait:
            say("  到上限 %s，链路仍是：%s" % (fmt(elapsed), link_text(link_state(dongle))))
            return None
        if elapsed >= next_check:
            st = link_state(dongle)
            say("  %s   已过 %-10s 链路：%s" % (stamp(), fmt(elapsed), link_text(st)))
            if st is not True:
                if dry:
                    say("  【试运行】链路变化不生效，直接结束本段。")
                    return elapsed
                say()
                say("  → 断链了，用时 %s。" % fmt(elapsed))
                return elapsed
            next_check += 5.0
        time.sleep(0.2)


def phase_poke(dongle, frames, gap, dry):
    banner("第 2 段：只发颜色帧，看能不能把它叫醒")
    say("  键盘现在是睡着的。接下来发 %d 帧，间隔 %d 秒。" % (frames, gap))
    say()
    say("  ！发完不要碰键盘，先看灯。看完回答一个问题。")
    say()
    sent = 0
    for i in range(frames):
        if not dry:
            ok = dongle.send_colors(GREEN_KEY1)
        else:
            ok = True
        sent += 1
        st = link_state(dongle)
        say("  %s   第 %d 帧 %s   链路：%s"
            % (stamp(), sent, "发送成功" if ok else "发送失败", link_text(st)))
        if i < frames - 1:
            time.sleep(gap)
    return sent


def phase_confirm_by_key(dongle, dry):
    banner("第 3 段：对照组 —— 按一下键盘能不能亮")
    say("  现在请按一下键盘上的任意键（比如 Shift），然后回来按 Enter。")
    try:
        input("  按完回车 > ")
    except (EOFError, KeyboardInterrupt):
        return None
    if dry:
        return True
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        st = link_state(dongle)
        if st is True:
            say("  链路：正常（键盘醒了）")
            return True
        time.sleep(1.0)
    say("  链路：%s" % link_text(link_state(dongle)))
    return False


def run(dongle, max_wait, frames, gap, dry, skip_gate):
    banner("休眠与唤醒验证")
    say("  两件事一起测：")
    say("    1. 不碰键盘时，多久彻底断链")
    say("    2. 断链之后，光发颜色帧能不能把灯点亮")
    say()
    say("  全程大约 %s，需要你配合两次：" % fmt(max_wait + 60))
    say("    开始前按一次 Enter")
    say("    第 2 段结束后看一眼灯、回答一句")
    say("    第 3 段按一下键盘，再按 Enter")
    if dry:
        say()
        say("  【试运行】不向键盘发数据。")
    say()
    say(THIN)
    say("  按 Enter 开始（之后请停止碰键盘）。")
    if not skip_gate:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            say("  已取消。")
            return 1

    sleep_took = phase_wait_sleep(dongle, max_wait, dry)
    if sleep_took is None:
        say()
        say("  没等到休眠，后面的测试做不了。可以加大 --max-wait 再跑。")
        return 1

    phase_poke(dongle, frames, gap, dry)

    lit_after_frames = True if dry else ask_yes_no(
        "刚才发完那几帧，键 1 亮了吗？（按发完之后、没碰键盘时的状态回答）",
        default=False)

    woke_by_frames = dry
    if not dry:
        st = link_state(dongle)
        say("  现在链路：%s" % link_text(st))
        woke_by_frames = (st is True)

    phase_confirm_by_key(dongle, dry)
    lit_after_key = True if dry else ask_yes_no(
        "按了键盘之后，键 1 亮了吗？", default=True)

    banner("结果")
    say("  休眠用时：%s" % fmt(sleep_took))
    say("  发 %d 帧之后：" % frames)
    say("    灯亮了        ：%s" % ("是" if lit_after_frames else "否"))
    say("    链路恢复      ：%s" % ("是" if woke_by_frames else "否"))
    say("  按一下键盘之后：")
    say("    灯亮了        ：%s" % ("是" if lit_after_key else "否"))
    say()
    if dry:
        say("  试运行，不评估结论。")
    elif lit_after_frames or woke_by_frames:
        say("  结论：颜色帧能把键盘叫醒（至少能点亮灯）。")
        say("        2.4G 可以「平时不补帧、需要时点火」。")
    else:
        say("  结论：颜色帧叫不醒键盘，只有物理按键能唤醒。")
        say("        要常亮就必须持续补帧；不能接受闪烁就插线。")

    if _log_path:
        say()
        say("  本次记录已保存：%s" % _log_path)
    return 0


def main():
    global _log, _log_path
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-wait", type=int, default=300,
                    help="等休眠的最长秒数，默认 300")
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--gap", type=int, default=5, help="帧间隔秒数，默认 5")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true", help="跳过开始确认（自动验证用）")
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    _log_path = args.log or os.path.join(
        LOG_DIR, "wake_test_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
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
            if _log:
                _log.close()
            return 1
        st = link_state(dongle)
        if st is not True:
            say("  键盘目前没有连上（%s）。" % link_text(st))
            say("  请按一下键盘任意键，然后重跑。")
            dongle.close()
            if _log:
                _log.close()
            return 1
        say("  键盘已连接，可以开始。")

    try:
        code = run(dongle, args.max_wait, args.frames, args.gap,
                   args.dry_run, args.yes)
    except KeyboardInterrupt:
        say()
        say("  被 Ctrl+C 中断。")
        code = 1
    finally:
        if dongle is not None:
            try:
                say()
                say("  收尾：发送全灭帧 …")
                dongle.send_colors(BLACK)
                dongle.close()
                say("  已全灭。")
            except Exception:
                pass
        if _log is not None:
            _log.close()
    return code


if __name__ == "__main__":
    sys.exit(main())