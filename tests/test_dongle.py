# -*- coding: utf-8 -*-
"""2.4G 协议帧构造测试：纯函数，不碰设备、不碰时间源。

覆盖三平面布局、27 包分包、CRC、以及「执行中不落色」。
"""
import pytest

from traffic_light import dongle, frame
from traffic_light.core import Light

LED1 = frame.NUMBER_ROW_LED["1"]      # 7
LED2 = frame.NUMBER_ROW_LED["2"]      # 13


def test_color_buf_is_378_bytes_in_three_planes():
    """布局是三平面，不是扁平 RGB 三元组：LED i 落在 i / 126+i / 252+i。

    按扁平布局发会亮在完全无关的键上（实测踩过），所以逐字节断言。
    """
    buf = dongle.build_color_buf({LED1: (255, 0, 0), LED2: (0, 200, 0)})
    assert len(buf) == dongle.COLOR_LEN == 378
    assert buf[LED1] == 255
    assert buf[dongle.LED_SLOTS + LED1] == 0
    assert buf[dongle.LED_SLOTS * 2 + LED1] == 0
    assert buf[LED2] == 0
    assert buf[dongle.LED_SLOTS + LED2] == 200
    assert buf[dongle.LED_SLOTS * 2 + LED2] == 0


def test_color_buf_leaves_other_leds_off():
    buf = dongle.build_color_buf({LED1: (1, 2, 3)})
    rest = bytearray(buf)
    for offset in (0, dongle.LED_SLOTS, dongle.LED_SLOTS * 2):
        rest[offset + LED1] = 0
    assert bytes(rest) == bytes(dongle.COLOR_LEN)


def test_empty_leds_is_all_black():
    assert dongle.build_color_buf({}) == bytes(dongle.COLOR_LEN)


@pytest.mark.parametrize("index", [-1, 126, 999])
def test_color_buf_rejects_out_of_range_index(index):
    with pytest.raises(ValueError):
        dongle.build_color_buf({index: (1, 2, 3)})


def test_color_buf_rejects_bool_index():
    """True 是 int 的子类，当索引用会静默点亮错误的键。"""
    with pytest.raises(ValueError):
        dongle.build_color_buf({True: (1, 2, 3)})


def test_color_packets_split_into_27_ack_packets():
    """378 字节 = 27 × 14，逐包编号，载荷按顺序切分。"""
    buf = bytes(range(256)) + bytes(dongle.COLOR_LEN - 256)
    packets = dongle.build_color_packets(buf)
    assert len(packets) == dongle.PKG_NUM == 27
    for index, pkt in enumerate(packets):
        assert len(pkt) == dongle.PKT_LEN == 19
        assert pkt[0] == dongle.CMD_SET_LED_COLORS == 2
        assert pkt[1] == 27                      # 包总数
        assert pkt[2] == index                   # 包序号
        assert pkt[3] == 14                      # 数据长度
        assert pkt[4:18] == buf[index * 14:(index + 1) * 14]


def test_packet_crc_covers_cmd_and_payload():
    """CRC = (19 + sum(byte0..16)) & 0xFF，与接收器一致的算法。"""
    pkt = dongle.build_packet(dongle.CMD_SET_LED_COLORS, 27, 5, 14, bytes(range(14)))
    expected = (19 + sum(pkt[:18])) & 0xFF
    assert pkt[18] == expected == dongle.packet_crc(pkt)


def test_packet_masks_totals_and_index():
    """包总数/序号只有 7 位：超出部分被掩掉，不会溢出到别的字段。"""
    pkt = dongle.build_packet(2, 200, 200, 14)
    assert pkt[1] == 200 & 0x7F
    assert pkt[2] == 200 & 0x7F


def test_color_packets_reject_wrong_length():
    with pytest.raises(ValueError):
        dongle.build_color_packets(bytes(377))


def test_running_light_renders_nothing():
    """执行中不落色：2.4G 也走 frame.render_slots，语义与有线完全一致。"""
    lights = {"1": Light.RUNNING, "2": Light.WAITING}
    buf = dongle.build_color_buf(frame.render_slots(lights, 1.0))
    assert buf[LED1] == buf[dongle.LED_SLOTS + LED1] == buf[dongle.LED_SLOTS * 2 + LED1] == 0
    assert (buf[LED2], buf[dongle.LED_SLOTS + LED2],
            buf[dongle.LED_SLOTS * 2 + LED2]) == frame.COLORS[Light.WAITING]


def test_waiting_is_steady_full_brightness():
    """2.4G 做不了呼吸：黄灯按满亮度常亮，不是最暗相位。"""
    buf = dongle.build_color_buf(frame.render_slots({"1": Light.WAITING}, 1.0))
    assert (buf[LED1], buf[dongle.LED_SLOTS + LED1],
            buf[dongle.LED_SLOTS * 2 + LED1]) == frame.COLORS[Light.WAITING]


def test_build_packet_truncates_long_payload():
    """载荷超过 14 字节只取前 14 个，包长恒为 19（截断而不是改长度）。"""
    pkt = dongle.build_packet(dongle.CMD_SET_LED_COLORS, 1, 0, 14, bytes(range(20)))
    assert len(pkt) == dongle.PKT_LEN
    assert pkt[4:18] == bytes(range(14))


# ---- 逐包 ACK（不碰真设备：只替换写/读两个底层动作） ----

class FakeDongle(dongle.Dongle):
    """绕开 CreateFileW：只保留 send_colors 的逐包逻辑。"""

    def __init__(self, reply):
        self.path = "fake"
        self.written: list[bytes] = []
        self._reply = reply

    def write(self, pkt, timeout_ms=2000):
        self.written.append(bytes(pkt))
        return True

    def read_until(self, cmd, timeout_ms):
        return self._reply(cmd, len(self.written) - 1)


def test_send_colors_writes_every_packet_in_order():
    buf = dongle.build_color_buf({LED1: (10, 20, 30)})
    dev = FakeDongle(lambda cmd, index: bytes([dongle.CMD_SET_LED_COLORS, 0]))
    dev.send_colors(buf)
    assert dev.written == dongle.build_color_packets(buf)


def test_send_colors_raises_when_a_packet_is_never_acked():
    buf = dongle.build_color_buf({LED1: (10, 20, 30)})
    dev = FakeDongle(lambda cmd, index: None)
    with pytest.raises(dongle.DongleError) as excinfo:
        dev.send_colors(buf)
    assert "包 0" in str(excinfo.value)
    assert len(dev.written) == 5           # 重试 5 次后放弃，不是死循环


def test_send_colors_treats_error_bit_as_nack():
    buf = dongle.build_color_buf({LED1: (10, 20, 30)})
    dev = FakeDongle(lambda cmd, index: bytes([dongle.CMD_SET_LED_COLORS, 0x80]))
    with pytest.raises(dongle.DongleError):
        dev.send_colors(buf)


def test_open_dongle_tries_col01_first(monkeypatch):
    """接收器换过至少四个实例 ID：只能现场枚举，且 COL01 先试（其余必然超时）。"""
    paths = [r"\\?\hid#vid_3554&pid_fa09&mi_01&col04#inst#x",
             r"\\?\hid#vid_3554&pid_fa09&mi_01&col01#inst#y"]
    tried = []

    class Handle:
        def __init__(self, path):
            self.path = path

        def simple_command(self, cmd, timeout_ms=1500):
            return b"\x07\x01\x01\x01\x01"

        def close(self):
            pass

    monkeypatch.setattr(dongle, "enumerate_dongle_paths", lambda: list(paths))
    monkeypatch.setattr(dongle, "Dongle", lambda path: (tried.append(path),
                                                        Handle(path))[1])
    handle = dongle.open_dongle()
    assert handle.path == paths[1]
    assert tried == [paths[1]]             # 第一个就应答，不再碰其余集合