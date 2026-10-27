# -*- coding: utf-8 -*-
"""托盘图标：常驻的全局状态显示（与键盘并列的第二个显示端口）。

分两层：
  1. 纯逻辑：灯态 → 颜色 / tooltip 文本，离线可测；
  2. Win32 壳：Shell_NotifyIcon + 隐藏窗口消息循环，纯 ctypes，零依赖。

线程模型：start() 起专用线程，创建隐藏窗口并跑 GetMessage 循环；
update()/balloon() 只改共享状态并 PostMessage 唤醒，GDI 与 Shell 调用都在托盘线程。
托盘失败不影响守护：start() 内部吞异常并记 warning。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading

from .core import Light

# ---- 纯逻辑（离线可测） ----

GRAY = (128, 128, 128)

COLOR_BY_LIGHT = {
    Light.RUNNING: (128, 0, 255),
    Light.FAILED: (255, 0, 0),
    Light.WAITING: (255, 210, 0),
    Light.IDLE: (0, 200, 0),
}

STATUS_CN = {
    Light.RUNNING: "执行中",
    Light.FAILED: "失败",
    Light.WAITING: "等待",
    Light.IDLE: "完成",
}

TIP_MAX = 127


def color_of(light, *, connected: bool, session_count: int,
             dry_run: bool) -> tuple[int, int, int]:
    """图标颜色：干跑 / 未连接 / 无会话 → 灰；否则按聚合灯态。"""
    if dry_run or not connected or session_count <= 0:
        return GRAY
    return COLOR_BY_LIGHT.get(light, GRAY)


def is_blinking(light, *, connected: bool, session_count: int,
                dry_run: bool) -> bool:
    """只有“等待”才闪烁：黄/灰交替，提示需要你处理。"""
    if dry_run or not connected or session_count <= 0:
        return False
    return light is Light.WAITING


def blink_color(light, *, connected: bool, session_count: int,
                dry_run: bool, phase_on: bool) -> tuple[int, int, int]:
    """按闪烁相位取颜色：暗相位用灰，其余同 color_of。"""
    if is_blinking(light, connected=connected, session_count=session_count,
                   dry_run=dry_run) and not phase_on:
        return GRAY
    return color_of(light, connected=connected, session_count=session_count,
                    dry_run=dry_run)


def tooltip_of(light, *, connected: bool, session_count: int,
               keyboard_count: int, dry_run: bool) -> str:
    """两行 tooltip：状态 + 会话数 / 键盘数；总长不超过 127 字符。"""
    if not connected:
        head = "OpenCode：未连接"
    elif session_count <= 0:
        head = "OpenCode：无会话"
    else:
        status = STATUS_CN.get(light, "空闲")
        head = "OpenCode：%s · %d 个会话" % (status, session_count)
    if dry_run:
        head = "[干跑] " + head
    text = head + "\n键盘：%d 把" % keyboard_count
    return text[:TIP_MAX]
# ---- Win32 壳 ----

WM_APP = 0x8000
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_RBUTTONUP = 0x0205
WM_NULL = 0x0000

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3

WM_TIMER = 0x0113
BLINK_TIMER_ID = 1
BLINK_INTERVAL_MS = 500

MF_STRING = 0x0
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_BOTTOMALIGN = 0x0020
ID_QUIT, ID_LOG, ID_OPENCODE = 1001, 1002, 1003
IDC_ARROW = 32512
ERROR_CLASS_ALREADY_EXISTS = 1410

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

_libs: dict[str, object] = {}


def _lib(name: str):
    """按需加载 DLL：模块导入时不碰 Win32，纯逻辑测试可在任何环境跑。"""
    if name not in _libs:
        _libs[name] = ctypes.WinDLL(name, use_last_error=True)
    return _libs[name]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HANDLE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HANDLE),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HANDLE),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD),
                ("yHotspot", wt.DWORD), ("hbmMask", wt.HANDLE),
                ("hbmColor", wt.HANDLE)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def _build_icon(rgb: tuple[int, int, int], size: int = 32) -> int:
    """画一个带透明边的实心圆，返回 HICON（32bpp BGRA，抗锯齿）。"""
    gdi32, user32 = _lib("gdi32"), _lib("user32")

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = size
    header.biHeight = -size          # 负数 = 自上而下
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0         # BI_RGB
    bmi = BITMAPINFO()
    bmi.bmiHeader = header

    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO),
                                       wt.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.c_void_p, wt.DWORD]
    bits = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(None, ctypes.byref(bmi), 0,
                                 ctypes.byref(bits), None, 0)
    if not hbm:
        raise ctypes.WinError(ctypes.get_last_error())

    buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
    center = (size - 1) / 2.0
    radius = size / 2.0 - 1.0
    r, g, b = rgb
    for y in range(size):
        for x in range(size):
            dist = ((x - center) ** 2 + (y - center) ** 2) ** 0.5
            if dist <= radius:
                alpha = 255
            elif dist >= radius + 1.0:
                alpha = 0
            else:
                alpha = int((radius + 1.0 - dist) * 255)
            i = (y * size + x) * 4
            buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = b, g, r, alpha

    gdi32.CreateBitmap.restype = ctypes.c_void_p
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wt.UINT,
                                   wt.UINT, ctypes.c_void_p]
    zeros = (ctypes.c_ubyte * 4)()   # 32x32 单色掩码 = 4 字节，全 0
    mask = gdi32.CreateBitmap(size, size, 1, 1, ctypes.byref(zeros))

    info = ICONINFO()
    info.fIcon = True
    info.hbmMask = mask
    info.hbmColor = hbm
    user32.CreateIconIndirect.restype = ctypes.c_void_p
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
    hicon = user32.CreateIconIndirect(ctypes.byref(info))

    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject(hbm)
    gdi32.DeleteObject(mask)
    if not hicon:
        raise ctypes.WinError(ctypes.get_last_error())
    return hicon

class TrayIcon:
    """一个常驻托盘图标；update() 幂等，close() 可重复调用。"""

    _CB_MSG = WM_APP + 3

    def __init__(self, log: logging.Logger, *, on_quit, log_path=None,
                 open_target: str = "opencode://", dry_run: bool = False):
        self._log = log
        self._on_quit = on_quit
        self._log_path = str(log_path) if log_path else None
        self._open_target = open_target
        self._dry_run = dry_run

        self._lock = threading.Lock()
        self._pending = {"light": Light.OFF, "sessions": 0, "keyboards": 0,
                         "connected": False}
        self._balloons: list[tuple[str, str, str]] = []

        self._hwnd = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._wndproc = None          # 保引用：被 GC 后窗口过程会崩
        self._icons: dict[tuple[int, int, int], int] = {}
        self._added = False
        self._color = None
        self._tip = ""
        self._blink_on = True
        self._timer_on = False

    # ---- 对外接口（任意线程） ----

    @property
    def available(self) -> bool:
        return self._added

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def update(self, light, *, session_count: int, keyboard_count: int,
               connected: bool) -> None:
        state = {"light": light, "sessions": session_count,
                 "keyboards": keyboard_count, "connected": connected}
        with self._lock:
            if state == self._pending:
                return
            self._pending = state
        self._post(WM_APP + 1)

    def balloon(self, title: str, body: str, level: str = "info") -> None:
        with self._lock:
            self._balloons.append((title, body, level))
            if len(self._balloons) > 8:
                del self._balloons[0]
        self._post(WM_APP + 2)

    def close(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            _lib("user32").PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ---- 托盘线程 ----

    def _post(self, msg: int) -> None:
        hwnd = self._hwnd
        if hwnd:
            _lib("user32").PostMessageW(hwnd, msg, 0, 0)

    def _run(self) -> None:
        user32 = _lib("user32")
        try:
            self._create_window()
            self._apply()
        except Exception as exc:
            self._log.warning("托盘图标启动失败（继续无托盘运行）：%s", exc)
            self._ready.set()
            return
        self._ready.set()
        try:
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._remove_icon()
            self._destroy_icons()

    def _create_window(self) -> None:
        user32 = _lib("user32")
        kernel32 = _lib("kernel32")
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        hinst = kernel32.GetModuleHandleW(None)

        user32.LoadCursorW.restype = ctypes.c_void_p
        user32.LoadCursorW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        cursor = user32.LoadCursorW(None, ctypes.cast(ctypes.c_void_p(IDC_ARROW), wt.LPCWSTR))

        self._wndproc = WNDPROC(self._wnd_proc)
        cls = WNDCLASSW()
        cls.lpfnWndProc = self._wndproc
        cls.hInstance = hinst
        cls.hCursor = cursor
        cls.lpszClassName = "KTLTrayWindow"
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wt.WORD
        if not user32.RegisterClassW(ctypes.byref(cls)):
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(err)

        user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user32.CreateWindowExW.restype = wt.HWND
        user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, wt.HWND, wt.HANDLE,
                                           wt.HANDLE, ctypes.c_void_p]
        hwnd = user32.CreateWindowExW(0, "KTLTrayWindow", "", 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = hwnd

    def _make_nid(self) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        return nid

    def _icon_for(self, color: tuple[int, int, int]) -> int:
        icon = self._icons.get(color)
        if icon is None:
            icon = _build_icon(color)
            self._icons[color] = icon
        return icon

    def _apply(self) -> None:
        with self._lock:
            p = dict(self._pending)
        blink = is_blinking(p["light"], connected=p["connected"],
                            session_count=p["sessions"], dry_run=self._dry_run)
        self._sync_timer(blink)
        color = blink_color(p["light"], connected=p["connected"],
                            session_count=p["sessions"], dry_run=self._dry_run,
                            phase_on=self._blink_on)
        tip = tooltip_of(p["light"], connected=p["connected"],
                         session_count=p["sessions"],
                         keyboard_count=p["keyboards"], dry_run=self._dry_run)
        if self._added and color == self._color and tip == self._tip:
            return
        shell32 = _lib("shell32")
        shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wt.BOOL
        nid = self._make_nid()
        nid.uFlags = NIF_ICON | NIF_TIP
        nid.hIcon = self._icon_for(color)
        nid.szTip = tip
        if self._added:
            ok = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        else:
            nid.uFlags |= NIF_MESSAGE
            nid.uCallbackMessage = self._CB_MSG
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
            self._added = bool(ok)
        if ok:
            self._color, self._tip = color, tip
        else:
            self._log.debug("Shell_NotifyIcon 更新失败（资源管理器未就绪？）")

    def _flush_balloons(self) -> None:
        with self._lock:
            items, self._balloons = self._balloons, []
        if not items or not self._added:
            return
        shell32 = _lib("shell32")
        for title, body, level in items:
            nid = self._make_nid()
            nid.uFlags = NIF_INFO | NIF_ICON | NIF_TIP
            nid.hIcon = self._icon_for(self._color or GRAY)
            nid.szTip = self._tip
            nid.szInfo = body[:255]
            nid.szInfoTitle = title[:63]
            nid.dwInfoFlags = {"warning": NIIF_WARNING,
                               "error": NIIF_ERROR}.get(level, NIIF_INFO)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _remove_icon(self) -> None:
        if not self._added or not self._hwnd:
            return
        shell32 = _lib("shell32")
        nid = self._make_nid()
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self._added = False

    def _destroy_icons(self) -> None:
        user32 = _lib("user32")
        user32.DestroyIcon.argtypes = [ctypes.c_void_p]
        for icon in self._icons.values():
            user32.DestroyIcon(icon)
        self._icons.clear()

    def _sync_timer(self, blink: bool) -> None:
        """等待态开 500ms 定时器，其余状态关掉；所有调用都在托盘线程。"""
        user32 = _lib("user32")
        user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT,
                                    ctypes.c_void_p]
        user32.SetTimer.restype = ctypes.c_size_t
        user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
        if blink and not self._timer_on:
            self._blink_on = True
            self._timer_on = bool(user32.SetTimer(self._hwnd, BLINK_TIMER_ID,
                                                  BLINK_INTERVAL_MS, None))
        elif not blink and self._timer_on:
            user32.KillTimer(self._hwnd, BLINK_TIMER_ID)
            self._timer_on = False
            self._blink_on = True

    def _stop_timer(self) -> None:
        if not self._timer_on:
            return
        user32 = _lib("user32")
        user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
        user32.KillTimer(self._hwnd, BLINK_TIMER_ID)
        self._timer_on = False
        self._blink_on = True

    def _show_menu(self, hwnd) -> None:
        user32 = _lib("user32")
        user32.CreatePopupMenu.restype = wt.HANDLE
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW.argtypes = [wt.HANDLE, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
        user32.AppendMenuW(menu, MF_STRING, ID_LOG, "打开日志")
        user32.AppendMenuW(menu, MF_STRING, ID_OPENCODE, "打开 OpenCode")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "退出")

        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        user32.TrackPopupMenu.restype = wt.UINT
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.PostMessageW(hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self._on_command(cmd)

    def _on_command(self, cmd: int) -> None:
        if cmd == ID_QUIT:
            try:
                self._on_quit()
            except Exception:
                self._log.debug("退出回调失败", exc_info=True)
        elif cmd == ID_LOG:
            self._open(self._log_path)
        elif cmd == ID_OPENCODE:
            self._open(self._open_target)

    def _open(self, target) -> None:
        if not target:
            return
        try:
            os.startfile(target)
        except OSError as exc:
            self._log.warning("打开 %s 失败：%s", target, exc)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        user32 = _lib("user32")
        try:
            if msg == WM_APP + 1:
                self._apply()
                return 0
            if msg == WM_APP + 2:
                self._flush_balloons()
                return 0
            if msg == self._CB_MSG:
                if lparam == WM_RBUTTONUP:
                    self._show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                self._on_command(wparam & 0xFFFF)
                return 0
            if msg == WM_TIMER and wparam == BLINK_TIMER_ID:
                self._blink_on = not self._blink_on
                self._apply()
                return 0
            if msg == WM_CLOSE:
                self._stop_timer()
                self._remove_icon()
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            self._log.debug("托盘消息处理失败", exc_info=True)
            return 0
        user32.DefWindowProcW.restype = LRESULT
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)