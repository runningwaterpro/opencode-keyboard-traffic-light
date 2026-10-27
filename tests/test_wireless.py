# -*- coding: utf-8 -*-
"""无线灯口测试：假传输层 + 假时钟 + 假空闲时间源，全离线。

只断言外部行为：发了哪些帧、每帧内容、哪些时刻一帧都没发。
不 mock 私有方法，也不碰真 HID。
"""
import logging
import time

import pytest

from traffic_light import dongle, frame
from traffic_light.core import Light, SetKeyLight
from traffic_light.wireless import (BLANK, MERGE_S, RESEND_S, RETRY_S,
                                    TYPING_IDLE_S, WirelessLightPort)

LOG = logging.getLogger("wireless-test")


class World:
    """假时钟 + 假空闲时间源，两者都由测试直接改。"""

    def __init__(self, idle: float = 1000.0):
        self.now = 0.0
        self.idle = idle

    def clock(self) -> float:
        return self.now

    def idle_seconds(self) -> float:
        return self.idle


class FakeTransport:
    def __init__(self, world: World):
        self.world = world
        self.open_calls: list[float] = []
        self.send_calls: list[tuple[float, bytes]] = []   # 含失败的那些
        self.sent: list[tuple[float, bytes]] = []         # 只有成功的
        self.closed: list[float] = []
        self.fail_open = False
        self.fail_send = False

    def open(self):
        self.open_calls.append(self.world.now)
        if self.fail_open:
            raise dongle.DongleError("桩：接收器没插")
        return object()

    def send(self, handle, buf: bytes) -> None:
        self.send_calls.append((self.world.now, bytes(buf)))
        if self.fail_send:
            raise dongle.DongleError("桩：键盘睡着")
        self.sent.append((self.world.now, bytes(buf)))

    def close(self, handle) -> None:
        self.closed.append(self.world.now)


def make_port(world=None, **kwargs):
    world = world or World()
    transport = kwargs.pop("transport", None) or FakeTransport(world)
    port = WirelessLightPort(LOG, transport=transport, clock=world.clock,
                             idle_seconds=world.idle_seconds, **kwargs)
    return port, transport, world


def step(port, world, t: float) -> None:
    world.now = t
    port._step(t)


def color_of(buf: bytes, key: str):
    index = frame.NUMBER_ROW_LED[key]
    return (buf[index], buf[dongle.LED_SLOTS + index],
            buf[dongle.LED_SLOTS * 2 + index])


def sent_times(transport):
    return [t for t, _ in transport.sent]


STEP = 0.5          # 生产步长，长跑测试照它推进


def run(port, world, start: float, seconds: float, idle=None) -> float:
    """按生产步长推进 start..start+seconds；idle 可随时间变。返回最后一刻。"""
    steps = int(round(seconds / STEP))
    for i in range(steps + 1):
        t = start + i * STEP
        if idle is not None:
            world.idle = idle(t)
        step(port, world, t)
    return start + steps * STEP


# ---- 基本显示 ----

def test_first_change_opens_and_sends_one_frame_after_merge_window():
    """灯态变化 → 合并窗口过后发一帧；帧内容 = 状态核心决定的颜色与键位。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])

    step(port, world, 0.0)
    assert transport.open_calls == []      # 合并窗口内连设备都不打开

    step(port, world, MERGE_S)
    assert transport.open_calls == [MERGE_S]
    assert sent_times(transport) == [MERGE_S]
    buf = transport.sent[0][1]
    assert len(buf) == dongle.COLOR_LEN
    assert color_of(buf, "1") == frame.COLORS[Light.FAILED]
    assert color_of(buf, "2") == (0, 0, 0)


def test_second_key_lights_its_own_led():
    port, transport, world = make_port()
    port.apply([SetKeyLight("2", Light.WAITING)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    buf = transport.sent[0][1]
    assert color_of(buf, "2") == frame.COLORS[Light.WAITING]
    assert color_of(buf, "1") == (0, 0, 0)


def test_running_only_lights_nothing_but_keeps_the_keyboard_awake():
    """执行中不落色：等于「什么都不用显示」，只发空白保活帧，一个 LED 都不点。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.RUNNING)])
    run(port, world, 0.0, 50.0)
    assert transport.open_calls                    # 保活要打开设备
    assert transport.sent
    assert all(buf == BLANK for _, buf in transport.sent)


def test_no_lights_still_sends_blank_keepalives():
    """没有灯态也要定期发一帧空白保活。

    真机复现（2026-10-27）：2.4G 下会话结束后对应的数字键不亮，
    只有偶尔碰一下键盘才补上绿灯。原因是不发帧时键盘约 70 秒休眠，
    休眠后发帧送不进去（断链后只有物理按键能叫醒）。
    空白帧不点亮任何 LED，视觉上无变化，唯一作用是别让键盘睡过去。
    """
    port, transport, world = make_port()
    step(port, world, 0.0)
    run(port, world, 1.0, 120.0)
    times = sent_times(transport)
    assert len(times) >= 3, times
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert max(gaps) <= RESEND_S + STEP, gaps
    assert all(buf == BLANK for _, buf in transport.sent)


def test_completion_after_a_long_blank_stretch_is_delivered():
    """空白保活跑了两分钟之后，完成事件必须能立刻上键。

    这条钉住用户看到的现象：会话跑完，灯要亮；不能因为键盘睡了而丢掉。
    """
    port, transport, world = make_port()
    run(port, world, 0.0, 120.0)          # 两分钟没有灯态
    port.apply([SetKeyLight("1", Light.IDLE)])
    t = world.now + MERGE_S + STEP
    step(port, world, t)
    assert color_of(transport.sent[-1][1], "1") == frame.COLORS[Light.IDLE]


def test_turning_off_sends_black_then_keeps_blank_keepalives():
    """灯灭了立刻发一帧全灭，之后只发空白保活（全灭内容不再变化）。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)

    port.apply([SetKeyLight("1", Light.OFF)])
    step(port, world, 2.0)               # 变化帧不吃 35 秒限流：距上次发帧已满 1 秒，立刻发
    assert sent_times(transport) == [1.0, 2.0]
    assert transport.sent[-1][1] == BLANK

    step(port, world, 2.0 + RESEND_S)     # 满一个补帧周期，再发一帧空白
    assert sent_times(transport) == [1.0, 2.0, 2.0 + RESEND_S]
    step(port, world, 500.0)              # 继续按周期保活
    assert len(sent_times(transport)) == 4
    assert all(buf == BLANK for _, buf in transport.sent[1:])


# ---- 打字门控（硬约束：15 秒内一帧都不发） ----

def test_typing_blocks_every_frame():
    """距上次输入不足 15 秒：一帧都不发，连接收器都不打开。"""
    port, transport, world = make_port()
    world.idle = TYPING_IDLE_S - 1
    port.apply([SetKeyLight("1", Light.FAILED)])
    for t in range(0, 31):
        step(port, world, float(t))
    assert transport.sent == []
    assert transport.open_calls == []


def test_hands_off_for_15_seconds_releases_the_gate():
    """停手满 15 秒，挂着的灯态当场发出去（灯不会一直黑着）。"""
    port, transport, world = make_port()
    world.idle = 3.0
    port.apply([SetKeyLight("1", Light.FAILED)])
    for t in range(0, 25):
        step(port, world, float(t))
    assert transport.sent == []

    world.idle = TYPING_IDLE_S
    step(port, world, 25.0)
    assert sent_times(transport) == [25.0]
    assert color_of(transport.sent[0][1], "1") == frame.COLORS[Light.FAILED]


def test_typing_again_blocks_the_next_frame():
    """发过一帧后继续打字：后续变化一律等着，不补帧。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert sent_times(transport) == [1.0]

    world.idle = 0.0
    port.apply([SetKeyLight("1", Light.OFF)])
    for t in (2.0, 40.0, 80.0):
        step(port, world, t)
    assert sent_times(transport) == [1.0]


def test_idle_source_error_counts_as_typing():
    """读不到空闲时间 = 不确定：按「在打字」处理，宁可不亮也不丢键。"""
    port, transport, world = make_port()

    def boom():
        raise OSError("GetLastInputInfo 失败")

    port._idle_seconds = boom
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 10.0)
    assert transport.sent == []


def test_typing_resumed_while_opening_blocks_the_frame():
    """打开接收器要几秒，期间人又开打了：发帧前必须再看一眼空闲时间。"""
    port, transport, world = make_port()
    calls = {"n": 0}

    def idle():
        calls["n"] += 1
        return 20.0 if calls["n"] == 1 else 2.0    # 第一次看过闸，第二次又开打

    port._idle_seconds = idle
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert transport.open_calls == [1.0]           # 设备开了
    assert transport.sent == []                    # 但一帧都没发


# ---- 合并窗口与限流 ----

def test_changes_within_one_second_become_one_frame():
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    port.apply([SetKeyLight("1", Light.WAITING)])
    step(port, world, 0.5)
    step(port, world, MERGE_S + 0.4)     # 0.9 秒，仍不够
    assert transport.sent == []
    step(port, world, MERGE_S + 0.5)     # 满 1 秒
    assert len(transport.sent) == 1
    assert color_of(transport.sent[0][1], "1") == frame.COLORS[Light.WAITING]


def test_new_state_goes_out_right_after_the_merge_window():
    """变化帧不等补帧周期：绿灯该灭就灭，最多等 1 秒合并窗口。

    真机复现过的缺陷：会话完成亮绿后重新开跑，绿灯拖了 35 秒才灭——
    因为 35 秒限流把「变化帧」也挡住了。丢键风险由打字门控负责，
    限流只该管「内容没变」的补帧。
    """
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert sent_times(transport) == [1.0]

    port.apply([SetKeyLight("1", Light.OFF), SetKeyLight("2", Light.IDLE)])
    step(port, world, 20.0)              # 变化后 0 秒：合并窗口内？
    assert sent_times(transport) == [1.0, 20.0]   # 不在窗口内（距上次发帧 19 秒），立刻发
    buf = transport.sent[-1][1]
    assert color_of(buf, "1") == (0, 0, 0)
    assert color_of(buf, "2") == frame.COLORS[Light.IDLE]


def test_rapid_changes_are_still_capped_at_one_frame_per_second():
    """变化帧不吃 35 秒限流，但也不许连发：1 秒内多次变化只出 1 帧。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert sent_times(transport) == [1.0]

    port.apply([SetKeyLight("1", Light.OFF), SetKeyLight("2", Light.IDLE)])
    step(port, world, 20.0)
    assert sent_times(transport) == [1.0, 20.0]

    port.apply([SetKeyLight("2", Light.OFF), SetKeyLight("3", Light.WAITING)])
    step(port, world, 20.5)              # 距上次发帧 0.5 秒：合并窗口内
    assert sent_times(transport) == [1.0, 20.0]
    step(port, world, 21.0)              # 满 1 秒
    assert sent_times(transport) == [1.0, 20.0, 21.0]
    assert color_of(transport.sent[-1][1], "3") == frame.COLORS[Light.WAITING]


def test_static_colors_are_resent_every_35_seconds():
    """不补帧灯 60 秒就灭：内容没变也每 35 秒补一帧。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    for t in (0.0, 1.0):
        step(port, world, t)
    for t in (2.0, 35.0, 35.9):
        step(port, world, t)
    assert sent_times(transport) == [1.0]
    step(port, world, 36.0)
    assert sent_times(transport) == [1.0, 36.0]
    assert transport.sent[0][1] == transport.sent[1][1]


# ---- 失败处理：退避重试，永不 disabled ----

def test_send_failure_backs_off_and_recovers(caplog):
    caplog.set_level(logging.INFO)
    port, transport, world = make_port()
    transport.fail_send = True
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)               # 打开成功，发帧失败

    assert port.state == "waiting"
    assert transport.closed == [1.0]
    assert port.failure_kind is None     # 无线永不进入「永久停灯」
    step(port, world, RETRY_S)           # 5.0 < 6.0，还没到重试点
    assert transport.open_calls == [1.0]

    transport.fail_send = False
    step(port, world, 1.0 + RETRY_S)
    assert len(transport.open_calls) == 2
    assert sent_times(transport) == [1.0 + RETRY_S]
    assert color_of(transport.sent[0][1], "1") == frame.COLORS[Light.FAILED]
    assert port.state == "open"

    warnings = [m for m in (r.getMessage() for r in caplog.records)
                if "发帧失败" in m]
    assert len(warnings) == 1            # 连续失败只在第一条记日志


def test_open_failure_backs_off_and_recovers(caplog):
    caplog.set_level(logging.INFO)
    port, transport, world = make_port()
    transport.fail_open = True
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert port.state == "waiting"
    step(port, world, 1.0 + RETRY_S - 0.1)
    assert transport.open_calls == [1.0]

    step(port, world, 1.0 + RETRY_S)     # 仍然失败
    assert len(transport.open_calls) == 2
    transport.fail_open = False
    step(port, world, 1.0 + RETRY_S * 2)
    assert len(transport.open_calls) == 3
    assert sent_times(transport) == [1.0 + RETRY_S * 2]

    warnings = [m for m in (r.getMessage() for r in caplog.records)
                if "打开 2.4G 接收器失败" in m]
    assert len(warnings) == 1


def test_failure_does_not_lose_the_pending_light():
    """失败期间灯态继续变化，重连后发的必须是当前灯态，不是失败那一帧。"""
    port, transport, world = make_port()
    transport.fail_send = True
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    port.apply([SetKeyLight("1", Light.OFF), SetKeyLight("3", Light.IDLE)])
    step(port, world, 2.0)                # 只登记变化，退避中不打开
    assert transport.open_calls == [1.0]
    transport.fail_send = False
    step(port, world, 1.0 + RETRY_S)
    buf = transport.sent[-1][1]
    assert color_of(buf, "1") == (0, 0, 0)
    assert color_of(buf, "3") == frame.COLORS[Light.IDLE]


# ---- 长跑：十分钟常亮、三分钟打字、停手回来 ----

def test_ten_minutes_keepalive_never_lets_the_light_die():
    """人离开十分钟：每 35 秒补一帧，帧间最大间隔远小于灯 60 秒的寿命。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    run(port, world, 0.0, 600.0, idle=lambda t: 1000.0)

    times = sent_times(transport)
    assert times[0] == MERGE_S
    assert len(times) == 18                      # 1 + 35k ≤ 600
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert max(gaps) <= RESEND_S + 1e-9          # 35 秒一帧，绝不拖到 60 秒
    assert len({buf for _t, buf in transport.sent}) == 1   # 内容始终一致


def test_three_minutes_of_typing_sends_nothing_at_all():
    """连续打字三分钟：零发帧、零闪烁（不打开设备、不写 HID）。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert sent_times(transport) == [1.0]

    opens = len(transport.open_calls)
    run(port, world, 2.0, 180.0, idle=lambda t: 2.0)
    assert sent_times(transport) == [1.0]
    assert len(transport.open_calls) == opens     # 连设备都不碰


def test_frames_resume_within_15_seconds_after_typing_stops():
    """停手 15 秒内灯自己回来：挂着的灯态在空闲满 15 秒那一刻发出。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)

    typed_until = 100.5
    run(port, world, 2.0, typed_until - 2.0, idle=lambda t: 2.0)
    assert sent_times(transport) == [1.0]

    run(port, world, typed_until, 20.0, idle=lambda t: t - typed_until)
    resume = [t for t in sent_times(transport) if t > 1.0]
    assert len(resume) == 1
    assert resume[0] - typed_until <= TYPING_IDLE_S + STEP
    assert resume[0] - typed_until >= TYPING_IDLE_S   # 不满 15 秒绝不发


def test_typing_burst_right_after_a_keepalive_does_not_flash():
    """补帧后立刻开打：补帧不会因为「灯态没变」再发一遍。"""
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.IDLE)])
    run(port, world, 0.0, 40.0, idle=lambda t: 1000.0)
    assert len(sent_times(transport)) == 2        # 1 和 36 各一帧
    before = len(transport.sent)
    run(port, world, 40.5, 60.0, idle=lambda t: 1.0)
    assert len(transport.sent) == before


# ---- 生命周期 ----

def test_stop_closes_the_handle_and_is_idempotent():
    port, transport, world = make_port()
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)
    assert port.state == "open"
    port.stop()
    assert port.state == "waiting"
    assert transport.closed == [1.0]
    port.stop()
    assert transport.closed == [1.0]


def test_background_thread_sends_frames():
    """start/stop：后台线程真的按步长发帧，stop 后关句柄。"""
    transport = FakeTransport(World())
    port = WirelessLightPort(LOG, transport=transport,
                             idle_seconds=lambda: 1000.0, step_interval=0.01)
    port.apply([SetKeyLight("1", Light.FAILED)])
    port.start()
    try:
        deadline = time.monotonic() + 5.0
        while not transport.sent and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        port.stop()
    assert len(transport.sent) == 1
    assert len(transport.closed) == 1


def test_apply_ignores_non_light_effects():
    port, transport, world = make_port()
    port.apply(["不是效果", object()])
    for t in (0.0, 10.0):
        step(port, world, t)
    assert all(buf == BLANK for _, buf in transport.sent)   # 一个 LED 都不点


def test_reopen_pushes_the_frame_even_if_content_is_unchanged():
    """键盘醒过来后重新打开句柄：必须马上补一帧，不能等满 35 秒。"""
    port, transport, world = make_port()
    transport.fail_send = True
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    step(port, world, 1.0)                       # 发失败：句柄关掉
    transport.fail_send = False
    step(port, world, 6.0)                       # 重开成功，立刻补帧
    step(port, world, 6.5)
    assert sent_times(transport) == [6.0]
    # 句柄重开后哪怕内容没变，也不该被 35 秒限流挡住
    assert transport.sent[0][1] == transport.send_calls[1][1]


def test_failures_keep_retrying_and_never_give_up():
    """失败是常态：每 5 秒重试一次，永远不停灯（红线与有线不同）。"""
    port, transport, world = make_port()
    transport.fail_send = True
    port.apply([SetKeyLight("1", Light.FAILED)])
    step(port, world, 0.0)
    for t in (1.0, 6.0, 11.0, 16.0, 21.0):
        step(port, world, t)
    assert len(transport.open_calls) == 5          # 每次退避到期都真的再试
    assert port.state == "waiting"
    assert port.failure_kind is None
    assert len(transport.sent) == 0