#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过 2.4G 接收器控制 Hi86 键盘 LED。

结论：可行（2026-10-26 实测，键 1/5/9 单键亮绿、全键盘亮绿均确认）。

协议事实（来源 https://github.com/myxk1792/openrgb-rk-r87pro，
同型号接收器 3554:FA09，GPL-2.0，此处按协议事实独立实现）：
- 设备：interface 1 的 COL01 集合，usage page 0xFF02 / usage 0x0002，
  输出报告 ID 0x13，报告长度 20 字节（1 字节报告 ID + 19 字节包）。
- 19 字节包：
      byte0     命令号
      byte1     包总数 & 0x7F
      byte2     包序号 & 0x7F
      byte3     (数据长度 & 0x0F) | (board << 4)
      byte4..17 最多 14 字节载荷
      byte18    CRC = (19 + sum(byte0..16)) & 0xFF
- 颜色缓冲 378 字节 = 3 个平面：R 面 [0..125]、G 面 [126..251]、B 面 [252..377]。
  LED i 的颜色在 i、126+i、252+i。切 27 个 14 字节包。
- 每包必须等接收器回包（byte0 = 同命令号 且 byte1 高位为 0）才算成功。
  WriteFile 返回 True 只代表写进了 HID 驱动，不代表接收器收到。
- 接收器会主动推送 ActivelyReport（命令号 10），读回包时必须跳过。
- 键盘会自己回落到内置灯效，所以保持期间要每 1.5 秒重发一次颜色帧。

用法（标准权限即可，不需要管理员）：
    python dongle_led.py                      # 键 1/5/9 绿色，10 秒后熄灭
    python dongle_led.py --keys 1,2,3 --color 255,0,0 --duration 20
    python dongle_led.py --all --color 0,200,0 --duration 10
    python dongle_led.py --off                # 只发全灭帧
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

DEVICE_PATH = (r"\\?\hid#vid_3554&pid_fa09&mi_01&col01"
               r"#8&1d8973ad&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}")

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

HI86_MODEL = (0x01, 0x8D)

# 数字键名 → LED 索引。与有线协议一致（leobog-hi86-lighting 的 led_map.json）。
NUMBER_ROW_LED = {"1": 7, "2": 13, "3": 19, "4": 25, "5": 31,
                  "6": 37, "7": 43, "8": 49, "9": 55, "0": 61}

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 0x00000003
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0


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
        raise RuntimeError("SetupDiGetClassDevsW 失败：%s"
                           % ctypes.WinError(ctypes.get_last_error()))

    cb_size = ctypes.sizeof(ctypes.c_void_p)
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
            if "vid_3554" in path.lower() and "pid_fa09" in path.lower():
                found.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info)
    return found


def log(*args):
    print(*args, flush=True)


def packet_crc(pkt: bytes) -> int:
    total = PKT_LEN
    for b in pkt[:PKT_LEN - 1]:
        total += b
    return total & 0xFF


def build_packet(cmd: int, package_num: int, package_index: int,
                 data_len: int, payload: bytes = b"") -> bytes:
    pkt = bytearray(PKT_LEN)
    pkt[0] = cmd
    pkt[1] = package_num & 0x7F
    pkt[2] = package_index & 0x7F
    pkt[3] = data_len & 0x0F
    if payload:
        pkt[4:4 + len(payload)] = payload[:CHUNK]
    pkt[PKT_LEN - 1] = packet_crc(bytes(pkt))
    return bytes(pkt)


def make_color_buf(leds: dict[int, tuple[int, int, int]]) -> bytes:
    """三平面缓冲。未给出的 LED 保持 0（灭）。"""
    buf = bytearray(COLOR_LEN)
    for i, (r, g, b) in leds.items():
        if not 0 <= i < LED_SLOTS:
            raise ValueError("LED 索引 %d 超出 0..%d" % (i, LED_SLOTS - 1))
        buf[i] = r & 0xFF
        buf[LED_SLOTS + i] = g & 0xFF
        buf[LED_SLOTS * 2 + i] = b & 0xFF
    return bytes(buf)


class Dongle:
    """COL01 句柄。读写都用 OVERLAPPED + 硬超时，任何情况下不会挂起。"""

    def __init__(self, path: str = DEVICE_PATH):
        self.h = kernel32.CreateFileW(path,
                                      GENERIC_READ | GENERIC_WRITE,
                                      FILE_SHARE_RW, None, OPEN_EXISTING,
                                      FILE_FLAG_OVERLAPPED, None)
        if not self.h or self.h == wt.HANDLE(-1).value:
            raise RuntimeError("打开设备失败：%s"
                               % ctypes.WinError(ctypes.get_last_error()))

    def close(self):
        if self.h:
            kernel32.CloseHandle(self.h)
            self.h = None

    def write(self, pkt: bytes, timeout_ms: int = 2000):
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

    def read(self, timeout_ms: int = 1000):
        """读一个 19 字节回包；超时返回 None。"""
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

    def read_until(self, cmd: int, timeout_ms: int):
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

    def simple_command(self, cmd: int, timeout_ms: int = 1500):
        if not self.write(build_packet(cmd, 1, 0, 0)):
            return None
        return self.read_until(cmd, timeout_ms)

    def send_colors(self, buf: bytes, retries: int = 5) -> bool:
        """378 字节切 27 包，逐包等 ACK。任一包失败即返回 False。"""
        for index in range(PKG_NUM):
            offset = index * CHUNK
            data_len = min(CHUNK, COLOR_LEN - offset)
            pkt = build_packet(CMD_SET_LED_COLORS, PKG_NUM, index,
                               data_len, buf[offset:offset + data_len])
            acked = False
            for _ in range(retries):
                if not self.write(pkt):
                    log("  包 %d 写入失败" % index)
                    return False
                reply = self.read_until(CMD_SET_LED_COLORS, 900)
                if reply and (reply[1] >> 7) == 0:
                    acked = True
                    break
            if not acked:
                log("  包 %d 未收到 ACK" % index)
                return False
        return True

    def hold_colors(self, buf: bytes, seconds: float) -> bool:
        """保持画面：每 1.5 秒重发一次，防止键盘回落到内置灯效。"""
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            if not self.send_colors(buf):
                return False
            stop = min(time.monotonic() + 1.5, start + seconds)
            while time.monotonic() < stop:
                time.sleep(0.05)
        return True

    def wait_keyboard(self, timeout_s: int = 180) -> bool:
        """等键盘和接收器连上（GetDongleStatus 回包 byte4 非 0）。"""
        log("等待键盘连上接收器 …（按一下键盘任意键唤醒）")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            reply = self.simple_command(CMD_GET_DONGLE_STATUS)
            if reply is not None and reply[4] != 0:
                log("键盘已连接")
                return True
            time.sleep(1.0)
        log("等待超时：键盘一直未连接")
        return False


def open_dongle():
    """找到能应答的接收器句柄。

    候选路径 = 现场枚举的 3554:FA09 接口 + 已知路径兜底；
    逐个打开并握手，第一个应答的就是 COL01。这样换 USB 口也不会失效。
    """
    try:
        paths = enumerate_dongle_paths()
    except Exception:
        paths = []
    if DEVICE_PATH not in paths:
        paths.append(DEVICE_PATH)

    problems = []
    for path in paths:
        try:
            dongle = Dongle(path)
        except RuntimeError as exc:
            problems.append("打不开 %s（%s）" % (path.rsplit("&", 1)[0], exc))
            continue
        if dongle.simple_command(CMD_GET_DONGLE_STATUS) is not None:
            dongle.path = path
            return dongle
        dongle.close()
        problems.append("无应答 %s" % path.rsplit("&", 1)[0])

    raise RuntimeError(
        "找不到能应答的 2.4G 接收器。请检查：\n"
        "  1. 接收器是否插在 USB 口上（现在系统里没有任何 3554:FA09 设备）\n"
        "  2. 官方软件 LEOBOG ONE / OemDrv.exe 是否开着（会独占设备）\n"
        "  3. 试过的路径：\n    " + "\n    ".join(problems or ["（没有候选路径）"]))


def parse_color(text: str) -> tuple[int, int, int]:
    parts = [int(x) for x in text.split(",")]
    if len(parts) != 3:
        raise ValueError("颜色必须是三个整数，例如 0,200,0")
    return parts[0] & 0xFF, parts[1] & 0xFF, parts[2] & 0xFF


def main() -> int:
    ap = argparse.ArgumentParser(description="2.4G 接收器控制 Hi86 LED")
    ap.add_argument("--keys", default="1,5,9", help="数字键，逗号分隔，例如 1,5,9")
    ap.add_argument("--color", default="0,200,0", help="RGB，例如 0,200,0")
    ap.add_argument("--duration", type=float, default=10.0, help="保持秒数")
    ap.add_argument("--all", action="store_true", help="点亮全部 126 个 LED")
    ap.add_argument("--off", action="store_true", help="只发全灭帧")
    args = ap.parse_args()

    try:
        dongle = open_dongle()
    except RuntimeError as exc:
        log("失败：%s" % exc)
        return 1

    try:
        log("设备已打开")
        status = dongle.simple_command(CMD_GET_DONGLE_STATUS)
        if status is None:
            log("失败：接收器无回包")
            return 1
        if status[4] == 0:
            if not dongle.wait_keyboard():
                return 1
        else:
            log("键盘已连接")

        password = dongle.simple_command(CMD_GET_PASSWORD)
        if password:
            model = (password[8], password[9])
            log("型号 0x%02X%02X %s" % (password[8], password[9],
                                        "（Hi86，匹配）" if model == HI86_MODEL
                                        else "（与 Hi86 不符）"))

        black = make_color_buf({})

        if args.off:
            log("发送全灭帧 …")
            return 0 if dongle.send_colors(black) else 1

        r, g, b = parse_color(args.color)
        if args.all:
            leds = {i: (r, g, b) for i in range(LED_SLOTS)}
            label = "全部 126 个 LED"
        else:
            names = [x.strip() for x in args.keys.split(",")]
            unknown = [x for x in names if x not in NUMBER_ROW_LED]
            if unknown:
                log("失败：未知数字键 %s（合法：1..9、0）" % unknown)
                return 1
            leds = {NUMBER_ROW_LED[x]: (r, g, b) for x in names}
            label = "键 %s" % "/".join(names)

        log("点亮 %s，RGB(%d,%d,%d)，保持 %.0f 秒 …" % (label, r, g, b, args.duration))
        if not dongle.hold_colors(make_color_buf(leds), args.duration):
            log("发送失败")
            dongle.send_colors(black)
            return 1

        log("熄灭")
        if not dongle.send_colors(black):
            log("全灭帧发送失败")
            return 1
        log("完成")
        return 0
    finally:
        dongle.close()


if __name__ == "__main__":
    sys.exit(main())
