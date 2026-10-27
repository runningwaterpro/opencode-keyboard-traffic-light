"""完成确认绿灯：30 秒窗口；未运行/静默中断/重建不点绿（票 #8）。"""
from traffic_light.core import (IDLE_VISIBLE_MS, Event, GuardState, Light,
                                SessionSnapshot, SetKeyLight, Snapshot,
                                apply_event, apply_snapshot, tick)

T0 = 1_703_000_000_000


def feed(state, type_, t, sid="ses_a", **kw):
    return apply_event(state, Event(type=type_, session_id=sid, **kw), t)


def lights(effects):
    return [e.light for e in effects if isinstance(e, SetKeyLight)]


def run_and_succeed(t0=T0, sid="ses_a"):
    s = GuardState()
    feed(s, "session.created", t0, sid=sid)
    feed(s, "session.execution.started", t0 + 10, sid=sid)
    return s


def test_green_lasts_thirty_seconds_then_turns_off():
    s = run_and_succeed()
    assert lights(feed(s, "session.execution.succeeded", T0 + 100)) == [Light.IDLE]
    assert tick(s, T0 + 100 + IDLE_VISIBLE_MS - 1) == []
    assert s.display is Light.IDLE
    assert lights(tick(s, T0 + 100 + IDLE_VISIBLE_MS)) == [Light.OFF]
    assert s.display is Light.OFF


def test_never_run_session_stays_dark():
    s = GuardState()
    assert feed(s, "session.created", T0) == []
    assert lights(feed(s, "session.execution.started", T0 + 10)) == [Light.RUNNING]
    s2 = GuardState()
    feed(s2, "session.created", T0)
    assert lights(feed(s2, "permission.asked", T0 + 10, request_id="p")) == [Light.WAITING]
    feed(s2, "permission.replied", T0 + 20, request_id="p")
    assert lights(tick(s2, T0 + 610)) == [Light.OFF]


def test_silent_interrupt_goes_dark_immediately():
    for reason in ("user", "superseded", None):
        s = run_and_succeed()
        eff = feed(s, "session.execution.interrupted", T0 + 50, reason=reason)
        assert lights(eff) == [Light.OFF], reason


def test_shutdown_interrupt_keeps_red_on_tray_after_keyboard_expires():
    """键盘红灯 5 分钟后灭，但托盘（display）继续红：键盘只负责打断。"""
    from traffic_light.core import FAILED_MAX_MS

    s = run_and_succeed()
    assert lights(feed(s, "session.execution.interrupted", T0 + 50, reason="shutdown")) == [Light.FAILED]
    assert tick(s, T0 + 50 + FAILED_MAX_MS - 1) == []
    assert s.display is Light.FAILED
    assert lights(tick(s, T0 + 50 + FAILED_MAX_MS)) == [Light.OFF]   # 键盘灭
    assert s.display is Light.FAILED                                  # 托盘仍红
    assert tick(s, T0 + 50 + 24 * 3600 * 1000) == []
    assert s.display is Light.FAILED


def test_waiting_expires_on_keyboard_after_ten_minutes():
    """黄亮满 10 分钟就交棒给托盘；补帧不必无限做下去。"""
    from traffic_light.core import WAITING_MAX_MS

    s = GuardState()
    feed(s, "session.created", T0)
    assert lights(feed(s, "permission.asked", T0, request_id="p")) == [Light.WAITING]
    assert tick(s, T0 + WAITING_MAX_MS - 1) == []
    assert s.display is Light.WAITING
    assert lights(tick(s, T0 + WAITING_MAX_MS)) == [Light.OFF]
    assert s.display is Light.WAITING         # 还在等你，托盘不能黑


def test_answering_after_expiry_lights_up_again():
    """10 分钟后灯灭了，但新的一次询问重新点灯：上限按「本轮等待」算，不是永久封杀。"""
    from traffic_light.core import WAITING_MAX_MS

    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "permission.asked", T0, request_id="p1")
    tick(s, T0 + WAITING_MAX_MS)
    assert s.rendered == {}
    assert lights(feed(s, "permission.replied", T0 + WAITING_MAX_MS + 10,
                       request_id="p1")) == []
    assert lights(feed(s, "permission.asked", T0 + WAITING_MAX_MS + 20,
                       request_id="p2")) == [Light.WAITING]


def test_snapshot_rebuild_clears_green():
    s = run_and_succeed()
    feed(s, "session.execution.succeeded", T0 + 100)
    assert s.display is Light.IDLE
    eff = apply_snapshot(s, Snapshot(sessions=[SessionSnapshot("ses_a", running=False)],
                                     all_ids={"ses_a"}), T0 + 200)
    assert lights(eff) == [Light.OFF]
    assert s.display is Light.OFF


def test_group_turns_green_when_last_descendant_finishes():
    s = GuardState()
    feed(s, "session.created", T0, sid="p")
    feed(s, "session.created", T0 + 1, sid="c", parent_id="p")
    feed(s, "session.execution.started", T0, sid="p")
    feed(s, "session.execution.started", T0 + 2, sid="c")
    assert lights(feed(s, "session.execution.succeeded", T0 + 6000, sid="p")) == []
    assert lights(feed(s, "session.execution.succeeded", T0 + 7000, sid="c")) == [Light.IDLE]
    assert s.display is Light.IDLE
    assert lights(tick(s, T0 + 7000 + IDLE_VISIBLE_MS)) == [Light.OFF]


def test_rerun_restarts_the_green_window():
    s = run_and_succeed()
    feed(s, "session.execution.succeeded", T0 + 100)
    feed(s, "session.execution.started", T0 + 5000)
    assert lights(feed(s, "session.execution.succeeded", T0 + 6000)) == [Light.IDLE]
    assert tick(s, T0 + 6000 + IDLE_VISIBLE_MS - 1) == []
    assert s.display is Light.IDLE
    assert lights(tick(s, T0 + 6000 + IDLE_VISIBLE_MS)) == [Light.OFF]


def test_eviction_still_prefers_longest_idle_after_green_decays():
    s = GuardState()
    for i in range(10):
        feed(s, "session.execution.started", T0 + i, sid=f"s{i}")
    feed(s, "session.execution.succeeded", T0 + 100, sid="s9")
    tick(s, T0 + 100 + IDLE_VISIBLE_MS)
    assert s.display is Light.RUNNING
    t_new = T0 + 100 + IDLE_VISIBLE_MS + 10
    assert lights(feed(s, "session.execution.started", t_new, sid="x")) == [Light.RUNNING]
    assert s.sessions["s9"].slot is None     # 淘汰的仍是最久空闲的 s9
    assert s.sessions["x"].slot == "0"