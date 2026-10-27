# -*- coding: utf-8 -*-
"""键盘活动检测：只认键盘，不认鼠标。

背景：最早用的是 GetLastInputInfo（键盘或鼠标都算），
结果用户用鼠标看网页时它一直小于阈值，无线灯口一直不发帧，
60 秒的绿灯整个错过（2026-10-27 真机发现）。

改成轮询 GetAsyncKeyState：最低位表示「自上次调用以来按过」，
鼠标键（VK 0x01..0x06）不在扫描范围里。
"""
import time

import pytest

from traffic_light import useractivity


@pytest.fixture(autouse=True)
def clean_state():
    useractivity._reset_for_tests()
    yield
    useractivity._reset_for_tests()


def test_no_press_ever_means_idle_counts_up_from_start(monkeypatch):
    """刚启动还没见过按键：按「刚输入过」处理，空闲随时间增长，不会卡在 0。"""
    monkeypatch.setattr(useractivity, "_key_was_pressed", lambda vk: False)
    first = useractivity.keyboard_idle_seconds()
    time.sleep(0.02)
    second = useractivity.keyboard_idle_seconds()
    assert first >= 0
    assert second > first


def test_any_key_press_resets_idle(monkeypatch):
    """扫到任意一个键被按过，空闲立即归零。"""
    monkeypatch.setattr(useractivity, "_key_was_pressed", lambda vk: False)
    useractivity.keyboard_idle_seconds()
    time.sleep(0.02)

    pressed = {0x41}   # A 键
    monkeypatch.setattr(useractivity, "_key_was_pressed",
                        lambda vk: vk in pressed)
    assert useractivity.keyboard_idle_seconds() == 0.0


def test_mouse_keys_are_not_scanned(monkeypatch):
    """鼠标键（VK 0x01..0x06）不在扫描范围：点鼠标不算在打字。"""
    seen: list[int] = []

    def fake(vk: int) -> bool:
        seen.append(vk)
        return False

    monkeypatch.setattr(useractivity, "_key_was_pressed", fake)
    useractivity.keyboard_idle_seconds()
    assert seen and min(seen) >= useractivity.VK_SCAN_FIRST
    assert max(seen) <= useractivity.VK_SCAN_LAST


def test_reset_between_tests():
    useractivity.keyboard_idle_seconds()
    useractivity._reset_for_tests()
    assert useractivity._keyboard_last_ms is None