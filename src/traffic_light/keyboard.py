# -*- coding: utf-8 -*-
"""键盘灯光口：设备状态机 + 连续发帧循环。

设备 I/O 全部经 device.py；本文件只管状态与节奏，不开第二处设备调用点。
安全红线：
  1. 一次失败即停：校验不符 / 独占打开失败 / 发帧失败且通道仍在 → disabled，永不重试。
  2. OemDrv.exe 在跑就避让：关句柄、不打开；退出后自动恢复。
  3. 发帧失败且通道消失（多半是拔线）→ suspended，插回自动恢复。
  4. 2.4G 接收器不是配置通道：只提示一次「请换有线连接」，绝不尝试打开。

状态 self._state ∈ idle / waiting / hold_oem / open / suspended / disabled。
_step(now) 是单步状态机（测试注入 now 直接驱动）；_run 是按 fps 的后台循环。
灯表 self._lights：主循环线程 apply 写、后台线程 _step 取副本，加锁同步。
"""
from __future__ import annotations

import logging
import threading
import time

from . import device, frame
from .core import Light, SetKeyLight


class KeyboardLightPort:
    """把 SetKeyLight 效果变成 520 字节帧，发给有线连接的 Hi86 配置通道。"""

    def __init__(self, log, *, on_wired_needed=None, fps: int = 20,
                 device_check_interval: float = 2.0, oem_check_interval: float = 5.0,
                 clock=time.monotonic):
        self._log = log
        self._on_wired_needed = on_wired_needed
        self._fps = fps
        self._device_check_interval = device_check_interval
        self._oem_check_interval = oem_check_interval
        self._clock = clock

        self._lock = threading.Lock()
        self._lights: dict[str, Light] = {}

        self._handle = None
        self._state = "idle"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # 周期检查时间表：-inf 表示「第一次 _step 立即查一次」。
        self._next_oem_check = float("-inf")
        self._next_device_check = float("-inf")
        self._oem_running = False
        self._logged: set[str] = set()

    # ---- 对外接口（主循环线程调用） ----

    def apply(self, effects) -> None:
        """OFF 从灯表移除该键，其余记入；非 SetKeyLight 效果忽略。"""
        with self._lock:
            for e in effects:
                if not isinstance(e, SetKeyLight):
                    continue
                if e.light is Light.OFF:
                    self._lights.pop(e.key, None)
                else:
                    self._lights[e.key] = e.light

    def start(self) -> None:
        """启动后台发帧线程（daemon：主程序退出即随进程结束）。"""
        if self._thread is not None and self._thread.is_alive():
            return  # 重复 start 不叠加线程
        self._thread = threading.Thread(target=self._run, name="keyboard-light",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """置停止事件、join(timeout=2)、关句柄。可重复调用。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._handle is not None:
            device.close(self._handle)
            self._handle = None

    # ---- 后台循环与状态机 ----

    def _run(self) -> None:
        interval = 1.0 / self._fps
        while not self._stop.is_set():
            self._step(self._clock())
            self._stop.wait(interval)

    def _step(self, now: float) -> None:
        """单步状态机：_run 每 1/fps 秒调一次；测试注入 now 直接驱动。"""
        if self._state == "disabled":
            return  # 一次失败即停：永不重试
        if self._poll_oem(now):
            self._hold_for_oem()
            return
        if self._handle is not None:
            self._send_frame(now)
            return
        self._check_device(now)

    def _poll_oem(self, now: float) -> bool:
        """每 oem_check_interval 秒刷新一次 OemDrv 状态；返回「是否在跑」。

        查询异常按「在跑」处理：查不到就是不确定，不确定就不碰设备（不抢占）。
        """
        if now >= self._next_oem_check:
            self._next_oem_check = now + self._oem_check_interval
            try:
                self._oem_running = bool(device.oemdrv_running())
            except Exception:
                self._oem_running = True
                self._log.debug("OemDrv 检测失败，按在跑处理", exc_info=True)
        return self._oem_running

    def _hold_for_oem(self) -> None:
        if self._handle is not None:
            device.close(self._handle)
            self._handle = None
            self._log_once("hold_oem", "OemDrv.exe 在运行，暂停灯光")
        self._state = "hold_oem"

    def _check_device(self, now: float) -> None:
        """无句柄时的设备检查：每 device_check_interval 秒一次 detect_mode。"""
        if now < self._next_device_check:
            return
        self._next_device_check = now + self._device_check_interval
        try:
            mode = device.detect_mode()
        except Exception as exc:
            # 枚举失败不是设备事实：不改状态，下一轮到期再试（不因一次读失败停灯）。
            self._log.debug("设备检测失败：%s", exc)
            return
        if mode == device.MODE_WIRED:
            self._open_wired()
        elif mode == device.MODE_24G:
            self._state = "waiting"
            self._notify_wired_needed()
        else:
            self._state = "waiting"
            self._log_once("none", "未检测到键盘，等待连接")

    def _open_wired(self) -> None:
        """wired：复核配置通道后独占打开；任何一步不过 → disabled，一次失败即停。"""
        try:
            info = device.find_config_channel()
        except Exception as exc:
            self._log.debug("配置通道查询失败：%s", exc)
            return
        if info is None:
            self._disable("校验不符：未找到配置通道，停止灯光")
            return
        try:
            ok, why = device.validate_channel(info)
        except Exception as exc:
            self._disable("校验不符：复核出错（%s），停止灯光" % (exc,))
            return
        if not ok:
            self._disable("校验不符：%s，停止灯光" % (why,))
            return
        try:
            self._handle = device.open_exclusive(info)
        except Exception as exc:
            self._disable("独占打开失败：%s，停止灯光" % (exc,))
            return
        self._state = "open"
        self._log_once("connected", "键盘已连接（wired）")

    def _notify_wired_needed(self) -> None:
        """只提醒一次：2.4G 接收器不是配置通道，请换有线。"""
        if "wired_needed" in self._logged:
            return
        self._logged.add("wired_needed")
        self._log.info("只检测到 2.4G 接收器：请换有线连接")
        if self._on_wired_needed is not None:
            try:
                self._on_wired_needed()
            except Exception:
                self._log.debug("on_wired_needed 回调失败", exc_info=True)

    def _send_frame(self, now: float) -> None:
        with self._lock:
            lights = dict(self._lights)  # 加锁取副本：apply 在主循环线程写
        breath = frame.breath_brightness(int(now * 1000))
        payload = frame.build_frame(frame.render_slots(lights, breath))
        try:
            device.write_feature(self._handle, payload)
        except Exception as exc:
            self._handle_failure(exc)

    def _handle_failure(self, exc: Exception) -> None:
        """发帧失败：关句柄，再判通道是否还在。

        通道消失（多半是拔线）→ suspended，插回自动恢复；
        通道仍在（驱动/硬件故障）→ disabled，绝不重试（红线）。
        """
        self._log.debug("发帧失败：%s", exc)
        device.close(self._handle)
        self._handle = None
        try:
            info = device.find_config_channel()
        except Exception:
            info = None  # 判不出来按「不在」处理：等待恢复比永久停灯更接近现场
            self._log.debug("发帧失败后复查通道出错", exc_info=True)
        if info is None:
            self._state = "suspended"
            self._log_once("suspended", "键盘离线，等待恢复")
        else:
            self._disable("发帧失败，停止灯光功能")

    # ---- 小工具 ----

    def _disable(self, message: str) -> None:
        """一次失败即停：进入 disabled 后 _step 直接返回，永不重试。"""
        self._state = "disabled"
        self._log_once("disabled", message, logging.WARNING)

    def _log_once(self, key: str, message: str, level: int = logging.INFO) -> None:
        """同一 key 的消息只记一次（防止每轮检查刷屏）。"""
        if key in self._logged:
            return
        self._logged.add(key)
        self._log.log(level, message)
