"""守护主循环：服务发现 → SSE 事件流 → 核心状态机 → 键盘灯 + 通知。

只读旁观：全程只用 GET 接口；dry_run 只写日志、不碰键盘、不发通知，也跳过单实例互斥。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import urllib.request
from pathlib import Path

from . import core
from . import notify as notify_mod
from . import profiles
from . import runtime
from . import settings as settings_mod
from . import tray as tray_mod
from .core import GuardState
from .discovery import (ServiceFileError, ServiceInfo, StaleServiceError,
                        basic_auth_header, check_consistency, load_service_file)
from .dryrun import DryRunLightPort, LIGHT_CN
from .keyboard_hub import KeyboardHub
from .rest import RestClient, build_snapshot, fetch_info
from .sse import SSEFrameParser

_log = logging.getLogger("traffic_light")

_TICK_SECONDS = 0.1
_READ_TIMEOUT = 120.0
_RECONNECT_BACKOFF = (1, 2, 5, 10)
_DISCOVER_RETRY_SECONDS = 5.0


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _keyboard_count(light) -> int:
    count = getattr(light, "open_count", None)
    if count is not None:
        return count
    return 1 if getattr(light, "state", "") == "open" else 0


def _pump(tray, state: GuardState, light, connected: bool) -> None:
    """把聚合灯态推给托盘；tray 为 None 时什么都不做。"""
    if tray is None:
        return
    tray.update(state.display, session_count=len(state.sessions),
                keyboard_count=_keyboard_count(light), connected=connected)


def _dispatch(light, notifier, effects) -> None:
    """效果分流：SetKeyLight → 灯口；Notify → 通知口。"""
    light_effects = [e for e in effects if isinstance(e, core.SetKeyLight)]
    if light_effects:
        light.apply(light_effects)
    for e in effects:
        if isinstance(e, core.Notify):
            notifier.send(notify_mod.NotifyRequest(
                kind=e.kind, session_id=e.session_id, slot=e.slot,
                title=e.title, project=e.project, detail=e.detail))


def _discover(service_json: Path, stop: threading.Event) -> ServiceInfo | None:
    """循环探测，直到拿到「service.json 与 /api/info 一致」的服务；被停止则返回 None。"""
    last_msg = None
    while not stop.is_set():
        try:
            info = load_service_file(service_json)
            check_consistency(info, fetch_info(info))
            _log.info("发现 OpenCode 服务：v%s %s（pid %s）", info.version, info.url, info.pid)
            return info
        except FileNotFoundError:
            msg = f"未找到 {service_json}（OpenCode 未运行？）"
        except (ServiceFileError, StaleServiceError) as exc:
            msg = f"service.json 无效或已过期：{exc}"
        except Exception as exc:  # 网络错误等
            msg = f"服务探测失败：{exc}"
        if msg != last_msg:
            _log.info("%s；%ss 后重试", msg, int(_DISCOVER_RETRY_SECONDS))
            last_msg = msg
        stop.wait(_DISCOVER_RETRY_SECONDS)
    return None


def _stream_once(info: ServiceInfo, stop: threading.Event, out: queue.Queue) -> None:
    """一次 SSE 连接：帧进队列；断开/出错时放入 ("disconnected", 原因)。"""
    parser = SSEFrameParser()
    try:
        req = urllib.request.Request(
            info.url.rstrip("/") + "/api/event",
            headers={"Authorization": basic_auth_header(info.password)},
        )
        with urllib.request.urlopen(req, timeout=_READ_TIMEOUT) as resp:
            out.put(("connected", None))
            for raw in resp:
                if stop.is_set():
                    return
                frame = parser.feed(raw.decode("utf-8", errors="replace"))
                if frame is not None:
                    out.put(("frame", frame))
    except Exception as exc:
        out.put(("disconnected", str(exc)))
        return
    out.put(("disconnected", "流结束"))


def _enrich(info: ServiceInfo, state: GuardState,
            light: DryRunLightPort | KeyboardHub,
            notifier: notify_mod.Notifier,
            sid: str, fetched: set[str]) -> None:
    """为新出现的会话补取详情（标题/目录/父会话）。每会话至多取一次。"""
    fetched.add(sid)
    try:
        detail = RestClient(info).session_detail(sid)
    except Exception as exc:
        _log.debug("取会话详情失败 %s：%s", sid, exc)
        return
    effects = core.apply_enrichment(
        state,
        sid,
        title=detail.get("title"),
        directory=(detail.get("location") or {}).get("directory"),
        parent_id=detail.get("parentID"),
        now_ms=_now_ms(),
    )
    _dispatch(light, notifier, effects)


def _handle_frame(info: ServiceInfo, state: GuardState,
                  light: DryRunLightPort | KeyboardHub,
                  notifier: notify_mod.Notifier,
                  frame: dict, fetched: set[str]) -> None:
    ev = core.parse_event(frame)
    if ev is None:
        return
    _dispatch(light, notifier, core.apply_event(state, ev, _now_ms()))
    sid = ev.session_id
    if sid and sid not in fetched:
        sess = state.sessions.get(sid)
        if sess is not None and not sess.title:
            _enrich(info, state, light, notifier, sid, fetched)


def _session_loop(info: ServiceInfo, state: GuardState,
                  light: DryRunLightPort | KeyboardHub,
                  notifier: notify_mod.Notifier,
                  stop: threading.Event, tray=None) -> bool:
    """一次连接生命周期：先 REST 重建，再消费事件；返回是否成功连上过。"""
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_stream_once, args=(info, stop, q),
                     daemon=True, name="sse-stream").start()

    try:
        snap = build_snapshot(RestClient(info))
        _dispatch(light, notifier, core.apply_snapshot(state, snap, _now_ms()))
        _log.info("状态重建完成：追踪 %d 个会话（键1 → %s）",
                  len(state.sessions), LIGHT_CN[state.display])
    except Exception as exc:
        _log.warning("状态重建失败（先只收事件，下次重连再试）：%s", exc)

    fetched: set[str] = set()
    connected = False
    while not stop.is_set():
        try:
            kind, payload = q.get(timeout=_TICK_SECONDS)
        except queue.Empty:
            _dispatch(light, notifier, core.tick(state, _now_ms()))
            _pump(tray, state, light, connected)
            continue
        if kind == "connected":
            connected = True
            _pump(tray, state, light, True)
            _log.debug("事件流已连接")
        elif kind == "frame":
            try:
                _handle_frame(info, state, light, notifier, payload, fetched)
            except Exception as exc:
                _log.debug("事件处理失败：%s", exc)
            _pump(tray, state, light, connected)
        elif kind == "disconnected":
            _pump(tray, state, light, False)
            _log.info("事件流断开：%s", payload)
            return connected
    return connected


def make_wireless_notice(settings, store, notifier):
    """一次性说明：只弹第一次，弹过就记进设置里（重启不再打扰）。

    会被两个线程调（调度线程的「接管了」、托盘线程的「打开了」），
    flag 的检查与置位必须在锁里，否则两次接管可能弹两个气泡。
    """
    guard = threading.Lock()

    def notice() -> None:
        with guard:
            if settings.wireless_notice_shown:
                return
            settings.wireless_notice_shown = True
        store.save(settings)
        notifier.send(notify_mod.NotifyRequest(kind="wireless_tip"))
    return notice


def make_wireless_toggle(settings, store, light, notice):
    """托盘开关：先存盘，再切通道；打开时补一次说明。

    存盘在前：哪怕后面切通道抛异常，用户的选择也不会丢。
    """
    def toggle(enabled: bool) -> None:
        settings.wireless_light = enabled
        store.save(settings)
        setter = getattr(light, "set_wireless_enabled", None)
        if setter is not None:
            setter(enabled)
        if enabled:
            notice()
    return toggle


def run(service_json: str | Path, verbose: bool = False, dry_run: bool = False,
        profile_name: str = "auto", use_tray: bool = True) -> int:
    """跑守护主循环。Ctrl+C 退出；dry_run 只写日志、不碰键盘、不发通知。"""
    runtime.setup_logging(verbose)
    if dry_run:
        _log.debug("dry-run：跳过单实例互斥")
    elif not runtime.acquire_single_instance():
        _log.error("已有守护实例在运行，退出")
        return 1
    service_json = Path(service_json)
    stop = threading.Event()
    state = GuardState()
    store = settings_mod.SettingsStore(
        runtime.DEFAULT_LOG_DIR / settings_mod.SETTINGS_FILE_NAME, _log)
    settings = store.load()
    tray = None
    if use_tray:
        tray = tray_mod.TrayIcon(
            _log, on_quit=stop.set,
            log_path=runtime.DEFAULT_LOG_DIR / runtime.LOG_FILE_NAME,
            dry_run=dry_run, wireless_light=settings.wireless_light)
        tray.start()
        if not tray.available:
            tray = None
    notifier = notify_mod.create_notifier(
        dry_run, _log, balloon=tray.balloon if tray is not None else None)
    maybe_wireless_notice = make_wireless_notice(settings, store, notifier)

    if dry_run:
        light = DryRunLightPort(_log)
    else:
        names = None if profile_name == "auto" else [profile_name]
        light = KeyboardHub(
            _log, profile_names=names,
            on_wired_needed=lambda: notifier.send(notify_mod.NotifyRequest(kind="cable")),
            wireless_enabled=settings.wireless_light,
            on_wireless_active=maybe_wireless_notice,
        )
        light.start()

    if tray is not None:
        tray.set_on_wireless_toggle(
            make_wireless_toggle(settings, store, light, maybe_wireless_notice))
    _pump(tray, state, light, False)
    attempt = 0
    try:
        while not stop.is_set():
            info = _discover(service_json, stop)
            if info is None:
                break
            if _session_loop(info, state, light, notifier, stop, tray):
                attempt = 0
            else:
                attempt += 1
            if stop.is_set():
                break
            delay = _RECONNECT_BACKOFF[min(max(attempt - 1, 0), len(_RECONNECT_BACKOFF) - 1)]
            _log.info("重连中（%ss 后）…", delay)
            stop.wait(delay)
    except KeyboardInterrupt:
        _log.info("收到 Ctrl+C，退出")
    finally:
        if not dry_run:
            light.stop()
        if tray is not None:
            tray.close()
    return 0
