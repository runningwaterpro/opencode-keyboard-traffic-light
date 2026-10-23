"""设备层测试：全离线，绝不碰真设备。

本文件不调用 list_devices / open_exclusive / write_feature 的真实现，
只测纯逻辑：帧自检、通道复核、模式判定、进程名匹配。
需要「证明没调用 API」的地方，把 device 模块里的 DLL 对象换成一碰就报错的桩。
"""
import pytest

from traffic_light import device
from traffic_light.device import (
    CONFIG_USAGE,
    CONFIG_USAGE_PAGE,
    FRAME_LENGTH,
    MODE_24G,
    MODE_NONE,
    MODE_WIRED,
    OBJECT_LIVE_DATA,
    PID_RECEIVER,
    PID_WIRED,
    ROUTE_BYTE,
    VID_RECEIVER,
    VID_WIRED,
    DeviceError,
    HidDeviceInfo,
    check_frame,
    detect_mode,
    find_config_channel,
    validate_channel,
    write_feature,
)

HID_GUID = "{4d1e55b2-f16f-11cf-88cb-001111000030}"


def wired_path(collection: str = "col06") -> str:
    """造一个形状真实的有线 Hi86 HID 接口路径（集合编号可换）。"""
    return (r"\\?\hid#vid_258a&pid_010c&mi_04&" + collection
            + r"#7&2c4b1e5c&0&0000#" + HID_GUID)


def receiver_path() -> str:
    """造一个形状真实的 2.4G 接收器路径：它也有 col06，但特征长度只有 8。"""
    return (r"\\?\hid#vid_3554&pid_fa09&mi_00&col06"
            + r"#7&3d1c2f4a&0&0000#" + HID_GUID)


def make_info(**overrides) -> HidDeviceInfo:
    """默认就是合法的有线配置通道，overrides 用来制造单项不符。"""
    fields = {
        "path": wired_path(),
        "vid": VID_WIRED,
        "pid": PID_WIRED,
        "usage_page": CONFIG_USAGE_PAGE,
        "usage": CONFIG_USAGE,
        "feature_length": FRAME_LENGTH,
    }
    fields.update(overrides)
    return HidDeviceInfo(**fields)


def make_receiver_info() -> HidDeviceInfo:
    """接收器上的 col06 集合：VID/PID/UsagePage/Usage/特征长度全都不符。"""
    return HidDeviceInfo(path=receiver_path(), vid=VID_RECEIVER, pid=PID_RECEIVER,
                         usage_page=0xFF04, usage=0x0002, feature_length=8)


def valid_frame() -> bytes:
    """一帧合法的实时数据帧：06 08 00 00 01 00 7A 01 + 512 字节载荷。"""
    header = bytes([ROUTE_BYTE, OBJECT_LIVE_DATA, 0x00, 0x00, 0x01, 0x00, 0x7A, 0x01])
    return header + bytes(FRAME_LENGTH - len(header))


def forbidden(name: str):
    """桩：一旦被调用就说明走了不该走的路径（比如本该空列表却去枚举）。"""

    def boom(*args, **kwargs):
        raise AssertionError("不应该走到 %s" % name)

    return boom


class NoApiCall:
    """假 DLL：任何属性被碰就失败，用来证明「自检没过就没调 API」。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str):
        raise AssertionError("不应该碰 %s.%s" % (self._name, item))


def test_api_constants_match_protocol():
    """API 常量就是硬件事实，改错会写到别的集合上。"""
    assert (MODE_WIRED, MODE_24G, MODE_NONE) == ("wired", "2.4g", "none")
    assert (VID_WIRED, PID_WIRED) == (0x258A, 0x010C)
    assert (VID_RECEIVER, PID_RECEIVER) == (0x3554, 0xFA09)
    assert (CONFIG_USAGE_PAGE, CONFIG_USAGE) == (0xFF00, 0x0001)
    assert FRAME_LENGTH == 520
    assert (ROUTE_BYTE, OBJECT_LIVE_DATA) == (0x06, 0x08)


def test_validate_channel_accepts_config_channel():
    ok, reason = validate_channel(make_info())
    assert ok is True
    assert reason is None


@pytest.mark.parametrize("collection", ["col06", "COL06", "Col06"])
def test_validate_channel_collection_tag_is_case_insensitive(collection):
    ok, reason = validate_channel(make_info(path=wired_path(collection)))
    assert ok is True
    assert reason is None


@pytest.mark.parametrize(
    ("overrides", "token"),
    [
        ({"vid": PID_WIRED}, "VID 不符"),
        ({"pid": VID_WIRED}, "PID 不符"),
        ({"usage_page": 0x0001}, "UsagePage 不符"),
        ({"usage": 0x0006}, "Usage 不符"),
        ({"feature_length": 6}, "FeatureReportByteLength 不符"),
        ({"path": wired_path("col05")}, "COL06"),
    ],
)
def test_validate_channel_rejects_each_single_mismatch(overrides, token):
    ok, reason = validate_channel(make_info(**overrides))
    assert ok is False
    assert token in reason


def test_validate_channel_rejects_col05_feature_length_zero():
    """COL03 特征长度 0：UsagePage/Usage 与 COL06 一模一样，仍必须拒绝。"""
    ok, reason = validate_channel(make_info(path=wired_path("col03"), feature_length=0))
    assert ok is False
    assert "FeatureReportByteLength" in reason


def test_validate_channel_rejects_same_usage_page_wrong_collection():
    """COL05：前四项全对、路径也含 col05，只有特征长度和集合编号能拦下来。"""
    info = make_info(path=wired_path("col05"), feature_length=6)
    ok, reason = validate_channel(info)
    assert ok is False
    assert "FeatureReportByteLength" in reason or "COL06" in reason


def test_validate_channel_rejects_receiver_collection():
    """接收器上也有 col06，但 VID/PID/UsagePage/特征长度全不符 → 拒绝。"""
    info = make_receiver_info()
    assert "col06" in info.path  # 只看集合编号会误判
    ok, reason = validate_channel(info)
    assert ok is False
    assert reason


def test_validate_channel_rejects_wired_without_collection_tag():
    """VID/PID/特征长度都对，但路径里没有集合编号 → 拒绝（第二重锁定）。"""
    ok, reason = validate_channel(make_info(path=r"\\?\hid#vid_258a&pid_010c#7&2c4b1e5c&0&0000#" + HID_GUID))
    assert ok is False
    assert "COL06" in reason


def test_find_config_channel_picks_only_col06():
    col03 = make_info(path=wired_path("col03"), feature_length=0)
    col05 = make_info(path=wired_path("col05"), feature_length=6)
    col06 = make_info(path=wired_path("col06"), feature_length=FRAME_LENGTH)
    assert find_config_channel([col03, col05, col06]) is col06


def test_find_config_channel_returns_none_without_match():
    col03 = make_info(path=wired_path("col03"), feature_length=0)
    col05 = make_info(path=wired_path("col05"), feature_length=6)
    assert find_config_channel([col03, col05, make_receiver_info()]) is None


def test_find_config_channel_empty_list_does_not_enumerate(monkeypatch):
    """传 [] 就是空列表：绝不能退回去现场枚举设备。"""
    monkeypatch.setattr(device, "list_devices", forbidden("list_devices"))
    assert find_config_channel([]) is None


def test_find_config_channel_enumerates_only_when_devices_is_none(monkeypatch):
    monkeypatch.setattr(device, "list_devices", lambda: [make_info()])
    found = find_config_channel()
    assert found is not None
    assert found.path == wired_path()


def test_detect_mode_wired_only():
    assert detect_mode([make_info()]) == MODE_WIRED


def test_detect_mode_receiver_only():
    assert detect_mode([make_receiver_info()]) == MODE_24G


def test_detect_mode_none_when_empty(monkeypatch):
    monkeypatch.setattr(device, "list_devices", forbidden("list_devices"))
    assert detect_mode([]) == MODE_NONE


def test_detect_mode_prefers_wired_over_receiver():
    assert detect_mode([make_receiver_info(), make_info()]) == MODE_WIRED


def test_detect_mode_enumerates_only_when_devices_is_none(monkeypatch):
    monkeypatch.setattr(device, "list_devices", lambda: [make_receiver_info()])
    assert detect_mode() == MODE_24G


def test_detect_mode_ignores_unrelated_hid_devices():
    mouse = HidDeviceInfo(path=wired_path("col01"), vid=0x046D, pid=0xC52B,
                          usage_page=0x0001, usage=0x0006, feature_length=0)
    assert detect_mode([mouse]) == MODE_NONE


def test_check_frame_accepts_live_data_frame():
    check_frame(valid_frame())  # 不抛异常就是合格
    check_frame(bytearray(valid_frame()))
    check_frame(memoryview(valid_frame()))


@pytest.mark.parametrize("length", [0, 1, 8, 519, 521, 1024])
def test_check_frame_rejects_wrong_length(length):
    with pytest.raises(DeviceError) as exc:
        check_frame(bytes(length))
    assert "帧长" in str(exc.value)


def test_check_frame_rejects_bad_route_byte():
    frame = bytearray(valid_frame())
    frame[0] = 0x0A
    with pytest.raises(DeviceError) as exc:
        check_frame(bytes(frame))
    assert "路由字节" in str(exc.value)


@pytest.mark.parametrize("object_number", [0x03, 0x04, 0x05, 0x06, 0x0A, 0x11])
def test_check_frame_rejects_forbidden_object_numbers(object_number):
    """对象 3/4/5/6/0x11 分别是键位表、LED 档案、宏、游戏模式、复位，红线禁止。"""
    frame = bytearray(valid_frame())
    frame[1] = object_number
    with pytest.raises(DeviceError) as exc:
        check_frame(bytes(frame))
    assert "对象号" in str(exc.value)


def test_check_frame_rejects_non_bytes():
    with pytest.raises(DeviceError):
        check_frame([0] * FRAME_LENGTH)
    with pytest.raises(DeviceError):
        check_frame("x" * FRAME_LENGTH)


def test_write_feature_rejects_wrong_length_before_any_api_call(monkeypatch):
    """自检先于任何 API 调用：帧不合格时连 hid DLL 都不许碰。"""
    monkeypatch.setattr(device, "hid", NoApiCall("hid"))
    with pytest.raises(DeviceError) as exc:
        write_feature(0xDEAD, bytes(FRAME_LENGTH - 1))
    assert "帧长" in str(exc.value)


@pytest.mark.parametrize("route,object_number", [(0x0A, OBJECT_LIVE_DATA), (ROUTE_BYTE, 0x11)])
def test_write_feature_rejects_bad_header_before_any_api_call(monkeypatch, route, object_number):
    monkeypatch.setattr(device, "hid", NoApiCall("hid"))
    frame = bytearray(valid_frame())
    frame[0] = route
    frame[1] = object_number
    with pytest.raises(DeviceError):
        write_feature(0xDEAD, bytes(frame))


def test_open_exclusive_refuses_invalid_channel_without_opening(monkeypatch):
    """复核不通过时连 CreateFileW 都不许调：句柄绝不能落到没确认的集合上。"""
    monkeypatch.setattr(device, "kernel32", NoApiCall("kernel32"))
    with pytest.raises(DeviceError) as exc:
        device.open_exclusive(make_receiver_info())
    assert "复核" in str(exc.value)


@pytest.mark.parametrize("name", ["OemDrv.exe", "oemdrv.exe", "OEMDRV.EXE", "oEmDrV.eXe"])
def test_match_process_is_case_insensitive(name):
    assert device._match_process([name], "OemDrv.exe") is True


def test_match_process_finds_target_among_many():
    names = ["System", "explorer.exe", "OemDrv.exe", "opencode.exe"]
    assert device._match_process(names, "OemDrv.exe") is True


@pytest.mark.parametrize(
    "names",
    [[], ["explorer.exe"], ["notOemDrv.exe"], ["OemDrv.exe.bak"], ["OemDrv"],
     [" OemDrv.exe"], ["OemDrv.exe "]],
)
def test_match_process_rejects_non_match(names):
    """全名相等，不是子串匹配：多一个字符就不算官方程序在跑。"""
    assert device._match_process(names, "OemDrv.exe") is False


def test_oemdrv_running_uses_process_snapshot(monkeypatch):
    """快照用桩替换，测试不跑真实进程枚举。"""
    monkeypatch.setattr(device, "_process_names", lambda: ["chrome.exe", "OemDrv.exe"])
    assert device.oemdrv_running() is True
    monkeypatch.setattr(device, "_process_names", lambda: ["chrome.exe"])
    assert device.oemdrv_running() is False
