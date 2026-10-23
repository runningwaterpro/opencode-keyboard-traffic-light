"""通知测试：四类触发/静默/时长门槛/延迟补发/字段（通知效应）。"""
from traffic_light.core import (Event, GuardState, Light, Notify, SetKeyLight,
                                apply_event, parse_event)

T0 = 1_703_000_000_000


def feed(state, type_, t, sid, **kw):
    return apply_event(state, Event(type=type_, session_id=sid, **kw), t)


def notifies(effects):
    return [e for e in effects if isinstance(e, Notify)]


def filled_busy(state, t0):
    for i in range(10):
        feed(state, "session.execution.started", t0 + i, sid=f"s{i}")


def test_permission_notify_detail_from_resources_first():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a", directory=r"C:\work\demo")
    ev = parse_event({"type": "permission.asked",
                      "data": {"sessionID": "ses_a", "id": "prq_1",
                               "resources": ["src/core.py", "b.txt"]}})
    assert ev.detail == "src/core.py"
    eff = apply_event(s, ev, T0 + 10)
    assert eff == [Notify(kind="permission", session_id="ses_a", slot="1",
                          title=None, project="demo", detail="src/core.py"),
                   SetKeyLight("1", Light.WAITING)]


def test_question_notify_and_replies_silent():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a")
    eff = feed(s, "form.created", T0 + 10, sid="ses_a", request_id="f1")
    assert eff == [Notify(kind="question", session_id="ses_a", slot="1"),
                   SetKeyLight("1", Light.WAITING)]
    assert feed(s, "form.replied", T0 + 20, sid="ses_a", request_id="f1") == []
    assert feed(s, "form.cancelled", T0 + 30, sid="ses_a", request_id="f1") == []


def test_failed_notify_carries_title_and_project():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a", title="我的会话", directory=r"D:\dev\opencode")
    feed(s, "session.execution.started", T0 + 10, sid="ses_a")
    eff = feed(s, "session.execution.failed", T0 + 20, sid="ses_a")
    assert eff == [Notify(kind="failed", session_id="ses_a", slot="1",
                          title="我的会话", project="opencode"),
                   SetKeyLight("1", Light.FAILED)]


def test_interrupted_shutdown_and_inactivity_notify():
    for reason in ("shutdown", "inactivity"):
        s = GuardState()
        feed(s, "session.created", T0, sid="ses_a")
        feed(s, "session.execution.started", T0 + 10, sid="ses_a")
        eff = feed(s, "session.execution.interrupted", T0 + 20, sid="ses_a", reason=reason)
        assert notifies(eff) == [Notify(kind="interrupted", session_id="ses_a", slot="1")], reason

def test_interrupted_user_and_superseded_silent():
    for reason in ("user", "superseded"):
        s = GuardState()
        feed(s, "session.created", T0, sid="ses_a")
        feed(s, "session.execution.started", T0 + 10, sid="ses_a")
        eff = feed(s, "session.execution.interrupted", T0 + 20, sid="ses_a", reason=reason)
        assert notifies(eff) == [], reason
        assert eff == [SetKeyLight("1", Light.IDLE)], reason


def test_root_succeeded_at_five_seconds_notifies_completed():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a")
    feed(s, "session.execution.started", T0, sid="ses_a")
    eff = feed(s, "session.execution.succeeded", T0 + 5000, sid="ses_a")
    assert eff == [Notify(kind="completed", session_id="ses_a", slot="1"),
                   SetKeyLight("1", Light.IDLE)]


def test_root_succeeded_below_five_seconds_is_silent():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a")
    feed(s, "session.execution.started", T0, sid="ses_a")
    eff = feed(s, "session.execution.succeeded", T0 + 4999, sid="ses_a")
    assert eff == [SetKeyLight("1", Light.IDLE)]


def test_succeeded_with_unknown_duration_notifies_completed():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a")
    eff = feed(s, "session.execution.succeeded", T0 + 10, sid="ses_a")
    assert notifies(eff) == [Notify(kind="completed", session_id="ses_a", slot="1")]


def test_child_succeeded_never_notifies():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p")
    feed(s, "session.created", T0 + 1, sid="ses_c", parent_id="ses_p")
    feed(s, "session.execution.started", T0 + 2, sid="ses_c")
    eff = feed(s, "session.execution.succeeded", T0 + 6000, sid="ses_c")
    assert notifies(eff) == []
    assert eff == [SetKeyLight("1", Light.IDLE)]

def test_parent_completion_waits_for_all_descendants():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p")
    feed(s, "session.execution.started", T0, sid="ses_p")
    feed(s, "session.created", T0 + 1, sid="ses_c1", parent_id="ses_p")
    feed(s, "session.execution.started", T0 + 2, sid="ses_c1")
    feed(s, "session.created", T0 + 3, sid="ses_c2", parent_id="ses_p")
    feed(s, "session.execution.started", T0 + 4, sid="ses_c2")
    eff = feed(s, "session.execution.succeeded", T0 + 6000, sid="ses_p")
    assert notifies(eff) == []
    assert s.sessions["ses_p"].completion_pending is True
    eff = feed(s, "session.execution.succeeded", T0 + 7000, sid="ses_c1")
    assert notifies(eff) == []
    eff = feed(s, "session.execution.succeeded", T0 + 8000, sid="ses_c2")
    assert notifies(eff) == [Notify(kind="completed", session_id="ses_p", slot="1")]
    assert s.sessions["ses_p"].completion_pending is False


def test_deferred_completion_uses_renamed_title():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p", title="旧标题")
    feed(s, "session.execution.started", T0, sid="ses_p")
    feed(s, "session.created", T0 + 1, sid="ses_c", parent_id="ses_p")
    feed(s, "session.execution.started", T0 + 2, sid="ses_c")
    feed(s, "session.execution.succeeded", T0 + 6000, sid="ses_p")
    feed(s, "session.renamed", T0 + 6100, sid="ses_p", title="新标题")
    eff = feed(s, "session.execution.succeeded", T0 + 6200, sid="ses_c")
    assert notifies(eff) == [Notify(kind="completed", session_id="ses_p", slot="1",
                                   title="新标题")]


def test_child_failure_notify_uses_parent_slot():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p")
    feed(s, "session.created", T0 + 1, sid="ses_c", parent_id="ses_p")
    feed(s, "session.execution.started", T0 + 2, sid="ses_c")
    eff = feed(s, "session.execution.failed", T0 + 10, sid="ses_c")
    assert notifies(eff) == [Notify(kind="failed", session_id="ses_c", slot="1")]
    assert s.slot_of("ses_c") == "1"


def test_notify_slot_is_none_without_slot():
    s = GuardState()
    filled_busy(s, T0)
    eff = feed(s, "permission.asked", T0 + 100, sid="ses_x", request_id="p1")
    assert notifies(eff) == [Notify(kind="permission", session_id="ses_x", slot=None)]
    assert s.sessions["ses_x"].slot is None


def test_project_name_from_windows_directory():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a", directory=r"C:\Users\me\proj")
    eff = feed(s, "session.execution.failed", T0 + 1, sid="ses_a")
    assert notifies(eff)[0].project == "proj"
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_b", directory="D:\\dev\\app\\")
    eff = feed(s, "session.execution.failed", T0 + 1, sid="ses_b")
    assert notifies(eff)[0].project == "app"
