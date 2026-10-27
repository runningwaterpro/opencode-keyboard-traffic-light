r"""Hi86 键盘设备层：本仓库唯一允许打开设备句柄的文件。

红线（数值来自 leobog-hi86-lighting 的实机验证）：
  1. 只认厂商配置通道：VID 0x258A + PID 0x010C + UsagePage 0xFF00 + Usage 0x0001 +
     FeatureReportByteLength 520 + 路径含 COL06，六项全中才算数。有线 Hi86 上有三个
     0xFF00/0x0001 的集合（特征长度 0 / 6 / 520），前四项完全相同，只有特征长度能把
     它们分开，COL06 只是第二重锁定。
  2. 独占打开（share=0）抢不到就报错退出，不抢占：同一时刻只允许一个程序控制灯光。
  3. 只发实时数据对象（路由字节 0x06 + 对象号 0x08）。发送前自检，不合格一个字节都不发。
  4. 一次失败就停：本模块不做重试，不做循环重发。

只读枚举复用 D:\dev\leobog-hi86-lighting\tools\hid_enum.py 的实机验证手法：
  - SetupDiGetDeviceInterfaceDetailW 的 cbSize 必须填 8（x64）/ 4（x86）：填的是结构体
    大小，不是缓冲区大小。填错 API 直接返回 FALSE。
  - 该调用成功后 DevicePath 在缓冲区偏移 4 处，不是 cbSize 那个偏移。读错偏移会得到
    少一个前导反斜杠的残缺路径，后续 CreateFile 静默失败。

仅 Windows，纯标准库（ctypes）。tests/test_device.py 全离线：只测纯逻辑，
不调用 list_devices / open_exclusive / write_feature。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

MODE_WIRED = "wired"
MODE_24G = "2.4g"
MODE_NONE = "none"

VID_WIRED, PID_WIRED = 0x258A, 0x010C
VID_RECEIVER, PID_RECEIVER = 0x3554, 0xFA09

CONFIG_USAGE_PAGE, CONFIG_USAGE = 0xFF00, 0x0001
FRAME_LENGTH = 520

# 第二重锁定：设备路径里必须有这个集合编号（大小写不敏感）
CONFIG_COLLECTION_TAG = "col06"
ROUTE_BYTE = 0x06
OBJECT_LIVE_DATA = 0x08

# 官方程序：它在运行时禁止本程序独占设备
OEMDRV_EXE = "OemDrv.exe"

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
HIDP_STATUS_SUCCESS = 0x00110000


class DeviceError(Exception):
    """设备层失败：枚举、通道复核、独占打开、发包。全部抛这一个异常类型。"""


@dataclass(frozen=True)
class HidDeviceInfo:
    """一个 HID 集合的只读快照。feature_length 就是 FeatureReportByteLength。"""

    path: str
    vid: int
    pid: int
    usage_page: int
    usage: int
    feature_length: int
    input_length: int = 0
    output_length: int = 0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wt.ULONG), ("VendorID", wt.USHORT),
                ("ProductID", wt.USHORT), ("VersionNumber", wt.USHORT)]


class HIDP_CAPS(ctypes.Structure):
    """HIDP_CAPS：只用前三个 USHORT 和 FeatureReportByteLength，其余字段照抄防错位。"""

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


class PROCESSENTRY32W(ctypes.Structure):
    """PROCESSENTRY32W：只用 szExeFile，字段顺序与大小必须与 Win32 完全一致。"""

    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * MAX_PATH)]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)

# 一律声明 argtypes/restype：靠 ctypes 猜类型时，64 位句柄可能被当成 int32 截断
hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_GetHidGuid.restype = None
hid.HidD_GetAttributes.argtypes = [wt.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
hid.HidD_GetAttributes.restype = wt.BOOLEAN
hid.HidD_GetPreparsedData.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hid.HidD_GetPreparsedData.restype = wt.BOOLEAN
hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hid.HidD_FreePreparsedData.restype = None
hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hid.HidP_GetCaps.restype = wt.ULONG
hid.HidD_SetFeature.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
hid.HidD_SetFeature.restype = wt.BOOLEAN

setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND,
                                          wt.DWORD]
setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wt.HANDLE, wt.DWORD, ctypes.POINTER(GUID), wt.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiEnumDeviceInterfaces.restype = wt.BOOL
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wt.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wt.BOOL
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
setupapi.SetupDiDestroyDeviceInfoList.restype = wt.BOOL

kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wt.BOOL
kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wt.BOOL


def _device_paths() -> list[str]:
    """枚举全部 HID 接口的设备路径。只读：不打开句柄、不碰设备。

    两条坑（实机验证过，见模块开头）：cbSize 填结构体大小 8/4；
    DevicePath 在缓冲区偏移 4 处。
    """
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    info_set = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                             DIGCF_DEVICEINTERFACE | DIGCF_PRESENT)
    if not info_set or info_set == INVALID_HANDLE_VALUE:
        raise DeviceError("SetupDiGetClassDevsW 失败：%s"
                          % ctypes.WinError(ctypes.get_last_error()))
    cb_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
    paths: list[str] = []
    try:
        index = 0
        while True:
            iface = SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    info_set, 0, ctypes.byref(guid), index, ctypes.byref(iface)):
                break  # ERROR_NO_MORE_ITEMS：枚举结束
            index += 1
            need = wt.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                info_set, ctypes.byref(iface), None, 0, ctypes.byref(need), None)
            if need.value == 0:
                continue  # 连长度都拿不到，跳过这个接口
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
    return paths


def _probe(path: str) -> HidDeviceInfo | None:
    """只读探测一个 HID 集合：CreateFileW 请求 0 权限 + 共享读写，探测完立刻关。

    只调 HidD_GetAttributes / HidD_GetPreparsedData / HidP_GetCaps，
    不引用任何写设备的 API。任何一步失败返回 None（调用方跳过这个集合）。
    """
    handle = kernel32.CreateFileW(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    try:
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
        if not hid.HidD_GetAttributes(handle, ctypes.byref(attrs)):
            return None
        preparsed = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            return None
        try:
            caps = HIDP_CAPS()
            if hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) != HIDP_STATUS_SUCCESS:
                return None
            return HidDeviceInfo(path=path, vid=attrs.VendorID, pid=attrs.ProductID,
                                 usage_page=caps.UsagePage, usage=caps.Usage,
                                 feature_length=caps.FeatureReportByteLength,
                                 input_length=caps.InputReportByteLength,
                                 output_length=caps.OutputReportByteLength)
        finally:
            hid.HidD_FreePreparsedData(preparsed)
    finally:
        kernel32.CloseHandle(handle)


def list_devices() -> list[HidDeviceInfo]:
    """枚举全部 HID 集合的只读信息（VID/PID/UsagePage/Usage/特征长度/路径）。

    每个集合用 0 权限打开一次做查询，探测完立刻关闭，不读也不写。
    枚举到 0 个接口视为失败：正常机器上总有 HID 接口，空结果说明枚举坏了，
    不能静默返回空列表，否则「没插键盘」和「枚举失败」分不开。
    """
    paths = _device_paths()
    if not paths:
        raise DeviceError("HID 接口枚举为空：SetupAPI 没返回任何设备路径")
    devices = []
    for path in paths:
        info = _probe(path)
        if info is not None:
            devices.append(info)
    return devices


def validate_channel(info: HidDeviceInfo) -> tuple[bool, str | None]:
    """六项复核配置通道：VID、PID、UsagePage、Usage、特征长度、COL06。

    返回 (True, None) 表示可以安全发包；任何一项不符返回 (False, 哪一项不符)。
    前四项分不开有线 Hi86 的三个 0xFF00/0x0001 集合，特征长度 + COL06 才是判据。
    """
    if info.vid != VID_WIRED:
        return False, "VID 不符：期望 0x%04X，实际 0x%04X" % (VID_WIRED, info.vid)
    if info.pid != PID_WIRED:
        return False, "PID 不符：期望 0x%04X，实际 0x%04X" % (PID_WIRED, info.pid)
    if info.usage_page != CONFIG_USAGE_PAGE:
        return False, "UsagePage 不符：期望 0x%04X，实际 0x%04X" % (
            CONFIG_USAGE_PAGE, info.usage_page)
    if info.usage != CONFIG_USAGE:
        return False, "Usage 不符：期望 0x%04X，实际 0x%04X" % (
            CONFIG_USAGE, info.usage)
    if info.feature_length != FRAME_LENGTH:
        return False, "FeatureReportByteLength 不符：期望 %d，实际 %d" % (
            FRAME_LENGTH, info.feature_length)
    if CONFIG_COLLECTION_TAG not in info.path.lower():
        return False, "设备路径不含 COL06：%s" % info.path
    return True, None


def find_config_channel(devices: list[HidDeviceInfo] | None = None) -> HidDeviceInfo | None:
    """在设备列表里找厂商配置通道；找不到返回 None。

    devices 只传 None 才现场枚举（传 [] 就是空列表，不再枚举）。
    """
    if devices is None:
        devices = list_devices()
    for info in devices:
        if validate_channel(info)[0]:
            return info
    return None


def detect_mode(devices: list[HidDeviceInfo] | None = None) -> str:
    """判断连接模式：有配置通道 → wired；只有 2.4G 接收器 → 2.4g；都没有 → none。

    枚举失败（list_devices 抛 DeviceError）不在这里吞掉：向上抛，调用方决定怎么办。
    """
    if devices is None:
        devices = list_devices()
    if find_config_channel(devices) is not None:
        return MODE_WIRED
    if any(d.vid == VID_RECEIVER and d.pid == PID_RECEIVER for d in devices):
        return MODE_24G
    return MODE_NONE


def open_exclusive(info: HidDeviceInfo) -> int:
    """独占打开配置通道，返回句柄；失败抛 DeviceError。

    share=0：官方程序持有时抢不到，直接报错，不抢占、不重试。
    打开前重做一遍六项复核：绝不把句柄交到没确认过的集合上。
    """
    ok, reason = validate_channel(info)
    if not ok:
        raise DeviceError("拒绝独占打开，通道复核未通过（%s）" % reason)
    handle = kernel32.CreateFileW(info.path, GENERIC_READ | GENERIC_WRITE, 0, None,
                                  OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise DeviceError("独占打开失败（可能被官方程序或其他灯效软件占用）：%s；%s"
                          % (info.path, ctypes.WinError(ctypes.get_last_error())))
    return handle


def check_frame(frame: bytes) -> None:
    """发包前的纯自检，不碰任何 API：长度 520、路由字节 0x06、对象号 0x08。

    不合格抛 DeviceError。这是发包前的最后一道闸：没通过就一个字节都不发。
    """
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise DeviceError("帧必须是 bytes / bytearray / memoryview，实际 %s"
                          % type(frame).__name__)
    if len(frame) != FRAME_LENGTH:
        raise DeviceError("帧长必须是 %d 字节，实际 %d 字节" % (FRAME_LENGTH, len(frame)))
    if frame[0] != ROUTE_BYTE:
        raise DeviceError("路由字节必须是 0x%02X，实际 0x%02X" % (ROUTE_BYTE, frame[0]))
    if frame[1] != OBJECT_LIVE_DATA:
        raise DeviceError("对象号必须是 0x%02X，实际 0x%02X（只允许实时数据）"
                          % (OBJECT_LIVE_DATA, frame[1]))


def write_feature(handle: int, frame: bytes) -> None:
    """写一帧 520 字节特征报告。本函数是全仓库唯一调用 HidD_SetFeature 的地方。

    顺序固定：先 check_frame 自检，通过才发；失败抛 DeviceError，不重试、不循环。
    """
    check_frame(frame)
    buf = ctypes.create_string_buffer(bytes(frame), FRAME_LENGTH)
    if not hid.HidD_SetFeature(handle, buf, FRAME_LENGTH):
        raise DeviceError("HidD_SetFeature 失败：%s"
                          % ctypes.WinError(ctypes.get_last_error()))


def close(handle: int) -> None:
    """关闭句柄。故意不抛异常：本函数通常在 finally 里收尾，抛错会盖住原始错误。

    None / 0 / INVALID_HANDLE_VALUE 一律当作「本来就没有句柄」，直接返回。
    """
    if not handle or handle == INVALID_HANDLE_VALUE:
        return
    kernel32.CloseHandle(handle)


def _process_names() -> list[str]:
    """快照当前全部进程的可执行文件名（szExeFile 是纯文件名，不含路径）。只读。"""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise DeviceError("CreateToolhelp32Snapshot 失败：%s"
                          % ctypes.WinError(ctypes.get_last_error()))
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        names: list[str] = []
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise DeviceError("Process32FirstW 失败：%s"
                              % ctypes.WinError(ctypes.get_last_error()))
        while True:
            names.append(entry.szExeFile)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return names
    finally:
        kernel32.CloseHandle(snapshot)


def _match_process(names, target: str) -> bool:
    """进程名匹配：忽略大小写、要求全名相等（不是子串匹配）。"""
    wanted = target.lower()
    return any(str(name).lower() == wanted for name in names)


def oemdrv_running() -> bool:
    """官方程序 OemDrv.exe 是否在跑：在跑就不许独占设备（红线：不抢占）。

    枚举失败抛 DeviceError，不返回 False：假阴性会导致我们去抢设备。
    """
    return _match_process(_process_names(), OEMDRV_EXE)
