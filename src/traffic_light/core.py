"""纯核心状态机（主缝）：OpenCode 事件 → 灯态与效果。

无 I/O、无时间源：调用方传入 now_ms（毫秒），保证离线可测、可回放。
灯态：执行中=紫、失败=红、等待=黄（呼吸）、空闲=绿；无会话=灭。
等待保底可见 WAITING_MIN_VISIBLE_MS；失败保持到该会话下次开跑或成功（ADR-0002）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

WAITING_MIN_VISIBLE_MS = 500
AGGREGATE_KEY = "1"
FAILED_REASONS = ("shutdown", "inactivity")


class Light(Enum):
    OFF = "off"
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"
    WAITING = "waiting"


_PRIORITY = {Light.WAITING: 3, Light.FAILED: 2, Light.RUNNING: 1, Light.IDLE: 0, Light.OFF: -1}


@dataclass(frozen=True)
class SetKeyLight:
    key: str
    light: Light


@dataclass
class SessionState:
    session_id: str
    parent_id: str | None = None
    title: str | None = None
    directory: str | None = None
    running: bool = False
    failed: bool = False
    pending: dict[str, str] = field(default_factory=dict)
    waiting_visible_until: int = 0

    def light(self, now_ms: int) -> Light:
        if self.pending or now_ms < self.waiting_visible_until:
            return Light.WAITING
        if self.failed:
            return Light.FAILED
        if self.running:
            return Light.RUNNING
        return Light.IDLE


@dataclass
class GuardState:
    sessions: dict[str, SessionState] = field(default_factory=dict)
    display: Light = Light.OFF
    gone: set[str] = field(default_factory=set)  # 已归档/删除的 id，迟到事件不复活；unarchived/created/快照可解除

    def compute_display(self, now_ms: int) -> Light:
        if not self.sessions:
            return Light.OFF
        children: dict[str, list[str]] = {}
        for s in self.sessions.values():
            if s.parent_id:
                children.setdefault(s.parent_id, []).append(s.session_id)
        best = Light.OFF
        seen: set[str] = set()

        def group_light(start: str) -> Light:
            group = Light.OFF
            stack = [start]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                m = self.sessions.get(cur)
                if m is None:
                    continue
                lv = m.light(now_ms)
                if _PRIORITY[lv] > _PRIORITY[group]:
                    group = lv
                stack.extend(children.get(cur, ()))
            return group

        for s in self.sessions.values():
            if s.parent_id and s.parent_id in self.sessions:
                continue
            g = group_light(s.session_id)
            if _PRIORITY[g] > _PRIORITY[best]:
                best = g
        for s in self.sessions.values():  # 环成员兜底：不属于任何组的会话也算灯，不整体忽略
            if s.session_id in seen:
                continue
            g = group_light(s.session_id)
            if _PRIORITY[g] > _PRIORITY[best]:
                best = g
        return best


@dataclass(frozen=True)
class Event:
    type: str
    session_id: str | None = None
    reason: str | None = None
    parent_id: str | None = None
    title: str | None = None
    request_id: str | None = None
    directory: str | None = None


def _s(v):
    return v if isinstance(v, str) and v else None


def parse_event(raw: dict) -> Event | None:
    if not isinstance(raw, dict):
        return None
    t = _s(raw.get("type"))
    if t is None:
        return None
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    sid = _s(data.get("sessionID"))
    directory = None
    for d in (raw.get("location"), data.get("location")):
        if isinstance(d, dict) and _s(d.get("directory")):
            directory = _s(d.get("directory"))
            break
    reason = parent_id = title = request_id = None
    if t == "session.execution.interrupted":
        reason = _s(data.get("reason"))
    elif t == "session.created":
        parent_id = _s(data.get("parentID"))
        title = _s(data.get("title"))
    elif t == "session.renamed":
        title = _s(data.get("title"))
    elif t == "permission.asked":
        request_id = _s(data.get("id"))
    elif t == "permission.replied":
        request_id = _s(data.get("requestID"))
    elif t == "form.created":
        form = data.get("form")
        if isinstance(form, dict):
            request_id = _s(form.get("id"))
            if sid is None:
                sid = _s(form.get("sessionID"))
    elif t in ("form.replied", "form.cancelled"):
        request_id = _s(data.get("id"))
    return Event(t, sid, reason, parent_id, title, request_id, directory)
def apply_event(state: GuardState, ev: Event, now_ms: int) -> list[SetKeyLight]:
    effects: list[SetKeyLight] = []
    t, sid = ev.type, ev.session_id
    if sid and sid in state.gone and t not in ("session.created", "session.unarchived"):
        # 已归档/删除的会话：迟到的执行/等待类事件不复活它（ADR-0003：归档/删除释放）
        _refresh(state, now_ms, effects)
        return effects
    if t == "session.created":
        if sid:
            state.gone.discard(sid)
            s = _ensure(state, sid)
            if ev.parent_id:
                s.parent_id = ev.parent_id
            if ev.directory:
                s.directory = ev.directory
            if ev.title:
                s.title = ev.title
    elif t == "session.renamed":
        s = state.sessions.get(sid) if sid else None
        if s is not None and ev.title:
            s.title = ev.title
    elif t == "session.unarchived":
        if sid:
            state.gone.discard(sid)
            _ensure(state, sid)
    elif t in ("session.archived", "session.deleted"):
        if sid:
            state.sessions.pop(sid, None)
            state.gone.add(sid)
    elif t == "session.execution.started":
        if sid:
            s = _ensure(state, sid)
            s.running, s.failed = True, False
    elif t == "session.execution.succeeded":
        if sid:
            s = _ensure(state, sid)
            s.running, s.failed = False, False
    elif t == "session.execution.failed":
        if sid:
            s = _ensure(state, sid)
            s.running, s.failed = False, True
    elif t == "session.execution.interrupted":
        if sid:
            s = _ensure(state, sid)
            s.running = False
            s.failed = ev.reason in FAILED_REASONS
    elif t == "permission.asked":
        if sid:
            _begin_pending(_ensure(state, sid), "permission",
                           ev.request_id or "permission:" + sid, now_ms)
    elif t == "permission.replied":
        s = state.sessions.get(sid) if sid else None
        if s is not None:
            if ev.request_id:
                s.pending.pop(ev.request_id, None)
            else:
                for k in [k for k, v in s.pending.items() if v == "permission"]:
                    s.pending.pop(k, None)
    elif t == "form.created":
        if sid:
            _begin_pending(_ensure(state, sid), "form",
                           ev.request_id or "form:" + sid, now_ms)
    elif t in ("form.replied", "form.cancelled"):
        s = state.sessions.get(sid) if sid else None
        if s is not None and ev.request_id:
            s.pending.pop(ev.request_id, None)
    _refresh(state, now_ms, effects)
    return effects


def apply_enrichment(state: GuardState, sid: str, *, title: str | None = None,
                     directory: str | None = None, parent_id: str | None = None,
                     now_ms: int) -> list[SetKeyLight]:
    """把会话详情（标题/目录/父会话，取自 GET /api/session/:id）并入追踪状态。"""
    effects: list[SetKeyLight] = []
    s = state.sessions.get(sid)
    if s is not None:
        if title:
            s.title = title
        if directory:
            s.directory = directory
        s.parent_id = parent_id
        _refresh(state, now_ms, effects)
    return effects


@dataclass
class SessionSnapshot:
    session_id: str
    parent_id: str | None = None
    title: str | None = None
    directory: str | None = None
    running: bool = False
    failed: bool = False
    pending: dict[str, str] = field(default_factory=dict)
    outcome: str | None = None  # REST outcome（succeeded/failed/interrupted），重建补丁区分中断歧义用


@dataclass
class Snapshot:
    sessions: list[SessionSnapshot] = field(default_factory=list)
    all_ids: set[str] | None = None  # None = 列表不完整，跳过缺席清理


def apply_snapshot(state: GuardState, snap: Snapshot, now_ms: int) -> list[SetKeyLight]:
    effects: list[SetKeyLight] = []
    if snap.all_ids is not None:
        for sid in list(state.sessions):
            if sid not in snap.all_ids:
                del state.sessions[sid]
                state.gone.add(sid)
    for inst in snap.sessions:
        sid = inst.session_id
        s = state.sessions.get(sid)
        if s is None:
            if not (inst.running or inst.pending):
                continue
            s = SessionState(session_id=sid)
            state.sessions[sid] = s
        state.gone.discard(sid)
        s.parent_id = inst.parent_id
        if inst.directory:
            s.directory = inst.directory
        if inst.title and not s.title:
            s.title = inst.title
        s.running = inst.running
        if inst.failed:
            s.failed = True
        elif inst.running or inst.outcome == "succeeded":
            s.failed = False
        # outcome=interrupted/未知且未运行：中断原因在 REST 里不可分辨，保持现有红
        # （ADR-0002：红不因歧义清除，等下次开跑或成功）
        had = bool(s.pending)
        s.pending = dict(inst.pending)
        if s.pending and not had:
            s.waiting_visible_until = now_ms + WAITING_MIN_VISIBLE_MS
    _refresh(state, now_ms, effects)
    return effects


def tick(state: GuardState, now_ms: int) -> list[SetKeyLight]:
    """时间推进：让等待保底到期之类的转变生效。"""
    effects: list[SetKeyLight] = []
    _refresh(state, now_ms, effects)
    return effects


def _ensure(state: GuardState, sid: str) -> SessionState:
    s = state.sessions.get(sid)
    if s is None:
        s = SessionState(session_id=sid)
        state.sessions[sid] = s
    return s


def _begin_pending(s: SessionState, kind: str, request_id: str, now_ms: int) -> None:
    had = bool(s.pending)
    s.pending[request_id] = kind
    if not had:
        s.waiting_visible_until = now_ms + WAITING_MIN_VISIBLE_MS


def _refresh(state: GuardState, now_ms: int, effects: list[SetKeyLight]) -> None:
    new = state.compute_display(now_ms)
    if new is not state.display:
        state.display = new
        effects.append(SetKeyLight(AGGREGATE_KEY, new))