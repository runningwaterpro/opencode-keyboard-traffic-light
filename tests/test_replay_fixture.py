"""真实事件回放：离线重放实机抓包摘录（tests/fixtures/replay-sample.jsonl），断言收敛灯态。

夹具按空行切块、按 type/sessionID 从实机 SSE 抓包原样摘录 12 帧（含 4 帧高频噪声）。
回放时间取帧的 created 字段；缺失时用递增计数兜底，保证确定性。
"""
from pathlib import Path

from traffic_light.core import GuardState, Light, apply_event, parse_event, tick
from traffic_light.sse import parse_sse_lines

FIXTURE = Path(__file__).parent / "fixtures" / "replay-sample.jsonl"

# 抓包里的两个真实会话
A = "ses_e97ea85d4ffe9d22XeuFKk3DXz"  # 抓包结束时仍在跑
B = "ses_e98089494ffeadfsPrnzVGW9Cl"  # 被用户中断


def replay_fixture():
    """读夹具全部行 → parse_sse_lines → 逐帧 parse_event/apply_event。

    返回 (state, 最后一帧时间ms, 逐帧效果列表)。
    """
    state = GuardState()
    counter = 0  # 帧缺 created 时的兜底递增计数
    last_ms = 0
    effects_log = []
    for raw in parse_sse_lines(FIXTURE.read_text(encoding="utf-8").splitlines()):
        ev = parse_event(raw)
        if ev is None:
            continue
        created = raw.get("created")
        if isinstance(created, int):
            last_ms = created
        else:
            counter += 1
            last_ms = counter
        effects_log.append(apply_event(state, ev, last_ms))
    return state, last_ms, effects_log


def test_replay_never_emits_failed():
    # user 中断、表单取消必须静默：整个回放不出现红灯
    _, _, effects_log = replay_fixture()
    assert all(e.light is not Light.FAILED for eff in effects_log for e in eff)


def test_replay_final_session_states():
    state, _, _ = replay_fixture()
    assert state.sessions[A].running is True
    assert state.sessions[B].running is False
    assert state.sessions[B].failed is False


def test_replay_tick_after_last_frame_keeps_running_display():
    state, last_ms, _ = replay_fixture()
    effects = tick(state, last_ms + 1000)
    assert all(e.light is not Light.FAILED for e in effects)
    assert state.display is Light.RUNNING


def test_replay_has_exactly_two_sessions():
    # 噪声帧（session.reasoning.delta）不得新开会话
    state, _, _ = replay_fixture()
    assert set(state.sessions) == {A, B}