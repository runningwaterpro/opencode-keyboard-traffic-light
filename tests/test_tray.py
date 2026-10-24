# -*- coding: utf-8 -*-
"""托盘纯逻辑测试：颜色与 tooltip，不创建任何窗口。"""
from traffic_light import tray
from traffic_light.core import Light


def test_color_gray_when_dry_run():
    assert tray.color_of(Light.RUNNING, connected=True, session_count=2,
                         dry_run=True) == tray.GRAY


def test_color_gray_when_disconnected_or_no_sessions():
    assert tray.color_of(Light.RUNNING, connected=False, session_count=2,
                         dry_run=False) == tray.GRAY
    assert tray.color_of(Light.RUNNING, connected=True, session_count=0,
                         dry_run=False) == tray.GRAY


def test_color_follows_light():
    for light, rgb in tray.COLOR_BY_LIGHT.items():
        assert tray.color_of(light, connected=True, session_count=1,
                             dry_run=False) == rgb


def test_tooltip_running_with_sessions():
    tip = tray.tooltip_of(Light.RUNNING, connected=True, session_count=2,
                          keyboard_count=1, dry_run=False)
    assert tip == "OpenCode：执行中 · 2 个会话\n键盘：1 把"


def test_tooltip_no_sessions_and_disconnected():
    assert tray.tooltip_of(Light.OFF, connected=True, session_count=0,
                           keyboard_count=0, dry_run=False) == "OpenCode：无会话\n键盘：0 把"
    assert tray.tooltip_of(Light.OFF, connected=False, session_count=0,
                           keyboard_count=2, dry_run=False) == "OpenCode：未连接\n键盘：2 把"


def test_tooltip_dry_run_prefix():
    tip = tray.tooltip_of(Light.IDLE, connected=True, session_count=1,
                          keyboard_count=0, dry_run=True)
    assert tip.startswith("[干跑] ")


def test_tooltip_truncated_to_limit():
    tip = tray.tooltip_of(Light.IDLE, connected=True, session_count=10 ** 40,
                          keyboard_count=10 ** 40, dry_run=False)
    assert len(tip) <= tray.TIP_MAX


def test_tooltip_all_status_words():
    words = {"执行中": Light.RUNNING, "失败": Light.FAILED,
             "等待": Light.WAITING, "空闲": Light.IDLE}
    for word, light in words.items():
        tip = tray.tooltip_of(light, connected=True, session_count=1,
                              keyboard_count=0, dry_run=False)
        assert word in tip