"""主缝测试：单会话状态迁移（事件 → 灯态效果）。"""
from traffic_light.core import Event, GuardState, Light, SetKeyLight, apply_event, tick

T0 = 1_703_000_000_000  # ms 基准时刻


def feed(state, type_, t, sid="ses_a", **kw):
    return apply_event(state, Event(type=type_, session_id=sid, **kw), t)


def lights(effects):
    return [e.light for e in effects]


def test_created_then_started_then_succeeded():
    s = GuardState()
    assert feed(s, "session.created", T0) == [SetKeyLight("1", Light.IDLE)]
    assert s.display is Light.IDLE
    assert lights(feed(s, "session.execution.started", T0 + 10)) == [Light.RUNNING]
    assert lights(feed(s, "session.execution.succeeded", T0 + 20)) == [Light.IDLE]


def test_failed_is_sticky_until_start_or_success():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "session.execution.started", T0)
    assert lights(feed(s, "session.execution.failed", T0 + 10)) == [Light.FAILED]
    assert feed(s, "session.renamed", T0 + 20, title="新标题") == []
    assert feed(s, "session.viewed", T0 + 30) == []
    assert s.display is Light.FAILED
    assert lights(feed(s, "session.execution.started", T0 + 40)) == [Light.RUNNING]
    assert lights(feed(s, "session.execution.succeeded", T0 + 50)) == [Light.IDLE]
    assert lights(feed(s, "session.execution.failed", T0 + 60)) == [Light.FAILED]
    assert s.sessions["ses_a"].title == "新标题"


def test_interrupted_user_and_superseded_are_silent():
    for reason in ("user", "superseded"):
        s = GuardState()
        feed(s, "session.created", T0)
        feed(s, "session.execution.started", T0)
        eff = feed(s, "session.execution.interrupted", T0 + 10, reason=reason)
        assert lights(eff) == [Light.IDLE], reason
        assert s.sessions["ses_a"].failed is False


def test_interrupted_shutdown_and_inactivity_are_failed():
    for reason in ("shutdown", "inactivity"):
        s = GuardState()
        feed(s, "session.created", T0)
        feed(s, "session.execution.started", T0)
        eff = feed(s, "session.execution.interrupted", T0 + 10, reason=reason)
        assert lights(eff) == [Light.FAILED], reason
        assert s.sessions["ses_a"].failed is True


def test_interrupted_unknown_reason_is_silent():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "session.execution.started", T0)
    eff = feed(s, "session.execution.interrupted", T0 + 10, reason=None)
    assert lights(eff) == [Light.IDLE]


def test_permission_waiting_and_min_visible():
    s = GuardState()
    feed(s, "session.created", T0)
    assert lights(feed(s, "permission.asked", T0 + 100, request_id="prq_1")) == [Light.WAITING]
    assert feed(s, "permission.replied", T0 + 110, request_id="prq_1") == []
    assert tick(s, T0 + 200) == []
    assert s.display is Light.WAITING
    assert lights(tick(s, T0 + 600)) == [Light.IDLE]


def test_permission_multiple_until_all_cleared():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "permission.asked", T0, request_id="a")
    feed(s, "permission.asked", T0, request_id="b")
    assert s.display is Light.WAITING
    assert feed(s, "permission.replied", T0 + 10, request_id="a") == []
    assert tick(s, T0 + 1000) == []
    assert s.display is Light.WAITING
    assert lights(feed(s, "permission.replied", T0 + 1010, request_id="b")) == [Light.IDLE]


def test_permission_replied_without_id_clears_session_permissions():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "permission.asked", T0, request_id="a")
    feed(s, "permission.asked", T0, request_id="b")
    assert lights(feed(s, "permission.replied", T0 + 700, request_id=None)) == [Light.IDLE]
    assert s.sessions["ses_a"].pending == {}


def test_form_waiting_cancel_and_reply():
    s = GuardState()
    feed(s, "session.created", T0)
    assert lights(feed(s, "form.created", T0, request_id="frm_1")) == [Light.WAITING]
    assert tick(s, T0 + 1000) == []
    assert lights(feed(s, "form.cancelled", T0 + 1000, request_id="frm_1")) == [Light.IDLE]
    assert lights(feed(s, "form.created", T0 + 2000, request_id="frm_2")) == [Light.WAITING]
    assert feed(s, "form.replied", T0 + 2010, request_id="frm_2") == []
    assert s.display is Light.WAITING
    assert lights(tick(s, T0 + 2500)) == [Light.IDLE]


def test_priority_across_sessions():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a")
    feed(s, "session.created", T0, sid="ses_b")
    assert s.display is Light.IDLE
    assert lights(feed(s, "session.execution.started", T0 + 10, sid="ses_b")) == [Light.RUNNING]
    assert lights(feed(s, "session.execution.failed", T0 + 20, sid="ses_a")) == [Light.FAILED]
    assert lights(feed(s, "form.created", T0 + 30, sid="ses_a", request_id="f1")) == [Light.WAITING]
    assert lights(feed(s, "session.archived", T0 + 40, sid="ses_a")) == [Light.RUNNING]
    assert lights(feed(s, "session.execution.succeeded", T0 + 50, sid="ses_b")) == [Light.IDLE]
    assert lights(feed(s, "session.archived", T0 + 60, sid="ses_b")) == [Light.OFF]


def test_child_sessions_fold_into_parent():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_p")
    feed(s, "session.created", T0, sid="ses_c", parent_id="ses_p")
    assert lights(feed(s, "session.execution.started", T0 + 10, sid="ses_c")) == [Light.RUNNING]
    feed(s, "session.execution.failed", T0 + 20, sid="ses_p")
    assert s.display is Light.FAILED
    assert feed(s, "session.execution.succeeded", T0 + 30, sid="ses_c") == []
    assert feed(s, "session.archived", T0 + 40, sid="ses_c") == []
    assert s.display is Light.FAILED
    assert lights(feed(s, "session.archived", T0 + 50, sid="ses_p")) == [Light.OFF]


def test_unarchived_returns_as_idle():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "session.archived", T0 + 10)
    assert s.display is Light.OFF
    assert lights(feed(s, "session.unarchived", T0 + 20)) == [Light.IDLE]


def test_renamed_for_unknown_session_is_ignored():
    s = GuardState()
    assert feed(s, "session.renamed", T0, sid="ses_ghost", title="x") == []
    assert s.sessions == {}


def test_noisy_events_ignored():
    s = GuardState()
    feed(s, "session.created", T0)
    for t in ("session.reasoning.delta", "session.text.delta", "session.tool.called",
              "session.step.started", "session.usage.updated", "server.connected"):
        assert apply_event(s, Event(type=t, session_id="ses_a"), T0 + 5) == []
    assert s.display is Light.IDLE
    assert len(s.sessions) == 1


def test_second_running_session_no_extra_effect():
    s = GuardState()
    feed(s, "session.execution.started", T0, sid="ses_a")
    assert feed(s, "session.execution.started", T0 + 1, sid="ses_b") == []



def test_parent_cycle_still_shows_group_light():
    s = GuardState()
    feed(s, "session.created", T0, sid="ses_a")
    feed(s, "session.created", T0, sid="ses_b")
    s.sessions["ses_a"].parent_id = "ses_b"
    s.sessions["ses_b"].parent_id = "ses_a"
    feed(s, "session.execution.started", T0 + 10, sid="ses_a")
    assert s.display is Light.RUNNING


def test_archived_session_does_not_revive_on_late_event():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "session.archived", T0 + 10)
    assert s.display is Light.OFF
    assert feed(s, "session.execution.succeeded", T0 + 20) == []
    assert s.sessions == {}
    assert s.display is Light.OFF


def test_unarchived_clears_tombstone():
    s = GuardState()
    feed(s, "session.created", T0)
    feed(s, "session.archived", T0 + 10)
    feed(s, "session.unarchived", T0 + 20)
    assert lights(feed(s, "session.execution.started", T0 + 30)) == [Light.RUNNING]
