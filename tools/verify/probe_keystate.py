# -*- coding: utf-8 -*-
"""诊断：GetAsyncKeyState 有没有哪个键位一直报「按过」。

用法：python tools/probe_keystate.py --seconds 10
这段时间不要碰键盘，看有没有键位一直亮着。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=10)
    args = ap.parse_args()

    counts = {}
    down = {}
    end = time.monotonic() + args.seconds
    polls = 0
    while time.monotonic() < end:
        for vk in range(0x08, 0x100):
            state = int(user32.GetAsyncKeyState(vk))
            if state & 0x0001:
                counts[vk] = counts.get(vk, 0) + 1
            if state & 0x8000:
                down[vk] = down.get(vk, 0) + 1
        polls += 1
        time.sleep(0.02)

    print("轮询 %d 次，用时 %d 秒" % (polls, args.seconds))
    print()
    if counts:
        print("始终报「按过」的键位（这会让打字检测卡死）：")
        for vk, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print("  VK 0x%02X  命中 %d/%d" % (vk, n, polls))
    else:
        print("没有键位持续报「按过」。")
    if down:
        print("期间处于按下状态的键位：")
        for vk, n in sorted(down.items(), key=lambda kv: -kv[1]):
            print("  VK 0x%02X  命中 %d/%d" % (vk, n, polls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())