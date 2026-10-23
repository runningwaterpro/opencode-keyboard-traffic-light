"""主缝测试：REST 重建快照（合并/清理/派生）。"""
from traffic_light.core import (Event, GuardState, Light, SessionSnapshot, SetKeyLight, Snapshot,
                                apply_event, apply_snapshot)

T0 = 1_703_000_000_000


def snap(sessions, all_ids=None):
    if all_ids is None:
        all_ids = {s.session_id for s in sessions}
    return Snapshot(sessions=list(sessions), all_ids=all_ids)


def S(sid, **kw):
    return SessionSnapshot(session_id=sid, **kw)


def test_rebuild_adds_running_and_pending():
    st = GuardState()
    eff = apply_snapshot(st, snap([S("ses_r", running=True),
                                   S("ses_w", pending={"prq_1": "permission"})]), T0)
    assert eff == [SetKeyLight("1", Light.RUNNING), SetKeyLight("2", Light.WAITING)]
    assert st.sessions["ses_r"].running is True
    assert st.sessions["ses_w"].pending == {"prq_1": "permission"}
    assert st.sessions["ses_w"].waiting_visible_until == T0 + 500


def test_rebuild_patches_tracked_sessions():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_old"), T0)
    apply_event(st, Event(type="session.execution.started", session_id="ses_old"), T0 + 10)
    apply_snapshot(st, snap([S("ses_old", title="T", running=False, failed=False)]), T0 + 100)
    assert st.sessions["ses_old"].running is False
    assert st.display is Light.IDLE
    assert st.sessions["ses_old"].title == "T"


def test_rebuild_marks_gap_failure():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_x"), T0)
    apply_event(st, Event(type="session.execution.started", session_id="ses_x"), T0 + 10)
    apply_snapshot(st, snap([S("ses_x", running=False, failed=True)]), T0 + 100)
    assert st.display is Light.FAILED


def test_rebuild_removes_absent():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_gone"), T0)
    eff = apply_snapshot(st, snap([], all_ids={"ses_other"}), T0 + 10)
    assert "ses_gone" not in st.sessions
    assert eff == [SetKeyLight("1", Light.OFF)]


def test_rebuild_incomplete_list_keeps_unknown():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_keep"), T0)
    apply_snapshot(st, Snapshot(sessions=[], all_ids=None), T0 + 10)
    assert "ses_keep" in st.sessions


def test_rebuild_ignores_idle_strangers():
    st = GuardState()
    apply_snapshot(st, snap([S("ses_hist", running=False)]), T0)
    assert st.sessions == {}
    assert st.display is Light.OFF


def test_rebuild_fills_title_only_when_missing():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_t"), T0)
    apply_event(st, Event(type="session.renamed", session_id="ses_t", title="A"), T0 + 1)
    apply_snapshot(st, snap([S("ses_t", title="B")]), T0 + 2)
    assert st.sessions["ses_t"].title == "A"


def test_rebuild_child_folding():
    st = GuardState()
    apply_snapshot(st, snap([S("ses_p", running=False),
                             S("ses_c", parent_id="ses_p", running=True)]), T0)
    assert st.display is Light.RUNNING



def test_rebuild_interrupted_keeps_existing_red():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_x"), T0)
    apply_event(st, Event(type="session.execution.interrupted", session_id="ses_x",
                          reason="shutdown"), T0 + 10)
    assert st.display is Light.FAILED
    apply_snapshot(st, snap([S("ses_x", running=False, failed=False, outcome="interrupted")]), T0 + 100)
    assert st.display is Light.FAILED


def test_rebuild_succeeded_clears_red():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_x"), T0)
    apply_event(st, Event(type="session.execution.failed", session_id="ses_x"), T0 + 10)
    apply_snapshot(st, snap([S("ses_x", running=False, failed=False, outcome="succeeded")]), T0 + 100)
    assert st.display is Light.IDLE


def test_rebuild_running_clears_red():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_x"), T0)
    apply_event(st, Event(type="session.execution.failed", session_id="ses_x"), T0 + 10)
    apply_snapshot(st, snap([S("ses_x", running=True, failed=False, outcome="interrupted")]), T0 + 100)
    assert st.display is Light.RUNNING


def test_rebuild_absence_adds_tombstone_and_readd_clears_it():
    st = GuardState()
    apply_event(st, Event(type="session.created", session_id="ses_z"), T0)
    apply_snapshot(st, snap([], all_ids=set()), T0 + 10)
    assert "ses_z" not in st.sessions
    assert apply_event(st, Event(type="session.execution.started", session_id="ses_z"), T0 + 20) == []
    apply_snapshot(st, snap([S("ses_z", running=True)], all_ids={"ses_z"}), T0 + 30)
    assert st.sessions["ses_z"].running is True
    assert st.display is Light.RUNNING
