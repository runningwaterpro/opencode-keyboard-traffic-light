# -*- coding: utf-8 -*-
"""用户活动：判断「用户现在在不在打字」。

两个信号，用途不同：

- `idle_seconds()`（GetLastInputInfo）：键盘或鼠标最后一次输入距今多久。
  把鼠标也算进去——用鼠标看网页时它一直很小，无线灯口就一直不发帧，
  60 秒的绿灯会整个错过。所以**不能**拿它当「在打字」的判据。
- `keyboard_idle_seconds()`（轮询 GetAsyncKeyState）：只看键盘。
  用鼠标浏览时键盘空闲照常增长，该发的帧照发；真的在打字时它才归零。

方向性取舍不变：宁可少发帧（看不见），不可多发帧（打出重复字符）。

不用全局键盘钩子：钩子要抢输入、要权限、还可能被安全软件拦；
轮询只是每半秒扫一遍键位状态，一个只读查询。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wt.BOOL
# GetTickCount 在 kernel32，不在 user32；dwTime 就是它的 32 位毫秒计数。
kernel32.GetTickCount.argtypes = []
kernel32.GetTickCount.restype = wt.DWORD

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

# VK 0x01..0x06 是鼠标键；从 0x08（退格）扫到 0xFF。
VK_SCAN_FIRST, VK_SCAN_LAST = 0x08, 0xFF

_keyboard_last_ms: float | None = None


def idle_seconds() -> float:
    """距最后一次用户输入（键盘或鼠标）的秒数；查询失败抛 OSError。

    dwTime 与 GetTickCount 都是 32 位毫秒计数（49.7 天回绕），
    所以相减后必须再取一次 32 位掩码，否则回绕时会算出一个巨大的负数。
    """
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo 失败：%s"
                      % ctypes.WinError(ctypes.get_last_error()))
    return ((int(kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0


def _key_was_pressed(vk: int) -> bool:
    """该键自上次调用以来是否被按过（GetAsyncKeyState 最低位）。

    单独成函数是为了测试能替换掉真实的 Win32 调用。
    """
    return bool(user32.GetAsyncKeyState(vk) & 0x0001)


def keyboard_idle_seconds() -> float:
    """距上一次「任意键盘按键」的秒数；鼠标移动不算。

    GetAsyncKeyState 的最低位表示「自上次调用以来按过」，每次调用都会清掉，
    所以必须持续轮询才追得回来。轮询间隔 0.5 秒，远小于打字节奏，不会漏。

    刚启动还没有见过任何按键时，按「刚输入过」处理：先观察 15 秒再发帧，
    宁可晚亮，不在用户可能正要打字的第一秒抢链路。
    """
    global _keyboard_last_ms
    now = time.monotonic() * 1000.0
    if _keyboard_last_ms is None:
        _keyboard_last_ms = now
    if any(_key_was_pressed(vk) for vk in range(VK_SCAN_FIRST, VK_SCAN_LAST + 1)):
        _keyboard_last_ms = now
    return (now - _keyboard_last_ms) / 1000.0


def _reset_for_tests() -> None:
    """测试用：清掉「上次按键时刻」，让每个用例从干净状态开始。"""
    global _keyboard_last_ms
    _keyboard_last_ms = None