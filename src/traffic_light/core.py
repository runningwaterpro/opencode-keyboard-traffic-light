"""纯核心状态机（主缝）：OpenCode 事件 → 灯态 / 槽位 / 通知效果。

无 I/O、无时间源：调用方传入 now_ms（毫秒），保证离线可测、可回放。
- 灯态：执行中=紫、失败=红、等待=黄（呼吸）、完成=绿（30 秒后灭）、其余=灭。
- 槽位（ADR-0003）：会话第一次出现占最小空号（1→…→9→0），已占不重排；
  归档/删除释放；满时先淘汰最久空闲 → 最早的失败 → 否则等待；子会话不占号。
- 通知（票⑥）：四类触发 + 静默规则，见 apply_event 内注释。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

WAITING_MIN_VISIBLE_MS = 500
IDLE_VISIBLE_MS = 60_000        # 完成确认绿灯；到期即灭
WAITING_MAX_MS = 600_000        # 键盘黄灯最长 10 分钟，之后交给托盘
FAILED_MAX_MS = 300_000         # 键盘红灯最长 5 分钟，之后交给托盘
FAILED_REASONS = ("shutdown", "inactivity")
SLOT_KEYS = ("1", "2", "3", "4", "5", "6", "7", "8", "9", "0")
COMPLETION_MIN_RUN_MS = 5000


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


@dataclass(frozen=True)
class Notify:
    """通知效果（由通知口渲染成 toast；字段与 notify.NotifyRequest 对齐）。"""
    kind: str  # "permission" | "question" | "completed" | "failed" | "interrupted"
    session_id: str | None = None
    slot: str | None = None
    title: str | None = None
    project: str | None = None
    detail: str | None = None


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
    waiting_since_ms: int | None = None   # 本轮等待开始时刻；黄灯显示上限用它
    slot: str | None = None
    first_seen_ms: int = 0
    run_started_ms: int | None = None
    completion_pending: bool = False
    idle_since_ms: int | None = None
    failed_since_ms: int | None = None
    wait_slot: bool = False
    completed_ms: int | None = None  # 最近一次成功的时刻；绿灯只在窗口内亮

    def settled_light(self, now_ms: int) -> Light:
        """不含「完成确认」衰减的灯态：排序与淘汰用它（空闲是状态，不是灯）。"""
        if self.pending or now_ms < self.waiting_visible_until:
            return Light.WAITING
        if self.failed:
            return Light.FAILED
        if self.running:
            return Light.RUNNING
        return Light.IDLE

    def light(self, now_ms: int, keyboard: bool = False) -> Light:
        """显示灯态：完成后的绿只在窗口内亮，其余空闲一律灭。

        keyboard=True 是「键盘视图」：执行中不亮，黄/红有显示上限。
        托盘仍用不带 keyboard 的版本——它负责长期记账，不能被这些上限抹掉。
        """
        kind = self.settled_light(now_ms)
        if kind is Light.IDLE:
            if self.completed_ms is not None and now_ms - self.completed_ms < IDLE_VISIBLE_MS:
                return Light.IDLE
            return Light.OFF
        if not keyboard:
            return kind
        if kind is Light.WAITING:
            if (self.pending and self.waiting_since_ms is not None
                    and now_ms - self.waiting_since_ms >= WAITING_MAX_MS):
                return Light.OFF
            return kind
        if kind is Light.FAILED:
            if (self.failed_since_ms is not None
                    and now_ms - self.failed_since_ms >= FAILED_MAX_MS):
                return Light.OFF
        return kind


def project_name(directory: str | None) -> str | None:
    """项目名 = 目录的文件夹名。"""
    if not directory:
        return None
    name = os.path.basename(directory.rstrip("\\/"))
    return name or None

@dataclass
class GuardState:
    sessions: dict[str, SessionState] = field(default_factory=dict)
    rendered: dict[str, Light] = field(default_factory=dict)  # 键 → 已渲染灯态（缺省=灭）
    display: Light = Light.OFF  # 兼容别名：全部槽位的最高优先级（无槽=灭）
    gone: set[str] = field(default_factory=set)  # 已归档/删除的 id，迟到事件不复活

    def _children_map(self) -> dict[str, list[str]]:
        children: dict[str, list[str]] = {}
        for s in self.sessions.values():
            if s.parent_id:
                children.setdefault(s.parent_id, []).append(s.session_id)
        return children

    def group_light(self, sid: str, now_ms: int, children: dict[str, list[str]] | None = None,
                    keyboard: bool = False) -> Light:
        """sid 组（自己 + 全部后裔）的最高优先级灯态。"""
        if children is None:
            children = self._children_map()
        best = Light.OFF
        seen: set[str] = set()
        stack = [sid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            m = self.sessions.get(cur)
            if m is None:
                continue
            lv = m.light(now_ms, keyboard=keyboard)
            if _PRIORITY[lv] > _PRIORITY[best]:
                best = lv
            stack.extend(children.get(cur, ()))
        return best

    def group_is_settled_idle(self, sid: str, now_ms: int,
                              children: dict[str, list[str]] | None = None) -> bool:
        """整组是否都已停下（不看绿灯衰减）：淘汰排序用。"""
        if children is None:
            children = self._children_map()
        seen: set[str] = set()
        stack = [sid]
        members = 0
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            m = self.sessions.get(cur)
            if m is None:
                continue
            members += 1
            if m.settled_light(now_ms) is not Light.IDLE:
                return False
            stack.extend(children.get(cur, ()))
        return members > 0

    def root_of(self, sid: str) -> str:
        """沿 parent_id 上溯到最顶层已追踪祖先（环安全）。"""
        cur = sid
        seen: set[str] = set()
        while cur not in seen:
            seen.add(cur)
            s = self.sessions.get(cur)
            if s is None or not s.parent_id or s.parent_id not in self.sessions:
                return cur
            cur = s.parent_id
        return sid

    def slot_of(self, sid: str) -> str | None:
        """有效槽位：折叠后的顶层祖先的号（子会话=父的号）。"""
        s = self.sessions.get(self.root_of(sid))
        return s.slot if s else None

    def _is_root(self, s: SessionState) -> bool:
        return s.parent_id is None or s.parent_id not in self.sessions

    def assign_slots(self, now_ms: int) -> None:
        """给无槽根会话按出现顺序补号；新到且满格时按规则淘汰一次；等号者不反复淘汰。"""
        assigned = {s.slot: s for s in self.sessions.values() if s.slot}
        waiting = sorted((s for s in self.sessions.values() if self._is_root(s) and not s.slot),
                         key=lambda x: (x.first_seen_ms, x.session_id))
        children = self._children_map()
        for s in waiting:
            free = next((k for k in SLOT_KEYS if k not in assigned), None)
            if free is None and not s.wait_slot:
                victim = self._pick_victim(assigned, children, now_ms)
                if victim is not None and victim.slot:
                    free = victim.slot
                    victim.slot = None
                    victim.wait_slot = True
                    del assigned[free]
            if free is None:
                s.wait_slot = True
                continue
            s.slot = free
            s.wait_slot = False
            assigned[free] = s
        # 子会话不占号：后来被认领为子会话的，释放其号
        for s in self.sessions.values():
            if s.slot and not self._is_root(s):
                assigned.pop(s.slot, None)
                s.slot = None
                s.wait_slot = True

    def _pick_victim(self, assigned: dict, children: dict, now_ms: int) -> SessionState | None:
        """先挑最久空闲（整组已停下），再挑最早的失败；都没有→None（等腾出）。"""
        idle = [s for s in assigned.values()
                if self.group_is_settled_idle(s.session_id, now_ms, children)
                and s.idle_since_ms is not None]
        if idle:
            return min(idle, key=lambda x: x.idle_since_ms)
        failed = [s for s in assigned.values()
                  if self.group_light(s.session_id, now_ms, children) is Light.FAILED
                  and s.failed_since_ms is not None]
        if failed:
            return min(failed, key=lambda x: x.failed_since_ms)
        return None

    def _sync_ordering(self, now_ms: int) -> None:
        for s in self.sessions.values():
            own = s.settled_light(now_ms)
            if own is Light.IDLE:
                if s.idle_since_ms is None:
                    s.idle_since_ms = now_ms
            else:
                s.idle_since_ms = None
            if own is Light.FAILED:
                if s.failed_since_ms is None:
                    s.failed_since_ms = now_ms
            else:
                s.failed_since_ms = None

    def render_lights(self, now_ms: int) -> dict[str, Light]:
        """键盘视图：执行中不亮，黄/红有上限（托盘用 state.display，不受这些限制）。"""
        children = self._children_map()
        out: dict[str, Light] = {}
        for s in self.sessions.values():
            if s.slot:
                out[s.slot] = self.group_light(s.session_id, now_ms, children,
                                               keyboard=True)
        return out

def _notify(state: GuardState, kind: str, sid: str, effects: list, detail: str | None = None) -> None:
    """构造通知效果：槽位取折叠后的有效槽位；标题/项目取当前会话状态。"""
    s = state.sessions.get(sid)
    if s is None:
        return
    effects.append(Notify(
        kind=kind,
        session_id=sid,
        slot=state.slot_of(sid),
        title=s.title,
        project=project_name(s.directory),
        detail=detail,
    ))


def _has_running_descendants(state: GuardState, sid: str) -> bool:
    children = state._children_map()
    seen: set[str] = set()
    stack = list(children.get(sid, ()))
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        m = state.sessions.get(cur)
        if m is not None and m.running:
            return True
        stack.extend(children.get(cur, ()))
    return False


def _maybe_finish_deferred(state: GuardState, sid: str, now_ms: int, effects: list) -> None:
    """子会话全部停止后，沿祖先链补发挂起的完成通知。"""
    cur = sid
    seen: set[str] = set()
    while cur not in seen:
        seen.add(cur)
        s = state.sessions.get(cur)
        if s is None or not s.parent_id or s.parent_id not in state.sessions:
            return
        parent = state.sessions[s.parent_id]
        cur = parent.session_id
        if parent.completion_pending and not _has_running_descendants(state, cur):
            parent.completion_pending = False
            _notify(state, "completed", cur, effects)


def apply_event(state: GuardState, ev: Event, now_ms: int) -> list:
    effects: list = []
    t, sid = ev.type, ev.session_id
    if sid and sid in state.gone and t not in ("session.created", "session.unarchived"):
        # 已归档/删除的会话：迟到的执行/等待类事件不复活它（ADR-0003：归档/删除释放）
        _refresh(state, now_ms, effects)
        return effects
    if t == "session.created":
        if sid:
            state.gone.discard(sid)
            s = _ensure(state, sid, now_ms)
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
            _ensure(state, sid, now_ms)
    elif t in ("session.archived", "session.deleted"):
        if sid:
            state.sessions.pop(sid, None)
            state.gone.add(sid)
    elif t == "session.execution.started":
        if sid:
            s = _ensure(state, sid, now_ms)
            s.running, s.failed = True, False
            s.completion_pending = False
            s.completed_ms = None
            if s.run_started_ms is None:
                s.run_started_ms = now_ms
    elif t == "session.execution.succeeded":
        if sid:
            s = _ensure(state, sid, now_ms)
            s.running, s.failed = False, False
            s.completed_ms = now_ms
            duration = (now_ms - s.run_started_ms) if s.run_started_ms is not None else None
            s.run_started_ms = None
            if state._is_root(s) and (duration is None or duration >= COMPLETION_MIN_RUN_MS):
                if _has_running_descendants(state, sid):
                    s.completion_pending = True
                else:
                    _notify(state, "completed", sid, effects)
            _maybe_finish_deferred(state, sid, now_ms, effects)
    elif t == "session.execution.failed":
        if sid:
            s = _ensure(state, sid, now_ms)
            s.running, s.failed = False, True
            s.completed_ms = None
            s.run_started_ms = None
            s.completion_pending = False
            _notify(state, "failed", sid, effects)
            _maybe_finish_deferred(state, sid, now_ms, effects)
    elif t == "session.execution.interrupted":
        if sid:
            s = _ensure(state, sid, now_ms)
            s.running = False
            s.failed = ev.reason in FAILED_REASONS
            s.completed_ms = None
            s.run_started_ms = None
            s.completion_pending = False
            if ev.reason in FAILED_REASONS:
                _notify(state, "interrupted", sid, effects)
            _maybe_finish_deferred(state, sid, now_ms, effects)
    elif t == "permission.asked":
        if sid:
            s = _ensure(state, sid, now_ms)
            _begin_pending(s, "permission", ev.request_id or "permission:" + sid, now_ms)
            state._sync_ordering(now_ms)
            state.assign_slots(now_ms)
            _notify(state, "permission", sid, effects, detail=ev.detail)
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
            s = _ensure(state, sid, now_ms)
            _begin_pending(s, "form", ev.request_id or "form:" + sid, now_ms)
            state._sync_ordering(now_ms)
            state.assign_slots(now_ms)
            _notify(state, "question", sid, effects)
    elif t in ("form.replied", "form.cancelled"):
        s = state.sessions.get(sid) if sid else None
        if s is not None and ev.request_id:
            s.pending.pop(ev.request_id, None)
    _refresh(state, now_ms, effects)
    return effects

@dataclass(frozen=True)
class Event:
    type: str
    session_id: str | None = None
    reason: str | None = None
    parent_id: str | None = None
    title: str | None = None
    request_id: str | None = None
    directory: str | None = None
    detail: str | None = None


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
    reason = parent_id = title = request_id = detail = None
    if t == "session.execution.interrupted":
        reason = _s(data.get("reason"))
    elif t == "session.created":
        parent_id = _s(data.get("parentID"))
        title = _s(data.get("title"))
    elif t == "session.renamed":
        title = _s(data.get("title"))
    elif t == "permission.asked":
        request_id = _s(data.get("id"))
        res = data.get("resources")
        if isinstance(res, list):
            for item in res:
                if _s(item):
                    detail = _s(item)
                    break
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
    return Event(t, sid, reason, parent_id, title, request_id, directory, detail)

def apply_enrichment(state: GuardState, sid: str, *, title: str | None = None,
                     directory: str | None = None, parent_id: str | None = None,
                     now_ms: int) -> list:
    """把会话详情（标题/目录/父会话）并入追踪状态。"""
    effects: list = []
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

def apply_snapshot(state: GuardState, snap: Snapshot, now_ms: int) -> list:
    effects: list = []
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
            s.first_seen_ms = now_ms
            state.sessions[sid] = s
        state.gone.discard(sid)
        s.parent_id = inst.parent_id
        if inst.directory:
            s.directory = inst.directory
        if inst.title and not s.title:
            s.title = inst.title
        s.running = inst.running
        s.completed_ms = None  # 重建不知道完成时刻：不假装新鲜
        if inst.failed:
            s.failed = True
        elif inst.running or inst.outcome == "succeeded":
            s.failed = False
        # outcome=interrupted/未知且未运行：中断原因不可分辨，保持现有红（ADR-0002）
        had = bool(s.pending)
        s.pending = dict(inst.pending)
        if s.pending and not had:
            s.waiting_visible_until = now_ms + WAITING_MIN_VISIBLE_MS
            s.waiting_since_ms = now_ms
        if inst.running and s.run_started_ms is None:
            s.run_started_ms = now_ms
    _refresh(state, now_ms, effects)
    return effects


def tick(state: GuardState, now_ms: int) -> list:
    """时间推进：让等待保底到期之类的转变生效。"""
    effects: list = []
    _refresh(state, now_ms, effects)
    return effects


def _ensure(state: GuardState, sid: str, now_ms: int) -> SessionState:
    s = state.sessions.get(sid)
    if s is None:
        s = SessionState(session_id=sid)
        s.first_seen_ms = now_ms
        state.sessions[sid] = s
    return s


def _begin_pending(s: SessionState, kind: str, request_id: str, now_ms: int) -> None:
    had = bool(s.pending)
    s.pending[request_id] = kind
    if not had:
        s.waiting_visible_until = now_ms + WAITING_MIN_VISIBLE_MS
        s.waiting_since_ms = now_ms


def _refresh(state: GuardState, now_ms: int, effects: list) -> None:
    state._sync_ordering(now_ms)
    state.assign_slots(now_ms)
    desired = state.render_lights(now_ms)
    for key in SLOT_KEYS:
        new = desired.get(key, Light.OFF)
        old = state.rendered.get(key, Light.OFF)
        if new is not old:
            effects.append(SetKeyLight(key, new))
            if new is Light.OFF:
                state.rendered.pop(key, None)
            else:
                state.rendered[key] = new
    best = Light.OFF
    children = state._children_map()
    for s in state.sessions.values():
        if s.slot:
            lv = state.group_light(s.session_id, now_ms, children)   # 不带上限：托盘记账
            if _PRIORITY[lv] > _PRIORITY[best]:
                best = lv
    state.display = best