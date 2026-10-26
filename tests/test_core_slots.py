"""槽位测试：编号占位/即时释放/淘汰/等号/子会话折叠（ADR-0003）。"""
from traffic_light.core import (Event, GuardState, Light, SetKeyLight, SLOT_KEYS,
                                apply_enrichment, apply_event, tick)

T0 = 1_703_000_000_000


def feed(state, type_, t, sid, **kw):
    return apply_event(state, Event(type=type_, session_id=sid, **kw), t)


def lights(effects):
    return [e.light for e in effects if isinstance(e, SetKeyLight)]


def fill_running(state, t0, n=10):
    """填满全部槽位：均为执行中，无淘汰对象。"""
    for i in range(n):
        feed(state, "session.execution.started", t0 + i, sid=f"s{i}")


def test_ten_sessions_take_keys_in_order():
    s = GuardState()
    for i in range(10):
        feed(s, "session.created", T0 + i, sid=f"s{i}")
    for i, key in enumerate(SLOT_KEYS):
        assert s.sessions[f"s{i}"].slot == key
    assert s.rendered == {}   # 占号但不点灯：未运行没有「完成」事件


def test_eleventh_session_gets_no_slot_when_all_busy():
    s = GuardState()
    fill_running(s, T0)
    assert feed(s, "session.execution.started", T0 + 100, sid="ses_x") == []
    assert s.sessions["ses_x"].slot is None
    assert s.sessions["ses_x"].wait_slot is True
    assert set(s.rendered) == set(SLOT_KEYS)


def test_archive_releases_key_without_reshuffle():
    s = GuardState()
    for i in range(3):
        feed(s, "session.created", T0 + i, sid=f"s{i}")
    assert feed(s, "session.archived", T0 + 10, sid="s1") == []
    assert s.sessions["s0"].slot == "1"
    assert s.sessions["s2"].slot == "3"


def test_freed_key_goes_to_next_new_session_lowest_first():
    s = GuardState()
    for i in range(4):
        feed(s, "session.created", T0 + i, sid=f"s{i}")
    feed(s, "session.archived", T0 + 10, sid="s1")
    feed(s, "session.archived", T0 + 11, sid="s3")
    assert feed(s, "session.created", T0 + 20, sid="sx") == []
    assert feed(s, "session.created", T0 + 21, sid="sy") == []
    assert s.sessions["sx"].slot == "2"
    assert s.sessions["sy"].slot == "4"


def test_delete_releases_key():
    s = GuardState()
    feed(s, "session.created", T0, sid="s0")
    feed(s, "session.created", T0 + 1, sid="s1")
    assert feed(s, "session.deleted", T0 + 10, sid="s0") == []
    assert s.sessions["s1"].slot == "2"

def test_new_session_evicts_longest_idle():
    s = GuardState()
    for i in range(10):
        feed(s, "session.created", T0 + i, sid=f"s{i}")
    assert feed(s, "session.execution.started", T0 + 100, sid="ses_x") == [SetKeyLight("1", Light.RUNNING)]
    assert s.sessions["ses_x"].slot == "1"
    assert s.sessions["s0"].slot is None
    assert s.sessions["s0"].wait_slot is True
    assert s.sessions["s1"].slot == "2"


def test_eviction_skips_running_sessions():
    s = GuardState()
    for i in range(10):
        feed(s, "session.created", T0 + i, sid=f"s{i}")
    feed(s, "session.execution.started", T0 + 50, sid="s0")
    assert feed(s, "session.execution.started", T0 + 100, sid="ses_x") == [SetKeyLight("2", Light.RUNNING)]
    assert s.sessions["s0"].slot == "1"
    assert s.sessions["s1"].slot is None
    assert s.sessions["ses_x"].slot == "2"


def test_eviction_prefers_idle_over_older_failed():
    s = GuardState()
    fill_running(s, T0)
    feed(s, "session.execution.failed", T0 + 10, sid="s0")
    feed(s, "session.execution.succeeded", T0 + 50, sid="s9")
    eff = feed(s, "session.execution.started", T0 + 100, sid="ses_x")
    assert s.sessions["ses_x"].slot == "0"
    assert s.sessions["s9"].slot is None
    assert s.sessions["s0"].slot == "1"
    assert lights(eff) == [Light.RUNNING]


def test_eviction_evicts_earliest_failed_when_no_idle():
    s = GuardState()
    for i in range(10):
        feed(s, "session.execution.started", T0 + i, sid=f"s{i}")
        feed(s, "session.execution.failed", T0 + 20 + i, sid=f"s{i}")
    eff = feed(s, "session.execution.started", T0 + 100, sid="ses_x")
    assert s.sessions["ses_x"].slot == "1"
    assert s.sessions["s0"].slot is None
    assert s.sessions["s1"].slot == "2"
    assert lights(eff) == [Light.RUNNING]


def test_all_busy_waits_then_takes_freed_key():
    s = GuardState()
    fill_running(s, T0)
    feed(s, "permission.asked", T0 + 100, sid="ses_x", request_id="p1")
    assert s.sessions["ses_x"].slot is None
    assert feed(s, "session.archived", T0 + 200, sid="s5") == [SetKeyLight("6", Light.WAITING)]
    assert s.sessions["ses_x"].slot == "6"
    assert s.sessions["ses_x"].wait_slot is False

def test_waiters_take_freed_keys_in_first_seen_order():
    s = GuardState()
    fill_running(s, T0)
    feed(s, "permission.asked", T0 + 100, sid="ses_x", request_id="p1")
    feed(s, "permission.asked", T0 + 110, sid="ses_y", request_id="p2")
    assert s.sessions["ses_x"].slot is None
    assert s.sessions["ses_y"].slot is None
    assert feed(s, "session.archived", T0 + 200, sid="s2") == [SetKeyLight("3", Light.WAITING)]
    assert feed(s, "session.archived", T0 + 201, sid="s6") == [SetKeyLight("7", Light.WAITING)]
    assert s.sessions["ses_x"].slot == "3"
    assert s.sessions["ses_y"].slot == "7"


def test_waiting_session_does_not_retry_eviction():
    s = GuardState()
    fill_running(s, T0)
    feed(s, "session.execution.started", T0 + 100, sid="ses_x")
    assert s.sessions["ses_x"].slot is None
    feed(s, "session.execution.succeeded", T0 + 200, sid="s0")
    assert tick(s, T0 + 210) == []
    assert s.sessions["ses_x"].slot is None
    assert s.sessions["s0"].slot == "1"


def test_child_does_not_take_slot_and_folds_into_parent():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p")
    feed(s, "session.created", T0 + 1, sid="ses_c", parent_id="ses_p")
    assert s.sessions["ses_c"].slot is None
    assert s.slot_of("ses_c") == "1"
    assert feed(s, "session.execution.started", T0 + 10, sid="ses_c") == [SetKeyLight("1", Light.RUNNING)]
    assert s.sessions["ses_p"].slot == "1"


def test_claimed_child_releases_its_slot():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p")
    feed(s, "session.created", T0 + 1, sid="ses_x")
    feed(s, "session.execution.started", T0 + 10, sid="ses_x")
    eff = apply_enrichment(s, "ses_x", parent_id="ses_p", now_ms=T0 + 20)
    assert eff == [SetKeyLight("1", Light.RUNNING), SetKeyLight("2", Light.OFF)]
    assert s.sessions["ses_x"].slot is None
    assert s.slot_of("ses_x") == "1"


def test_eviction_skips_group_with_active_child():
    s = GuardState()
    for i in range(10):
        feed(s, "session.created", T0 + i, sid=f"s{i}")
    feed(s, "session.created", T0 + 50, sid="ses_c", parent_id="s0")
    feed(s, "session.execution.started", T0 + 60, sid="ses_c")
    assert feed(s, "session.execution.started", T0 + 100, sid="ses_x") == [SetKeyLight("2", Light.RUNNING)]
    assert s.sessions["s0"].slot == "1"
    assert s.sessions["s1"].slot is None
    assert s.sessions["ses_x"].slot == "2"
