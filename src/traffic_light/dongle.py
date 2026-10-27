# -*- coding: utf-8 -*-
"""2.4G 接收器协议 I/O：枚举接收器、19 字节包、27 包逐包 ACK、378 字节三平面缓冲。

本模块只负责「怎么发」，不决定「什么时候发」——发送时机在 wireless.py。
实现从 tools/dongle_probe/dongle_led.py 迁移（七轮真机实测通过），只改三处：

  1. 删掉 1.5 秒周期重发（hold_colors）：实测单帧可保持约 60 秒，高频重发还会丢按键；
  2. print 改成抛 DongleError，日志归调用方；
  3. 不保留写死的设备路径兜底：接收器换过至少四个实例 ID，只认现场枚举。

协议事实（docs/2.4G-LED-验证记录.md）：
  - 通道：3554:FA09 的 COL01 集合（usage page 0xFF02 / usage 0x0002），
    输出报告 ID 0x13，报告 20 字节 = 1 字节报告 ID + 19 字节包。
  - 19 字节包：
        byte0     命令号
        byte1     包总数 & 0x7F
        byte2     包序号 & 0x7F
        byte3     数据长度 & 0x0F
        byte4..17 最多 14 字节载荷
        byte18    CRC = (19 + sum(byte0..16)) & 0xFF
  - 颜色缓冲 378 字节 = 3 平面：R[0..125]、G[126..251]、B[252..377]，
    LED i 落在 i / 126+i / 252+i。按扁平 RGB 三元组发会亮在无关的键上。
  - 每包必须等接收器回 ACK（byte0 == cmd 且 byte1 高位为 0）；
    WriteFile 返回 True 只代表写进了 HID 驱动，不代表接收器收到。
  - 读回包必须用 FILE_FLAG_OVERLAPPED + 硬超时：没有数据时阻塞 ReadFile 永不返回。
  - 接收器会主动推 ActivelyReport（cmd 10），读回包时跳过。

纯函数（packet_crc / build_packet / build_color_buf / build_color_packets）不碰设备，
tests/test_dongle.py 全部离线覆盖。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
import time

REPORT_ID = 0x13
REPORT_LEN = 20
PKT_LEN = 19
CHUNK = 14
LED_SLOTS = 126
COLOR_LEN = LED_SLOTS * 3
PKG_NUM = (COLOR_LEN + CHUNK - 1) // CHUNK          # 27

CMD_SET_LED_COLORS = 2
CMD_GET_PASSWORD = 5
CMD_GET_DONGLE_STATUS = 7
CMD_ACTIVELY_REPORT = 10

VID_RECEIVER, PID_RECEIVER = 0x3554, 0xFA09

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x00000003
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DongleError(Exception):
    """接收器侧失败：打不开、无回包、某包没 ACK。统一抛这一个类型。"""


# ---- 纯函数：帧构造（无设备、无时间源） ----

def packet_crc(pkt: bytes) -> int:
    """19 字节包的 CRC：19 + byte0..16 的和，取低 8 位。"""
    return (PKT_LEN + sum(pkt[:PKT_LEN - 1])) & 0xFF


def build_packet(cmd: int, package_num: int, package_index: int,
                 data_len: int, payload: bytes = b"") -> bytes:
    """构造一个 19 字节包；payload 超过 14 字节的直接截断（协议上限）。"""
    pkt = bytearray(PKT_LEN)
    pkt[0] = cmd
    pkt[1] = package_num & 0x7F
    pkt[2] = package_index & 0x7F
    pkt[3] = data_len & 0x0F
    if payload:
        count = min(len(payload), CHUNK)
        pkt[4:4 + count] = payload[:count]
    pkt[PKT_LEN - 1] = packet_crc(bytes(pkt))
    return bytes(pkt)


def build_color_buf(leds: dict[int, tuple[int, int, int]]) -> bytes:
    """三平面颜色缓冲（378 字节）。未给出的 LED 保持 0（灭）。

    :raises ValueError: LED 索引不在 0..125，或颜色不是三个 0..255 的整数。
    """
    buf = bytearray(COLOR_LEN)
    for index, color in leds.items():
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("LED 索引必须是整数，收到 %r" % (index,))
        if not 0 <= index < LED_SLOTS:
            raise ValueError("LED 索引 %d 超出 0..%d" % (index, LED_SLOTS - 1))
        try:
            r, g, b = color
        except (TypeError, ValueError):
            raise ValueError("颜色必须是三个分量，收到 %r" % (color,)) from None
        buf[index] = int(r) & 0xFF
        buf[LED_SLOTS + index] = int(g) & 0xFF
        buf[LED_SLOTS * 2 + index] = int(b) & 0xFF
    return bytes(buf)


def build_color_packets(buf: bytes) -> list[bytes]:
    """378 字节缓冲切 27 个 SetLedColors 包，序号 0..26，CRC 已算好。"""
    if len(buf) != COLOR_LEN:
        raise ValueError("颜色缓冲必须是 %d 字节，实际 %d" % (COLOR_LEN, len(buf)))
    packets = []
    for index in range(PKG_NUM):
        offset = index * CHUNK
        payload = buf[offset:offset + CHUNK]
        packets.append(build_packet(CMD_SET_LED_COLORS, PKG_NUM, index,
                                    len(payload), payload))
    return packets


# ---- 设备路径枚举 ----

class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wt.DWORD),
                ("OffsetHigh", wt.DWORD),
                ("hEvent", wt.HANDLE)]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", _GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]


# 一律声明 argtypes/restype：靠 ctypes 猜类型时，64 位句柄会被当成 int32 截断。
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.restype = wt.BOOL
kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.ReadFile.restype = wt.BOOL
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD
kernel32.GetOverlappedResult.argtypes = [wt.HANDLE, ctypes.POINTER(OVERLAPPED),
                                         ctypes.POINTER(wt.DWORD), wt.BOOL]
kernel32.GetOverlappedResult.restype = wt.BOOL
kernel32.CancelIo.argtypes = [wt.HANDLE]
kernel32.CancelIo.restype = wt.BOOL


def enumerate_dongle_paths() -> list[str]:
    """枚举所有 3554:FA09 的 HID 接口路径。

    不依赖固定实例 ID —— 接收器换 USB 口后实例 ID 会变，
    写死的路径就失效。这里按路径里的 vid/pid 过滤，再由调用方握手验证。
    """
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    hid = ctypes.WinDLL("hid", use_last_error=True)

    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
    hid.HidD_GetHidGuid.restype = None

    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR,
                                              wt.HWND, wt.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(_GUID), wt.DWORD,
        ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wt.HANDLE, ctypes.POINTER(_SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
        wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL

    guid = _GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))

    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10
    info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if not info or info == wt.HANDLE(-1).value:
        raise DongleError("SetupDiGetClassDevsW 失败：%s"
                          % ctypes.WinError(ctypes.get_last_error()))

    cb_size = ctypes.sizeof(ctypes.c_void_p)
    tag = "vid_%04x&pid_%04x" % (VID_RECEIVER, PID_RECEIVER)
    found: list[str] = []
    try:
        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(_SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    info, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                break
            index += 1

            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info, ctypes.byref(iface), None, 0, ctypes.byref(need), None)
            if need.value == 0:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.memmove(buf, cb_size.to_bytes(4, "little"), 4)
            ok = setupapi.SetupDiGetDeviceInterfaceDetailW(
                info, ctypes.byref(iface), buf, need, None, None)
            if not ok:
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if tag in path.lower():
                found.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return found


# ---- 接收器句柄 ----

class Dongle:
    """COL01 句柄。读写都用 OVERLAPPED + 硬超时，任何情况下不会挂起。"""

    def __init__(self, path: str):
        self.path = path
        self._close_lock = threading.Lock()
        self.h = kernel32.CreateFileW(path,
                                      GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_RW, None, OPEN_EXISTING,
                                      FILE_FLAG_OVERLAPPED, None)
        if not self.h or self.h == wt.HANDLE(-1).value:
            raise DongleError("打开设备失败：%s"
                              % ctypes.WinError(ctypes.get_last_error()))

    def close(self) -> None:
        """幂等且线程安全：灯口线程可能在发帧中途被主线程叫停。

        句柄先换出再关，两个线程同时进来也只会有一个真的 CloseHandle。
        """
        with self._close_lock:
            h, self.h = self.h, None
        if h:
            kernel32.CloseHandle(h)

    def write(self, pkt: bytes, timeout_ms: int = 2000) -> bool:
        data = bytes([REPORT_ID]) + pkt
        buf = ctypes.create_string_buffer(data, REPORT_LEN)
        ov = OVERLAPPED()
        ov.hEvent = kernel32.CreateEventW(None, True, False, None)
        written = wt.DWORD(0)
        try:
            ok = kernel32.WriteFile(self.h, buf, REPORT_LEN,
                                    ctypes.byref(written), ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    return False
                if kernel32.WaitForSingleObject(ov.hEvent, timeout_ms) != WAIT_OBJECT_0:
                    kernel32.CancelIo(self.h)
                    return False
                if not kernel32.GetOverlappedResult(self.h, ctypes.byref(ov),
                                                    ctypes.byref(written), False):
                    return False
            return written.value == REPORT_LEN
        finally:
            kernel32.CloseHandle(ov.hEvent)

    def read(self, timeout_ms: int = 1000) -> bytes | None:
        """读一个 19 字节回包；超时或格式不符返回 None。"""
        buf = ctypes.create_string_buffer(64)
        ov = OVERLAPPED()
        ov.hEvent = kernel32.CreateEventW(None, True, False, None)
        got = wt.DWORD(0)
        try:
            ok = kernel32.ReadFile(self.h, buf, 64,
                                   ctypes.byref(got), ctypes.byref(ov))
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_IO_PENDING:
                    return None
                if kernel32.WaitForSingleObject(ov.hEvent, timeout_ms) != WAIT_OBJECT_0:
                    kernel32.CancelIo(self.h)
                    kernel32.WaitForSingleObject(ov.hEvent, 1000)
                    return None
                if not kernel32.GetOverlappedResult(self.h, ctypes.byref(ov),
                                                   ctypes.byref(got), False):
                    return None
            if got.value < REPORT_LEN:
                return None
            raw = buf.raw[:got.value]
            if raw[0] != REPORT_ID:
                return None
            return raw[1:20]
        finally:
            kernel32.CloseHandle(ov.hEvent)

    def read_until(self, cmd: int, timeout_ms: int) -> bytes | None:
        """在超时内等到指定命令号的回包，跳过 ActivelyReport。"""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            remain = int(max(1, (deadline - time.monotonic()) * 1000))
            pkt = self.read(min(remain, 200))
            if pkt is None or pkt[0] == CMD_ACTIVELY_REPORT:
                continue
            if pkt[0] == cmd:
                return pkt
        return None

    def simple_command(self, cmd: int, timeout_ms: int = 1500) -> bytes | None:
        if not self.write(build_packet(cmd, 1, 0, 0)):
            return None
        return self.read_until(cmd, timeout_ms)

    def send_colors(self, buf: bytes, retries: int = 5) -> None:
        """378 字节切 27 包，逐包等 ACK；任一包失败抛 DongleError。

        ACK 判定：回包 byte0 == 命令号 且 byte1 高位的「错误位」为 0。
        """
        for index, pkt in enumerate(build_color_packets(buf)):
            acked = False
            for _ in range(retries):
                if not self.write(pkt):
                    raise DongleError("包 %d 写入失败" % index)
                reply = self.read_until(CMD_SET_LED_COLORS, 900)
                if reply and (reply[1] >> 7) == 0:
                    acked = True
                    break
            if not acked:
                raise DongleError("包 %d 未收到 ACK" % index)


def open_dongle() -> Dongle:
    """现场枚举并握手，返回第一个能应答的接收器句柄。

    候选 = 现场枚举的所有 3554:FA09 接口（接收器 + 插线后的有线键盘树都在里面）；
    COL01 集合排在最前面，逐个打开并发 GetDongleStatus，第一个应答的就是它。

    先试 COL01 只是省时间：其余集合要么写不进去，要么永远不回包，
    每个都要耗掉一次超时。天线（插线后多出的设备树）不会因此漏掉——
    COL01 没应答就回到全量列表。
    """
    try:
        paths = enumerate_dongle_paths()
    except DongleError:
        raise
    except Exception as exc:
        raise DongleError("枚举 3554:FA09 接口失败：%s" % (exc,)) from exc

    preferred = [p for p in paths if "&col01" in p.lower()]
    ordered = preferred + [p for p in paths if p not in preferred]

    problems = []
    for path in ordered:
        try:
            dongle = Dongle(path)
        except DongleError as exc:
            problems.append("打不开 %s（%s）" % (path.rsplit("&", 1)[0], exc))
            continue
        try:
            replied = dongle.simple_command(CMD_GET_DONGLE_STATUS) is not None
        except Exception as exc:              # 握手期间出任何错都要把句柄收掉
            dongle.close()
            problems.append("握手出错 %s（%s）" % (path.rsplit("&", 1)[0], exc))
            continue
        if replied:
            return dongle
        dongle.close()
        problems.append("无应答 %s" % path.rsplit("&", 1)[0])

    raise DongleError(
        "找不到能应答的 2.4G 接收器。请检查：\n"
        "  1. 接收器是否插在 USB 口上（系统里没有任何 3554:FA09 设备）\n"
        "  2. 官方软件 LEOBOG ONE / OemDrv.exe 是否开着（会独占设备）\n"
        "  3. 试过的路径：\n    " + "\n    ".join(problems or ["（没有候选路径）"]))


class DongleTransport:
    """无线灯口用的传输层：打开句柄、整帧发送、关句柄。

    只有这一层碰真设备；测试注入假传输层，wireless.py 不依赖 ctypes。
    """

    def open(self):
        return open_dongle()

    def send(self, handle, buf: bytes) -> None:
        handle.send_colors(buf)

    def close(self, handle) -> None:
        handle.close()