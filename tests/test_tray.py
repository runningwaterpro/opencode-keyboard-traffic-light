# -*- coding: utf-8 -*-
"""托盘纯逻辑测试：颜色与 tooltip、右键菜单项，不创建任何窗口。"""
import logging

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
             "等待": Light.WAITING, "完成": Light.IDLE}
    for word, light in words.items():
        tip = tray.tooltip_of(light, connected=True, session_count=1,
                              keyboard_count=0, dry_run=False)
        assert word in tip
    tip = tray.tooltip_of(Light.OFF, connected=True, session_count=1,
                          keyboard_count=0, dry_run=False)
    assert "空闲" in tip

def test_waiting_blinks_only_when_active():
    assert tray.is_blinking(Light.WAITING, connected=True, session_count=1,
                            dry_run=False)
    assert not tray.is_blinking(Light.RUNNING, connected=True, session_count=1,
                                dry_run=False)
    assert not tray.is_blinking(Light.WAITING, connected=True, session_count=1,
                                dry_run=True)
    assert not tray.is_blinking(Light.WAITING, connected=False, session_count=1,
                                dry_run=False)
    assert not tray.is_blinking(Light.WAITING, connected=True, session_count=0,
                                dry_run=False)


def test_blink_color_alternates_between_yellow_and_gray():
    on = tray.blink_color(Light.WAITING, connected=True, session_count=1,
                          dry_run=False, phase_on=True)
    off = tray.blink_color(Light.WAITING, connected=True, session_count=1,
                           dry_run=False, phase_on=False)
    assert on == tray.COLOR_BY_LIGHT[Light.WAITING]
    assert off == tray.GRAY


def test_blink_color_static_when_not_waiting():
    for phase in (True, False):
        assert tray.blink_color(Light.RUNNING, connected=True, session_count=1,
                                dry_run=False,
                                phase_on=phase) == tray.COLOR_BY_LIGHT[Light.RUNNING]


# ---- 右键菜单：无线开关 ----

def _item(spec, item_id):
    return next(item for item in spec if item[1] == item_id)


def test_menu_toggle_is_checked_when_taken_over():
    flags, _id, text = _item(tray.menu_spec(True), tray.ID_WIRELESS)
    assert text == "无线键盘也亮灯"
    assert flags & tray.MF_CHECKED


def test_menu_toggle_is_unchecked_when_off():
    flags, _id, text = _item(tray.menu_spec(False), tray.ID_WIRELESS)
    assert text == "无线键盘也亮灯"
    assert not flags & tray.MF_CHECKED


def test_menu_hint_is_gray_and_right_below_the_toggle():
    spec = tray.menu_spec(True)
    flags, item_id, text = spec[1]
    assert text == "打字时不发帧，避免吞键"
    assert flags & tray.MF_GRAYED
    assert item_id == 0                       # 点了不会有命令
    assert spec[0][1] == tray.ID_WIRELESS     # 紧跟在开关下面


def test_menu_quit_stays_last():
    """退出固定在最后（沿用既有顺序）。"""
    spec = tray.menu_spec(True)
    assert spec[-1][1] == tray.ID_QUIT
    assert spec[-1][2] == "退出"
    assert spec[2][0] == tray.MF_SEPARATOR


def test_toggle_flips_state_and_reports_new_value():
    """点菜单项：回调收到「新状态」，勾选状态跟着变。"""
    seen = []
    icon = tray.TrayIcon(logging.getLogger("tray-test"), on_quit=lambda: None,
                         wireless_light=True)
    icon.set_on_wireless_toggle(seen.append)
    icon._toggle_wireless()
    assert seen == [False]
    assert icon.wireless_light is False
    icon._toggle_wireless()
    assert seen == [False, True]
    assert icon.wireless_light is True


def test_toggle_callback_error_does_not_break_the_tray():
    def boom(_enabled):
        raise RuntimeError("回调挂了")

    icon = tray.TrayIcon(logging.getLogger("tray-test"), on_quit=lambda: None,
                         wireless_light=True)
    icon.set_on_wireless_toggle(boom)
    icon._toggle_wireless()
    assert icon.wireless_light is False       # 本地状态照旧翻转


def test_menu_command_routes_wireless_id():
    seen = []
    icon = tray.TrayIcon(logging.getLogger("tray-test"), on_quit=lambda: None,
                         wireless_light=False)
    icon.set_on_wireless_toggle(seen.append)
    icon._on_command(tray.ID_WIRELESS)
    assert seen == [True]
