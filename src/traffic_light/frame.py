# -*- coding: utf-8 -*-
"""副缝：灯态 → 520 字节 HID 特征报告。

本模块是纯函数：没有设备、没有时间源、没有 I/O。呼吸波形的时间由调用方传入，
逐字节落位只依赖常数，所以整条链路都能离线测。

帧结构（与 leobog-hi86-lighting 的两个实现互证过）：
    偏移 0..7      HEADER，8 字节，逐字节固定
    偏移 8..519    载荷，每个 LED 3 字节 RGB
    LED 索引 i 的 RGB 起点 = PAYLOAD_OFFSET + 3 × i

LED 索引是稀疏的：86 个键散布在 0..95，中间有 10 个空洞。
算错偏移，灯会亮在别的键上，而且「有灯在亮」，不容易发现。
索引上界 95 是硬件事实（最右边的键），不是载荷能塞多少。
"""
from __future__ import annotations

from .core import Light

FRAME_LENGTH = 520
HEADER = bytes([0x06, 0x08, 0x00, 0x00, 0x01, 0x00, 0x7A, 0x01])
PAYLOAD_OFFSET = 8
BYTES_PER_LED = 3
MAX_LED_INDEX = 95

# 数字键名 → LED 索引。与 leobog-hi86-lighting/data/led_map.json 的 keys 段
# 逐项核对过（唯一真源）：1→7、2→13、3→19、4→25、5→31、
# 6→37、7→43、8→49、9→55、0→61。数字行不连续，相邻数字差 6。
NUMBER_ROW_LED = {"1": 7, "2": 13, "3": 19, "4": 25, "5": 31,
                  "6": 37, "7": 43, "8": 49, "9": 55, "0": 61}

# 四色灯态 → RGB。WAITING 的黄只乘亮度系数，不换色相。
COLORS = {Light.RUNNING: (128, 0, 255), Light.FAILED: (255, 0, 0),
          Light.WAITING: (255, 210, 0), Light.IDLE: (0, 200, 0)}

BREATH_PERIOD_MS = 1500
BREATH_MIN_LEVEL = 0.25


def _check_index(index: int) -> None:
    """LED 索引必须是 0..MAX_LED_INDEX 的整数。"""
    # bool 是 int 的子类，但 True 被当作索引 1 是静默点亮错误的键，直接拒。
    if isinstance(index, bool) or not isinstance(index, int):
        raise TypeError("LED 索引必须是整数，收到 %r" % (index,))
    if index < 0 or index > MAX_LED_INDEX:
        raise ValueError("LED 索引越界：%d（合法 0..%d）" % (index, MAX_LED_INDEX))


def _check_color(color) -> list[int]:
    """颜色必须是三个整数分量。返回掩码到 0..255 的三元组。"""
    try:
        channels = tuple(color)
    except TypeError:
        raise ValueError("颜色必须是三个分量，收到 %r" % (color,)) from None
    if len(channels) != 3:
        raise ValueError("颜色必须是三个分量，收到 %r" % (color,))
    out = []
    for channel in channels:
        if isinstance(channel, bool) or not isinstance(channel, int):
            raise ValueError("颜色分量必须是整数，收到 %r" % (color,))
        out.append(channel & 0xFF)
    return out


def build_frame(colors: dict[int, tuple[int, int, int]]) -> bytearray:
    """构造一帧 520 字节。

    :param colors: {LED 索引: (R, G, B)}。没给出的索引保持全 0（灭）。
    :returns: bytearray，长度恒为 FRAME_LENGTH。
    :raises TypeError: 索引不是整数。
    :raises ValueError: 索引越界，或颜色不是三个整数分量。
    """
    frame = bytearray(FRAME_LENGTH)
    frame[0:len(HEADER)] = HEADER
    for index, color in colors.items():
        _check_index(index)
        r, g, b = _check_color(color)
        offset = PAYLOAD_OFFSET + index * BYTES_PER_LED
        frame[offset] = r
        frame[offset + 1] = g
        frame[offset + 2] = b
    return frame
def breath_brightness(now_ms: int, period_ms: int = BREATH_PERIOD_MS,
                      min_level: float = BREATH_MIN_LEVEL) -> float:
    """黄灯呼吸的亮度系数，恒在 [min_level, 1.0]。

    三角波：t=0 最暗（正好 min_level），半周期最亮（1.0），整周期回到起点。
    所以它是周期函数，t 与 t+period_ms 取值相同，波形在接缝处连续。
    系数永不低于 min_level，也就是「呼吸但永不熄灭」。
    """
    if period_ms <= 0:
        raise ValueError("呼吸周期必须是正数，收到 %r" % (period_ms,))
    if not 0.0 <= min_level <= 1.0:
        raise ValueError("最低亮度必须在 0..1，收到 %r" % (min_level,))
    phase = (now_ms % period_ms) / period_ms        # 0..1
    triangle = 1.0 - abs(2.0 * phase - 1.0)         # 0 → 1 → 0
    return min_level + (1.0 - min_level) * triangle


def render_slots(slots: dict[str, Light], breath: float
                 ) -> dict[int, tuple[int, int, int]]:
    """数字键名 → (LED 索引, 颜色) 的渲染表。

    OFF 不出现在结果里（灭）；RUNNING/FAILED/IDLE 用 COLORS 原色；
    WAITING 用 COLORS[WAITING] 每通道乘 breath 后取 int，即呼吸。
    :raises ValueError: 键名不在 NUMBER_ROW_LED 里。
    """
    rendered = {}
    for name, light in slots.items():
        if name not in NUMBER_ROW_LED:
            raise ValueError("未知的数字键名 %r（合法：1..9、0）" % (name,))
        if light is Light.OFF or light is Light.RUNNING:
            # 执行中不点亮：它不触发任何你要做的动作，亮着只会稀释信号。
            continue
        if light is Light.WAITING:
            base = COLORS[Light.WAITING]
            rendered[NUMBER_ROW_LED[name]] = (int(base[0] * breath),
                                              int(base[1] * breath),
                                              int(base[2] * breath))
        elif light in COLORS:
            rendered[NUMBER_ROW_LED[name]] = COLORS[light]
        else:
            raise ValueError("未知灯态 %r（键名 %r）" % (light, name))
    return rendered
