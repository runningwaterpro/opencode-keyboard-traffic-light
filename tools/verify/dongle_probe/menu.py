#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""2.4G 灯光测试入口菜单。

双击 `验证-单帧保持.cmd` 会进到这里。保留一个入口，避免点错脚本。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

LINE = "=" * 64

TESTS = {
    "1": ("单帧保持：发 1 帧，之后什么都不发，看灯多久灭",
          ["hold_test.py"],
          "用来测空闲关灯/休眠。插上充电线跑这个，可测插线会不会不休眠。"),
    "2": ("补帧 30 秒：每 30 秒发一帧，跑 6 分钟",
          ["keepalive_test.py", "--interval", "30", "--minutes", "6"],
          "已验证可用。灯基本上一直亮，每 30 秒黑 1 秒。"),
    "3": ("补帧 45 秒：每 45 秒发一帧，跑 6 分钟",
          ["keepalive_test.py", "--interval", "45", "--minutes", "6"],
          "看能不能把闪烁频率从每 30 秒降到每 45 秒。"),
    "4": ("休眠与唤醒：等它睡过去，再发帧看能不能叫醒",
          ["wake_test.py"],
          "约 3-6 分钟，需要你中途回答两次、按一次键盘。"),
    "5": ("打字干扰：每 30 秒发一帧的同时打字 120 秒",
          ["interference_test.py", "--interval", "30", "--seconds", "120",
           "--skip-phase1"],
          "看补帧会不会影响打字手感。打满两分钟，期间会补 4 帧。"),
    "6": ("补帧丢键：打字 60 秒不发帧 + 打字 120 秒每 8 秒发帧",
          ["keydrop_test.py"],
          "约 4 分钟。要连续打字两段，两段后各回答一次重复字符次数。"),
    "7": ("打字能否维持灯亮：连续打字 3 分钟，中间不发帧",
          ["typing_hold_test.py"],
          "约 3 分钟。全程打字别停，结束回答灯有没有灭过。"),
}


def main():
    print()
    print(LINE)
    print("  2.4G 键盘灯光测试")
    print(LINE)
    print()
    for key in sorted(TESTS):
        title, cmd, note = TESTS[key]
        print("  %s = %s" % (key, title))
        print("      %s" % note)
        print()
    print("  0 = 退出")
    print()
    try:
        choice = input("  输入编号，回车：").strip()
    except (EOFError, KeyboardInterrupt):
        return 0

    if choice not in TESTS:
        print("  已退出。")
        return 0

    title, cmd, note = TESTS[choice]
    print()
    print("  启动：%s" % " ".join(cmd))
    print()
    return subprocess.call([sys.executable] + cmd, cwd=HERE)


if __name__ == "__main__":
    sys.exit(main())