# -*- coding: utf-8 -*-
"""多键盘调度：0..N 把键盘共用同一份灯态。

职责：
  - 每 reconcile_interval 秒枚举已验证档案的配置通道；
  - 按设备路径增删 KeyboardLightPort，插拔自动生效；
  - 单设备故障隔离：校验不符 → 该设备停到拔插；发帧/打开失败 → 退避重试，
    连续 3 次失败后停到拔插；
  - 新接入的键盘立即补发当前灯态，不等下一次状态变化。

灯表在 hub 里留一份副本：新端口创建时重放，保证热插拔后立刻显示正确颜色。
线程：start() 起一个调度线程；apply()/open_count 由主循环线程调用，加锁同步。
"""
from __future__ import annotations

import logging
import threading
import time

from . import profiles
from .core import Light, SetKeyLight
from .keyboard import KeyboardLightPort

BACKOFF = (5.0, 15.0, 60.0)
MAX_STRIKES = 3


class KeyboardHub:
    """管理所有已连接键盘；没有键盘时保持空转，不影响守护其余部分。"""

    def __init__(self, log: logging.Logger, *, profile_names=None,
                 profile_list=None, on_wired_needed=None, fps: int = 20,
                 clock=time.monotonic, reconcile_interval: float = 2.0,
                 port_factory=None):
        self._log = log
        self._profile_names = list(profile_names) if profile_names else None
        self._profile_list = profile_list
        self._on_wired_needed = on_wired_needed
        self._fps = fps
        self._clock = clock
        self._reconcile_interval = reconcile_interval
        self._port_factory = port_factory or self._make_port

        self._lock = threading.Lock()
        self._lights: dict[str, Light] = {}
        self._ports: dict[str, KeyboardLightPort] = {}
        self._strikes: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._blocked: set[str] = set()
        self._port_profiles: dict[str, object] = {}
        self._logged: set[str] = set()

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned_24g = False

    # ---- 对外接口（主循环线程） ----

    def apply(self, effects) -> None:
        """记录灯态并转发给所有端口；新端口靠这份副本补齐。"""
        with self._lock:
            for e in effects:
                if not isinstance(e, SetKeyLight):
                    continue
                if e.light is Light.OFF:
                    self._lights.pop(e.key, None)
                else:
                    self._lights[e.key] = e.light
            ports = list(self._ports.values())
        for port in ports:
            port.apply(effects)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="keyboard-hub",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            ports = list(self._ports.values())
            self._ports.clear()
        for port in ports:
            try:
                port.stop()
            except Exception:
                self._log.debug("关闭键盘端口失败", exc_info=True)

    @property
    def open_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._ports.values() if p.state == "open")

    # ---- 调度线程 ----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._reconcile(self._clock())
            except Exception:
                self._log.debug("键盘调度失败", exc_info=True)
            self._stop.wait(self._reconcile_interval)

    def _reconcile(self, now: float) -> None:
        desired = self._enumerate()
        with self._lock:
            existing = dict(self._ports)

        for path, port in existing.items():
            if path in desired:
                continue
            profile = self._port_profiles.get(path)
            if profile is not None and port.state != "disabled":
                # 我们以 share=0 独占着这条通道，重新探测必然失败。
                # 「探测不到」不等于「设备已拔」：真拔线由端口的发帧失败反映。
                desired[path] = (profile, None)

        for path in list(existing):
            if path not in desired:
                self._drop_port(path)
                self._forget(path)
        for path in list(self._blocked | set(self._retry_at) | set(self._strikes)):
            if path not in desired:
                self._forget(path)   # 设备已拔：拉黑与退避记录一起清掉

        with self._lock:
            existing = dict(self._ports)
        for path, port in existing.items():
            if port.state == "disabled":
                self._handle_disabled(path, port)

        with self._lock:
            existing = dict(self._ports)
        for path, (profile, _info) in desired.items():
            if path in existing:
                if existing[path].state == "open":
                    self._strikes[path] = 0
                continue
            if path in self._blocked:
                continue
            if self._retry_at.get(path, 0.0) > now:
                continue
            self._spawn(path, profile)

        if not desired:
            self._check_24g()

    def _enumerate(self) -> dict:
        desired = {}
        for profile in self._profiles():
            try:
                infos = profile.list_channels()
            except Exception as exc:
                self._log.debug("枚举 %s 失败：%s", profile.capability.name, exc)
                continue
            for info in infos:
                desired[info.path] = (profile, info)
        return desired

    def _profiles(self) -> list:
        if self._profile_list is not None:
            return list(self._profile_list)
        if self._profile_names is None:
            return profiles.verified_profiles()
        out = []
        for name in self._profile_names:
            try:
                profile = profiles.get_profile(name)
            except ValueError as exc:
                self._log_once("unknown_" + name, str(exc), logging.WARNING)
                continue
            if not profile.capability.verified:
                self._log_once("unverified_" + name,
                               "机型 %s 未验证：%s" % (name, profile.capability.note),
                               logging.WARNING)
                continue
            out.append(profile)
        return out

    def _handle_disabled(self, path: str, port) -> None:
        kind = port.failure_kind
        self._drop_port(path)
        if kind == "validate":
            self._blocked.add(path)
            self._log_once("validate_" + path, "设备校验不符，停到拔插：%s" % path)
            return
        strikes = self._strikes.get(path, 0) + 1
        self._strikes[path] = strikes
        if strikes >= MAX_STRIKES:
            self._blocked.add(path)
            self._log_once("strikes_" + path,
                           "设备连续失败 %d 次，停到拔插：%s" % (strikes, path))
            return
        delay = BACKOFF[min(strikes - 1, len(BACKOFF) - 1)]
        self._retry_at[path] = self._clock() + delay
        self._log.info("键盘 %s 失败（第 %d 次），%ds 后重试",
                       path, strikes, int(delay))

    def _spawn(self, path: str, profile) -> None:
        try:
            port = self._port_factory(path, profile)
        except Exception as exc:
            self._log.warning("创建键盘端口失败 %s：%s", path, exc)
            return
        with self._lock:
            lights = dict(self._lights)
        if lights:
            port.apply([SetKeyLight(key=k, light=v) for k, v in lights.items()])
        port.start()
        with self._lock:
            self._ports[path] = port
            self._port_profiles[path] = profile
        self._log.info("键盘已接入：%s（%s）", path, profile.capability.name)

    def _drop_port(self, path: str) -> None:
        with self._lock:
            port = self._ports.pop(path, None)
            self._port_profiles.pop(path, None)
        if port is None:
            return
        try:
            port.stop()
        except Exception:
            self._log.debug("关闭键盘端口失败：%s", path, exc_info=True)
        self._log.info("键盘已移除：%s", path)

    def _forget(self, path: str) -> None:
        self._strikes.pop(path, None)
        self._retry_at.pop(path, None)
        self._blocked.discard(path)

    def _check_24g(self) -> None:
        if self._warned_24g:
            return
        for profile in self._profiles():
            try:
                if profile.detect_mode() != profiles.MODE_24G:
                    continue
            except Exception:
                continue
            self._warned_24g = True
            self._log.info("只检测到 2.4G 接收器：请换有线连接")
            if self._on_wired_needed is not None:
                try:
                    self._on_wired_needed()
                except Exception:
                    self._log.debug("on_wired_needed 回调失败", exc_info=True)
            return

    def _make_port(self, path: str, profile) -> KeyboardLightPort:
        return KeyboardLightPort(
            self._log, on_wired_needed=self._on_wired_needed, fps=self._fps,
            clock=self._clock, profile=profile, bound_path=path)

    def _log_once(self, key: str, message: str,
                  level: int = logging.INFO) -> None:
        if key in self._logged:
            return
        self._logged.add(key)
        self._log.log(level, message)