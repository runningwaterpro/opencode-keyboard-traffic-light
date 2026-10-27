# -*- coding: utf-8 -*-
"""2.4G 接收器（3554:FA09，CX 2.4G）只读/最小写入探测。

一次性技术验证，不进守护、不进 src。

协议来源：RK R87 Pro 的 2.4G 接收器协议（同为 3554:FA09），
见 https://github.com/myxk1792/openrgb-rk-r87pro 的 docs/rf-dongle-protocol.md
与 src/R87ProDongle.{h,cpp}。不复制其 GPL 代码，只按协议事实实现。

包格式（输出报告 ID 0x13，数据 19 字节）：

    byte  0     命令号
    byte  1     127 & packageNum
    byte  2     127 & packageIndex
    byte  3     (15 & dataLength) | (board << 4)
    byte 4..17  最多 14 字节载荷
    byte 18     CRC = (19 + sum(byte 0..16)) & 0xFF

ACK：回包 byte 1 的 bit 7 为 0 即接受。
颜色：SetLedColors 载荷是 126*3 平面缓冲：先 126 个 R，再 126 个 G，再 126 个 B。

命令号：2 SetLedColors  5 GetPassword  6 SetFactory  7 GetDongleStatus
        66 GetLedColors  10 ActivelyReport（接收器 → 主机，忽略）

用法（管理员终端）：
    python probe_dongle.py            # 第 1 步：握手（只读）
    python probe_dongle.py --led      # 第 2 步：试点（发一帧、保持 5 秒、自动还原）
    python probe_dongle.py --restore  # 只写回备份缓冲区
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import time

VID, PID = 0x3554, 0xFA09
USAGE_PAGE, USAGE = 0xFF02, 0x0002
REPORT_ID = 0x13
REPORT_LEN = 20          # 1（报告 ID）+ 19
PKT_LEN = 19
CHUNK = 14
LED_SLOTS = 126
COLOR_LEN = LED_SLOTS * 3

CMD_SET_LED_COLORS = 2
CMD_GET_PASSWORD = 5
CMD_GET_DONGLE_STAT = 7
CMD_GET_LED_COLORS = 66
CMD_ACTIVELY_REPORT = 10

HI86_MODEL = (0x01, 0x8D)

BACKUP_FILE = "colors_backup.json"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)

kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.ReadFile.restype = wt.BOOL

hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(ctypes.c_uint8 * 16)]
hid.HidD_GetHidGuid.restype = None

setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.HWND,
                                          wt.DWORD]
setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [wt.HANDLE, wt.DWORD,
                                                 ctypes.c_void_p, wt.DWORD,
                                                 ctypes.c_void_p]
setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                                      ctypes.c_void_p, wt.DWORD,
                                                      ctypes.POINTER(wt.DWORD),
                                                      ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

class GUID_(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA_(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID_),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


def find_col01_path() -> str:
    """枚举出 3554:FA09 且 usage_page=0xFF02 的集合路径。只读。"""
    guid = GUID_()
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID_)]
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID_), wt.LPCWSTR,
                                               wt.HWND, wt.DWORD]
    info_set = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                              DIGCF_DEVICEINTERFACE | DIGCF_PRESENT)
    if not info_set or info_set == wt.HANDLE(-1).value:
        raise RuntimeError("SetupDiGetClassDevsW 失败：%s"
                           % ctypes.WinError(ctypes.get_last_error()))
    cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
    paths = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA_()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA_)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    info_set, 0, ctypes.byref(guid), index, ctypes.byref(iface)):
                break
            index += 1
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set, ctypes.byref(iface), None, 0, ctypes.byref(need), None)
            if need.value == 0:
                continue
            buf = ctypes.create_string_buffer(need.value)
            ctypes.memmove(buf, cb_size.to_bytes(4, "little"), 4)
            base = ctypes.cast(buf, ctypes.c_void_p)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    info_set, ctypes.byref(iface), base, need, None, None):
                continue
            path = ctypes.wstring_at(base.value + 4)
            if path.startswith("\\\\?\\"):
                paths.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(info_set)
    for path in paths:
        handle = kernel32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                      None, OPEN_EXISTING, 0, None)
        if not handle or handle == wt.HANDLE(-1).value:
            continue
        try:
            attrs = HIDD_ATTRIBUTES()
            attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
                continue
            if attrs.VendorID != VID or attrs.ProductID != PID:
                continue
            preparsed = ctypes.c_void_p()
            if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
                continue
            try:
                caps = HIDD_CAPS()
                if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != 0x00110000:
                    continue
                if caps.UsagePage == USAGE_PAGE and caps.Usage == USAGE \
                        and caps.OutputReportByteLength == REPORT_LEN:
                    return path
            finally:
                hid.HidD_FreePreparsedData(preparsed)
        finally:
            kernel32.CloseHandle(handle)
    raise RuntimeError("未找到 3554:FA09 的 0xFF02/0x0002 集合（20 字节输出）")


class HIDD_CAPS(ctypes.Structure):
    """HIDP_CAPS：只取前五个 USHORT，其余按文档补齐。"""
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT),
                ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT),
                ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17),
                ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT),
                ("NumberInputValueCaps", wt.USHORT),
                ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT),
                ("NumberOutputValueCaps", wt.USHORT),
                ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT),
                ("NumberFeatureValueCaps", wt.USHORT),
                ("NumberFeatureDataIndices", wt.USHORT)]


class Ctx:
    """一个打开的 COL01 句柄 + 非阻塞读。句柄不共享给别的进程。"""

    def __init__(self, path: str):
        self.path = path
        self.handle = kernel32.CreateFileW(
            path, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if not self.handle or self.handle == wt.HANDLE(-1).value:
            raise RuntimeError("独占打开失败（可能被官方程序占用）：%s；%s"
                               % (path, ctypes.WinError(ctypes.get_last_error())))
        self._queue: list[bytes] = []
        self._reader = None

    def start_reader(self) -> None:
        import threading
        buf = ctypes.create_string_buffer(REPORT_LEN + 16)
        got = wt.DWORD(0)

        def _run():
            buf_size = ctypes.sizeof(buf)
            while True:
                got.value = 0
                ok = kernel32.ReadFile(self.handle, buf, buf_size, ctypes.byref(got), None)
                if not ok:
                    err = ctypes.get_last_error()
                    if err in (2, 231):  # ERROR_BROKEN_PIPE / ERROR_SEEK_ON_NAMED_PIPE
                        return
                    time.sleep(0.05)
                    continue
                self._queue.append(bytes(buf[:got.value]))

        self._reader = threading.Thread(target=_run, daemon=True, name="ding-reader")
        self._reader.start()

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def poll(self, timeout_ms: int) -> list[bytes]:
        """取回队列里的全部 20 字节报告（含报告 ID）。"""
        deadline = time.monotonic() + timeout_ms / 1000.0
        seen: list[bytes] = []
        while time.monotonic() < deadline:
            if self._queue:
                seen.append(self._queue.pop(0))
            else:
                time.sleep(min(0.01, max(0.001, deadline - time.monotonic())))
        return seen


def crc(data: bytes) -> int:
    total = PKT_LEN
    for b in data[:PKT_LEN - 1]:
        total += b
    return total & 0xFF


def build_packet(cmd: int, board: int, package_num: int, package_index: int,
                 data_len: int, payload: bytes = b"") -> bytes:
    pkt = bytearray(PKT_LEN)
    pkt[0] = cmd
    pkt[1] = 0x7F & package_num
    pkt[2] = 0x7F & package_index
    pkt[3] = (0x0F & data_len) | ((board << 4) & 0xF0)
    if payload:
        pkt[4:4 + len(payload)] = payload[:CHUNK]
    pkt[PKT_LEN - 1] = crc(bytes(pkt))
    return bytes(pkt)


kernel32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                               ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
kernel32.WriteFile.restype = wt.BOOL


def send_report(ctx: Ctx, pkt: bytes) -> bool:
    buf = ctypes.create_string_buffer(bytes([REPORT_ID]) + bytes(pkt), REPORT_LEN)
    written = wt.DWORD(0)
    ok = kernel32.WriteFile(ctx.handle, buf, REPORT_LEN, ctypes.byref(written), None)
    if not ok:
        err = ctypes.get_last_error()
        print("    WriteFile 失败：%s" % ctypes.WinError(err))
        return False
    return written.value == REPORT_LEN


def recv(ctx: Ctx, cmd: int, timeout_ms: int) -> bytes | None:
    """等一个 byte0 == cmd 且 byte1.bit7 == 0 的回包。"""
    seen = ctx.poll(timeout_ms)
    for rep in seen:
        if len(rep) >= REPORT_LEN and rep[0] == REPORT_ID:
            pkt = rep[1:]
            if pkt[0] == CMD_ACTIVELY_REPORT:
                continue
            if pkt[0] == cmd:
                if (pkt[1] >> 7) == 0:
                    return pkt
    return None


def simple_command(ctx: Ctx, cmd: int, timeout_ms: int = 600) -> bytes | None:
    """发一个无载荷包，最多重试 8 轮、每轮等 60ms。"""
    pkt = build_packet(cmd, 0, 1, 0, 0)
    for _ in range(8):
        if not send_report(ctx, pkt):
            return None
        got = recv(ctx, cmd, 60)
        if got is not None:
            return got
    return None


def get_dongle_status(ctx: Ctx):
    pkt = simple_command(ctx, CMD_GET_DONGLE_STAT)
    if pkt is None:
        return None, "无回包"
    linked = pkt[4] != 0
    return pkt, ("键盘已连接" if linked else "接收器没有连到键盘")


def get_password(ctx: Ctx):
    pkt = simple_command(ctx, CMD_GET_PASSWORD)
    if pkt is None:
        return None
    model = (pkt[8], pkt[9])
    fw = "%02X%02X" % (pkt[12], pkt[13])
    return model, fw, pkt


def read_colors(ctx: Ctx, retries: int = 3) -> bytes | None:
    """读 126 槽颜色缓冲（378 字节，1 平面）。

    按 R87 文档：GET 请求和 SET 请求同格式，byte1=packageNum、byte2=packageIndex，
    dataLength=CHUNK。每包回包同样布局，ACK 时 payload 是颜色缓冲中该段的 14 字节。
    27 个包取齐 378 字节。
    """
    out = bytearray(COLOR_LEN)
    package_num = (COLOR_LEN + CHUNK - 1) // CHUNK
    for index in range(package_num):
        data_len = min(CHUNK, COLOR_LEN - index * CHUNK)
        pkt = build_packet(CMD_GET_LED_COLORS, 0, package_num, index, data_len)
        got = None
        for _ in range(retries):
            if send_report(ctx, pkt):
                got = recv(ctx, CMD_GET_LED_COLORS, 150)
                if got is not None:
                    break
        if got is None:
            return None
        payload = got[4:4 + data_len]
        offset = index * CHUNK
        out[offset:offset + len(payload)] = payload
    return bytes(out)


def main() -> int:
    args = sys.argv[1:]
    only_restore = "--restore" in args
    do_led = "--led" in args
    do_probe = not (only_restore or do_led)

    # 已在 PowerShell 握手验证通过的 COL01 路径（3554:FA09，mi_01）
    # 如 PowerShell 所见：\\?\hid#vid_3554&pid_fa09&mi_01&col01#8&1d8973ad&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}
    # 下面把 \\?\hid#... 转成 Windows 宽路径
    KNOWN_PATH = r"\\?\hid#vid_3554&pid_fa09&mi_01&col01#8&1d8973ad&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
    path = KNOWN_PATH  # 直接使用已验证路径，不再跑枚举循环
    ctx = Ctx(path)
    ctx.start_reader()
    try:
        if only_restore:
            data = json.load(open(BACKUP_FILE, "r", encoding="utf-8"))
            buf = bytes.fromhex(data["hex"])
            ok = send_colors(ctx, buf)
            print("已写回备份缓冲：%s" % ("成功" if ok else "失败"))
            return 0 if ok else 1

        if do_probe:
            stat, msg = get_dongle_status(ctx)
            print("GetDongleStatus → %s" % msg)
            if stat is None or stat[4] == 0:
                print("接收器没有连到键盘。请确认开关在 2.4G 且键盘与接收器已配对。")
                return 1
            model, fw, raw = get_password(ctx)
            print("GetPassword → model=0x%02X%02X  fw=0x%s  raw=%s"
                  % (model[0], model[1], fw, raw.hex()))
            expect = "%02X%02X" % HI86_MODEL
            ok = model == HI86_MODEL
            print("型号判定：%s" % ("匹配 Hi86（0x%04X）" % (HI86_MODEL[0] << 8 | HI86_MODEL[1])
                                    if ok else "不匹配，期望 0x%s" % expect))
            if not ok:
                print("握手不通。停在这里，不进入试点。")
                return 1
            print("握手通过。可以继续 --led。")
            return 0

        # --led 试点：单键绿色，保持 5 秒，自动还原
        print("读取当前颜色缓冲（约 0.4 秒）…")
        backup = read_colors(ctx)
        if backup is not None:
            with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump({"hex": backup.hex()}, f)
            print("已备份当前颜色到 %s（%d 字节）" % (BACKUP_FILE, len(backup)))
        else:
            print("警告：读当前颜色失败，无备份。还原将发送全 0 缓冲。")
            backup = b"\x00" * COLOR_LEN

        buf = bytearray(COLOR_LEN)
        # 槽 0 = Esc（Hi86 的 LED 索引 0）
        buf[0] = 0
        buf[LED_SLOTS + 0] = 200
        buf[LED_SLOTS * 2 + 0] = 0
        print("发送单键试点：Esc = 绿，其余全灭。")
        ok = send_colors(ctx, bytes(buf))
        print("SetLedColors → %s。保持 5 秒，请盯着键盘。" % ("成功" if ok else "失败"))
        time.sleep(5.0)
        print("写回原颜色缓冲（约 0.4 秒）…")
        ok2 = send_colors(ctx, backup)
        print("还原 → %s。观察：Esc 是否熄灭、其余是否恢复原样？"
              % ("成功" if ok2 else "失败"))
        return 0

    finally:
        ctx.close()


def send_colors(ctx: Ctx, color_buf: bytes) -> bool:
    """把 378 字节颜色缓冲切 27 包发下去，每包等 ACK。"""
    package_num = (COLOR_LEN + CHUNK - 1) // CHUNK
    for index in range(package_num):
        offset = index * CHUNK
        data_len = min(CHUNK, COLOR_LEN - offset)
        payload = color_buf[offset:offset + data_len]
        pkt = build_packet(CMD_SET_LED_COLORS, 0, package_num, index, data_len,
                           payload.ljust(CHUNK, b"\x00"))
        acked = False
        for _ in range(10):
            if not send_report(ctx, pkt):
                return False
            got = recv(ctx, CMD_SET_LED_COLORS, 60)
            if got is not None:
                acked = (got[1] >> 7) == 0
                if acked:
                    break
        if not acked:
            print("  包 %d 未 ACK，放弃" % index)
            return False
    return True


if __name__ == "__main__":
    sys.exit(main())
