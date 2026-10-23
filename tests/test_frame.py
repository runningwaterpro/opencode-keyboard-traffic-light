# -*- coding: utf-8 -*-
"""副缝测试：灯态 → 520 字节帧，全程离线。

纯函数，没有设备也没有时钟源，所有期望值都是手算的字面量，不是从实现反推的
—— 一份实现被它自己的逻辑算出的期望值验证，是同义反复，永远通过。

灯亮在错误的键上是本模块最危险的故障：Hi86 的 LED 索引是稀疏的
（86 个键散布在 0..95，中间 10 个空洞），偏移算错灯照样会亮，只是亮在别的键上，
看起来「有灯在亮」，不容易发现。

数字行索引与 leobog-hi86-lighting/data/led_map.json 的 keys 段逐项核对过
（写这份测试前读过该文件）：1→7、2→13、3→19、4→25、5→31、
6→37、7→43、8→49、9→55、0→61。
本测试不依赖那个仓库，也不读那个 JSON —— 上面是核对记录，不是运行时依赖。
"""
import pytest

from traffic_light.core import Light
from traffic_light.frame import (
    BREATH_MIN_LEVEL,
    BREATH_PERIOD_MS,
    BYTES_PER_LED,
    COLORS,
    FRAME_LENGTH,
    HEADER,
    MAX_LED_INDEX,
    NUMBER_ROW_LED,
    PAYLOAD_OFFSET,
    breath_brightness,
    build_frame,
    render_slots,
)

# 手算的灯态 → RGB，与规定表一致。
RUNNING_RGB = (128, 0, 255)
FAILED_RGB = (255, 0, 0)
WAITING_RGB = (255, 210, 0)
IDLE_RGB = (0, 200, 0)

HEADER_BYTES = [0x06, 0x08, 0x00, 0x00, 0x01, 0x00, 0x7A, 0x01]


def payload(frame, index):
    """取第 index 个 LED 的三字节。"""
    base = PAYLOAD_OFFSET + index * BYTES_PER_LED
    return list(frame[base:base + BYTES_PER_LED])


def test_frame_length_is_constant():
    assert len(build_frame({})) == 520
    assert len(build_frame({0: (1, 2, 3)})) == 520
    assert len(build_frame({MAX_LED_INDEX: (255, 255, 255)})) == 520


def test_header_is_exactly_eight_literal_bytes():
    frame = build_frame({})
    assert list(frame[:8]) == HEADER_BYTES
    assert list(HEADER) == HEADER_BYTES


def test_empty_frame_has_all_black_payload():
    frame = build_frame({})
    assert list(frame[PAYLOAD_OFFSET:]) == [0] * (FRAME_LENGTH - PAYLOAD_OFFSET)


def test_digit_one_lands_at_offset_8_plus_7_times_3():
    """数字 1 的 LED 索引是 7，RGB 起点应为 8 + 3×7 = 29。"""
    frame = build_frame({7: (255, 0, 0)})
    assert 29 == PAYLOAD_OFFSET + NUMBER_ROW_LED["1"] * BYTES_PER_LED
    assert list(frame[29:32]) == [255, 0, 0]


def test_untouched_leds_stay_dark():
    frame = build_frame({7: (255, 0, 0)})
    assert list(frame[PAYLOAD_OFFSET:29]) == [0] * 21
    assert list(frame[32:]) == [0] * (FRAME_LENGTH - 32)


def test_multiple_leds_land_independently():
    """1/5/0 三个键各落各的偏移，互不影响，中间的空隙保持黑。"""
    wanted = {"1": (1, 2, 3), "5": (4, 5, 6), "0": (7, 8, 9)}
    frame = build_frame({NUMBER_ROW_LED[k]: v for k, v in wanted.items()})
    for name, rgb in wanted.items():
        assert payload(frame, NUMBER_ROW_LED[name]) == list(rgb), name
    assert payload(frame, NUMBER_ROW_LED["1"] + 1) == [0, 0, 0]
    assert payload(frame, NUMBER_ROW_LED["9"] + 1) == [0, 0, 0]


def test_payload_offset_for_the_last_key():
    """索引 95 是硬件上界，起点 8 + 3×95 = 293，帧尾不被撑长。"""
    frame = build_frame({95: (0x12, 0x34, 0x56)})
    assert list(frame[293:296]) == [0x12, 0x34, 0x56]
    assert list(frame[296:]) == [0] * (FRAME_LENGTH - 296)
def test_index_above_max_is_value_error():
    for index in (MAX_LED_INDEX + 1, 96, 170, 512):
        with pytest.raises(ValueError):
            build_frame({index: (255, 0, 0)})


def test_negative_index_is_value_error():
    with pytest.raises(ValueError):
        build_frame({-1: (255, 0, 0)})


def test_index_at_max_is_accepted():
    assert len(build_frame({MAX_LED_INDEX: (1, 2, 3)})) == FRAME_LENGTH


def test_bad_color_width_is_value_error():
    for color in ((255, 0), (255, 0, 0, 0), (), (1, 2, 3, 4)):
        with pytest.raises(ValueError):
            build_frame({7: color})


def test_color_that_is_not_three_integers_is_value_error():
    for color in (255, None, (255, 0, "x"), (1.5, 0, 0), "abc"):
        with pytest.raises(ValueError):
            build_frame({7: color})


def test_non_int_index_is_type_error():
    for index in ("7", 7.0, None, [7]):
        with pytest.raises(TypeError):
            build_frame({index: (255, 0, 0)})


def test_bool_index_is_rejected():
    """bool 是 int 的子类，但 True 当索引 1 会静默点亮错误的键，必须拒。"""
    with pytest.raises(TypeError):
        build_frame({True: (255, 0, 0)})


def test_number_row_led_matches_the_true_source():
    """十项与 led_map.json 的 keys 段逐项核对过，这里是手抄的字面量。"""
    assert NUMBER_ROW_LED == {"1": 7, "2": 13, "3": 19, "4": 25, "5": 31,
                              "6": 37, "7": 43, "8": 49, "9": 55, "0": 61}


def test_number_row_led_is_sparse_and_in_range():
    assert set(NUMBER_ROW_LED) == set("1234567890")
    assert len(set(NUMBER_ROW_LED.values())) == 10
    for name, index in NUMBER_ROW_LED.items():
        assert 0 <= index <= MAX_LED_INDEX, name
    # 数字行相邻两个差 6：中间夹着同一列的其他行。
    for left, right in zip("123456789", "234567890"):
        assert NUMBER_ROW_LED[right] - NUMBER_ROW_LED[left] == 6


def test_render_slots_off_disappears():
    assert render_slots({"1": Light.OFF}, 1.0) == {}
    assert render_slots({"1": Light.OFF, "2": Light.IDLE}, 0.5) == {13: IDLE_RGB}


def test_render_slots_maps_four_states_to_their_colors():
    slots = {"1": Light.RUNNING, "2": Light.FAILED, "3": Light.IDLE}
    assert render_slots(slots, 1.0) == {7: RUNNING_RGB, 13: FAILED_RGB,
                                       19: IDLE_RGB}


def test_render_slots_colors_table_is_frozen():
    assert COLORS == {Light.RUNNING: RUNNING_RGB, Light.FAILED: FAILED_RGB,
                      Light.WAITING: WAITING_RGB, Light.IDLE: IDLE_RGB}


def test_render_slots_waiting_is_scaled_by_breath():
    assert render_slots({"0": Light.WAITING}, 1.0) == {61: WAITING_RGB}
    # 255×0.5=127.5 → int 截断 127；210×0.5=105.0 → 105；0 恒为 0
    assert render_slots({"0": Light.WAITING}, 0.5) == {61: (127, 105, 0)}
    # 最暗档：255×0.25=63.75 → 63；210×0.25=52.5 → 52
    assert render_slots({"0": Light.WAITING}, BREATH_MIN_LEVEL) == {
        61: (63, 52, 0)}


def test_render_slots_waiting_stays_visible_at_the_dimmest():
    """呼吸到最暗仍是可见的黄，不是灭。"""
    rgb = render_slots({"0": Light.WAITING}, BREATH_MIN_LEVEL)[61]
    assert rgb != (0, 0, 0)
    assert rgb[0] > 0 and rgb[1] > 0


def test_render_slots_unknown_key_is_value_error():
    for name in ("10", "a", "", "-", "Enter"):
        with pytest.raises(ValueError):
            render_slots({name: Light.IDLE}, 1.0)


def test_render_slots_unknown_key_raises_even_when_off():
    with pytest.raises(ValueError):
        render_slots({"x": Light.OFF}, 1.0)


def test_render_slots_covers_the_whole_number_row():
    """数字行十个键名都能渲染，且落在十个不同的 LED 上。"""
    rendered = render_slots({name: Light.IDLE for name in "1234567890"}, 1.0)
    assert set(rendered) == set(NUMBER_ROW_LED.values())
    assert len(rendered) == 10
def test_breath_is_never_below_min_level_and_never_above_one():
    for t in range(0, 3 * BREATH_PERIOD_MS, 25):
        level = breath_brightness(t)
        assert BREATH_MIN_LEVEL <= level <= 1.0, t


def test_breath_at_zero_is_the_min_level():
    """默认档在 t=0 就是最暗，且最暗不为 0（黄灯永不熄灭）。"""
    assert breath_brightness(0) == BREATH_MIN_LEVEL
    assert BREATH_MIN_LEVEL > 0.0


def test_breath_min_level_boundary():
    # min_level=1.0：没有明暗变化，恒亮
    for t in range(0, 2 * BREATH_PERIOD_MS, 50):
        assert breath_brightness(t, BREATH_PERIOD_MS, 1.0) == 1.0
    # min_level=0.0：允许触到 0（这条只在显式传参时成立）
    assert breath_brightness(0, BREATH_PERIOD_MS, 0.0) == 0.0


def test_breath_wave_shape_dimmest_at_zero_brightest_at_half():
    """三角波：t=0 最暗，半周期最亮，整周期回到起点。"""
    half = BREATH_PERIOD_MS // 2
    assert breath_brightness(0) == BREATH_MIN_LEVEL
    assert breath_brightness(half) == 1.0
    assert breath_brightness(BREATH_PERIOD_MS) == BREATH_MIN_LEVEL
    assert breath_brightness(BREATH_PERIOD_MS + half) == 1.0
    # 上升段单调不减
    levels = [breath_brightness(t) for t in range(0, half + 1, 15)]
    assert levels == sorted(levels)
    assert levels[0] < levels[-1]


def test_breath_is_periodic_and_continuous_at_the_seam():
    """周期函数：t 与 t+period 相等；接缝两侧必须挨着。"""
    for t in (0, 1, 375, 749, 1499, 2001):
        assert breath_brightness(t) == pytest.approx(
            breath_brightness(t + BREATH_PERIOD_MS))
    seam_before = breath_brightness(BREATH_PERIOD_MS - 1)
    assert abs(seam_before - breath_brightness(BREATH_PERIOD_MS)) < 0.01


def test_breath_rejects_bad_arguments():
    with pytest.raises(ValueError):
        breath_brightness(0, 0)
    with pytest.raises(ValueError):
        breath_brightness(0, BREATH_PERIOD_MS, 1.5)
def test_rendered_slots_to_frame_end_to_end():
    """灯态 → 渲染表 → 帧：每个数字键的三字节都在自己的偏移上，其余全黑。"""
    slots = {"1": Light.IDLE, "2": Light.FAILED, "5": Light.RUNNING,
             "0": Light.WAITING, "9": Light.OFF}
    rendered = render_slots(slots, 0.5)
    frame = build_frame(rendered)
    assert len(frame) == FRAME_LENGTH
    assert list(frame[:8]) == HEADER_BYTES
    assert payload(frame, NUMBER_ROW_LED["1"]) == [0, 200, 0]
    assert payload(frame, NUMBER_ROW_LED["2"]) == [255, 0, 0]
    assert payload(frame, NUMBER_ROW_LED["5"]) == [128, 0, 255]
    assert payload(frame, NUMBER_ROW_LED["0"]) == [127, 105, 0]
    assert payload(frame, NUMBER_ROW_LED["9"]) == [0, 0, 0]
    # 除渲染表给出的 LED 外，0..95 全黑（不许写到别的键上）
    for index in range(MAX_LED_INDEX + 1):
        if index in rendered:
            continue
        assert payload(frame, index) == [0, 0, 0], index


def test_breath_sweep_keeps_waiting_visible_in_every_frame():
    """整周期扫一遍：黄灯每一帧都亮着，且亮度不过界。"""
    for t in range(0, BREATH_PERIOD_MS, 30):
        breath = breath_brightness(t)
        rgb = render_slots({"0": Light.WAITING}, breath)[61]
        assert 0 < rgb[0] <= 255, t
        assert 0 < rgb[1] <= 210, t
        assert rgb[2] == 0, t
