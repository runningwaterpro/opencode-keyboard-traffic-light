# -*- coding: utf-8 -*-
"""无线灯口：把灯态渲染成 2.4G 颜色帧，并决定「什么时候发」。

协议事实来自 docs/2.4G-LED-验证记录.md（七轮真机实测），三条决定了这里的形态：

  1. 2.4G 一帧能保持约 60 秒（有线只有约 2 秒）。所以不需要有线那套 20 fps 刷新，
     补帧间隔是 35 秒量级，而不是 0.5 秒。
  2. 每发一帧必丢一次按键上报（实测 15 帧 15 次错；不发帧时 0 次）。
     所以打字期间一帧都不能发 —— 这就是「距上次按键不足 15 秒 → 不发」。
  3. 按键本身会重置键盘的空闲计（连续打字 3 分钟、零补帧，灯全程亮）。
     所以打字时不发帧不会让灯灭，只是在拿不到画面。

发送规则（与 issue #10 的实现决策一致）：

    距最后一次灯态变化 < 1 秒   → 不发（合并窗口，避免连闪）
    距上次发帧 < 35 秒          → 不发（限流）
    距上次键盘/鼠标输入 < 15 秒 → 不发（人在打字，发帧就是丢键）
    否则发一帧

15 + 35 = 50 ≤ 55：灯在最后一次按键后约 60 秒灭，留 10 秒余量。

失败语义与有线相反：2.4G 发帧失败是常态（键盘睡着了），只能关句柄、退避 5 秒重试，
绝不进入「永久停灯」。没有灯态要显示时不做重试。

本模块不碰 ctypes：设备 I/O 走注入的 transport（生产环境是 dongle.DongleTransport），
时间与「多久没输入」也由外部注入，所以 tests/test_wireless.py 全离线。
"""
from __future__ import annotations

import logging
import threading
import time

from . import dongle, frame
from .core import Light, SetKeyLight

# 人手停下来多久才算「不打了」。实测每帧必丢一次按键上报，所以宁可多等。
TYPING_IDLE_S = 15.0
# 补帧间隔。单帧能活约 60 秒，35 秒留 25 秒余量；与 TYPING_IDLE_S 之和 50 ≤ 55。
RESEND_S = 35.0
# 合并窗口：1 秒内的多次灯态变化只发一帧。
MERGE_S = 1.0
# 发帧失败后的重试间隔。失败是常态，不进 disabled。
RETRY_S = 5.0
# 状态机步长。最细的时限是 1 秒合并窗口，0.5 秒足够。
STEP_S = 0.5

BLANK = bytes(dongle.COLOR_LEN)


def default_idle_seconds() -> float:
    """生产环境的空闲时间源；延迟导入，纯逻辑测试不会碰 Win32。

    用 keyboard_idle_seconds 而不是 idle_seconds：后者把鼠标也算进去，
    用户用鼠标看网页时它永远小于阈值，60 秒的绿灯会整个错过。
    """
    from . import useractivity
    return useractivity.keyboard_idle_seconds()


def render_lights(lights: dict[str, Light]) -> bytes:
    """灯态 → 378 字节三平面缓冲。纯函数。

    复用 frame.render_slots，所以「执行中不落色」、键位、颜色全部与有线同一套语义；
    唯一区别是 breathing 固定 1.0：2.4G 做不了呼吸，黄灯常亮。
    """
    return dongle.build_color_buf(frame.render_slots(lights, 1.0))


class WirelessLightPort:
    """2.4G 灯口：与 KeyboardLightPort 同一个 apply(effects) 接口，发送时机不同。"""

    def __init__(self, log: logging.Logger, *, transport=None,
                 clock=time.monotonic, idle_seconds=None, render=None,
                 step_interval: float = STEP_S):
        self._log = log
        self._transport = transport if transport is not None else dongle.DongleTransport()
        self._clock = clock
        self._idle_seconds = (idle_seconds if idle_seconds is not None
                              else default_idle_seconds)
        self._render = render or render_lights
        self._step_interval = step_interval

        self._lock = threading.Lock()
        self._lights: dict[str, Light] = {}

        self._handle = None
        self._desired = BLANK        # 当前灯态渲染出的缓冲
        self._last_sent = BLANK      # 最近一次成功发出去的缓冲
        self._changed_at = 0.0       # 最后一次「内容真的变了」的时刻
        self._sent_at = float("-inf")
        self._next_open_at = float("-inf")

        self._failure_logged = False  # 只在连续失败的第一条记日志
        self._connected_logged = False
        self._skip_reason = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 对外接口（主循环线程） ----

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
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wireless-light",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._handle is not None:
            self._close_handle()

    @property
    def state(self) -> str:
        """open = 接收器句柄在手上；waiting = 等接收器/等重试。没有 disabled。"""
        return "open" if self._handle is not None else "waiting"

    @property
    def failure_kind(self) -> None:
        """无线不做「永久停灯」，所以永远没有失败类型（hub 据此不会拉黑）。"""
        return None

    # ---- 后台循环与状态机 ----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._step(self._clock())
            except Exception:
                # 单步出错不能带走整个线程：无线只是显示，断了也不该影响守护。
                self._log.debug("无线灯口单步失败", exc_info=True)
            self._stop.wait(self._step_interval)

    def _step(self, now: float) -> None:
        """单步状态机：_run 每 0.5 秒调一次；测试注入 now 直接驱动。"""
        with self._lock:
            lights = dict(self._lights)
        desired = self._render(lights)
        if desired != self._desired:
            if desired != self._last_sent:
                self._changed_at = now     # 合并窗口从最后一次变化算起
            self._desired = desired

        reason = self._skip_reason_at(now)
        if reason is not None:
            self._note_skip(reason)
            return
        if self._handle is None:
            if not self._open(now):
                return
            # 打开接收器要枚举 + 握手，可能耗几秒；这段时间人手又放回键盘了。
            # 发帧前再看一眼空闲时间，别在「刚停手又开打」的那一秒丢键。
            if self._idle() < TYPING_IDLE_S:
                self._note_skip("打字中")
                return
        self._send(now)

    def _skip_reason_at(self, now: float) -> str | None:
        """不发帧的理由；None = 该发。判定顺序即优先级，日志里看到的就是第一道闸。"""
        if self._desired == self._last_sent:
            if self._desired == BLANK:
                return "无灯态"                       # 全灭：没有要显示的东西
            if now - self._sent_at < RESEND_S:
                return "未到补帧周期"
        else:
            # 变化帧只等合并窗口，不吃 35 秒补帧限流：
            # 丢键只发生在「发帧时人正在打字」，而打字门控已经挡住了这种情况，
            # 变化帧再等 35 秒买不到任何安全性，只会让绿灯晚灭、黄灯晚亮。
            # 但合并窗口对「从未发过帧」的第一次变化同样生效（_sent_at 是 -inf）。
            in_merge = now - self._changed_at < MERGE_S
            recently_sent = (self._sent_at == float("-inf")
                             or now - self._sent_at < MERGE_S)
            if in_merge and recently_sent:
                return "合并窗口"
        if self._idle() < TYPING_IDLE_S:
            return "打字中"
        return None

    def _idle(self) -> float:
        """距上次用户输入的秒数。取不到就按 0 算：不确定时不发帧，少亮比丢键安全。"""
        try:
            return float(self._idle_seconds())
        except Exception:
            self._log.debug("读取系统空闲时间失败，按「在打字」处理", exc_info=True)
            return 0.0

    def _open(self, now: float) -> bool:
        """按退避重试打开接收器；成功返回 True。

        失败很常见（接收器没插、键盘睡着、官方软件占着），一律退避 5 秒再来，
        不进入任何「永久停灯」状态。
        """
        if now < self._next_open_at:
            return False
        try:
            handle = self._transport.open()
        except Exception as exc:
            self._next_open_at = now + RETRY_S
            self._note_open_failure(exc)
            return False
        if self._stop.is_set():          # 打开期间被叫停：别把句柄留在这儿
            try:
                self._transport.close(handle)
            except Exception:
                self._log.debug("关闭接收器句柄失败", exc_info=True)
            return False
        self._handle = handle
        self._next_open_at = float("-inf")
        # 新句柄必须马上补一帧：键盘睡着时画面会掉回内置灯效、甚至灭掉，
        # 光靠「内容没变」去限流会让它黑到下一次补帧周期。
        self._sent_at = float("-inf")
        if not self._connected_logged:
            self._connected_logged = True
            self._log.info("无线键盘已连接（2.4G）")
        return True

    def _note_open_failure(self, exc: Exception) -> None:
        if self._failure_logged:
            return
        self._failure_logged = True
        self._log.warning("打开 2.4G 接收器失败，%d 秒后重试：%s", int(RETRY_S), exc)

    def _send(self, now: float) -> None:
        handle = self._handle
        if handle is None:                 # 被 stop() 抢走了，这一帧不发了
            return
        try:
            self._transport.send(handle, self._desired)
        except Exception as exc:
            self._fail(now, exc)
            return
        self._last_sent = self._desired
        self._sent_at = now
        self._failure_logged = False
        self._skip_reason = None

    def _fail(self, now: float, exc: Exception) -> None:
        """发帧失败 = 键盘多半睡着了：关句柄、退避重试，保留待发内容。"""
        self._close_handle()
        self._connected_logged = False
        if self._stop.is_set():
            return                         # 关闭时的半截发送不当失败记
        self._next_open_at = now + RETRY_S
        if not self._failure_logged:
            self._failure_logged = True
            self._log.warning("无线发帧失败（键盘可能已休眠），%d 秒后重试：%s",
                              int(RETRY_S), exc)

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            self._transport.close(handle)
        except Exception:
            self._log.debug("关闭接收器句柄失败", exc_info=True)

    def _note_skip(self, reason: str) -> None:
        """同一理由只记一次 debug，避免每步刷屏。"""
        if reason == self._skip_reason:
            return
        self._skip_reason = reason
        self._log.debug("无线灯口不发帧：%s", reason)