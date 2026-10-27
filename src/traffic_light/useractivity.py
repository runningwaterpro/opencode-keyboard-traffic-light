# -*- coding: utf-8 -*-
"""系统空闲时间：距最后一次键盘/鼠标输入的秒数（GetLastInputInfo）。

用系统空闲时间而不是全局键盘钩子：钩子要抢输入、要权限、还可能被安全软件拦；
空闲时间只需要一个只读查询。

方向性取舍：鼠标活动也算「在输入」，会让补帧晚一点甚至错过一轮。
这个方向是安全的——少发帧只会看不见，不会丢键；反过来才会打出重复字符。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.GetLastInputInfo.restype = wt.BOOL
# GetTickCount 在 kernel32，不在 user32；dwTime 就是它的 32 位毫秒计数。
kernel32.GetTickCount.argtypes = []
kernel32.GetTickCount.restype = wt.DWORD


def idle_seconds() -> float:
    """距最后一次用户输入的秒数；查询失败抛 OSError。

    dwTime 与 GetTickCount 都是 32 位毫秒计数（49.7 天回绕），
    所以相减后必须再取一次 32 位掩码，否则回绕时会算出一个巨大的负数。
    """
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo 失败：%s"
                      % ctypes.WinError(ctypes.get_last_error()))
    return ((int(kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF) / 1000.0