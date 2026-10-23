"""通知口：把事件发成 Windows 系统通知（winotify），缺库时降级为日志。

v1 口径：不点击跳转、不做焦点抑制、系统默认提示音。
winotify 惰性导入：未安装只 warning 一次并降级为日志；发送异常一律吞掉，绝不拖垮主流程。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

# kind → 标题前缀词；cable 单独处理
_PREFIX = {
    "permission": "需授权",
    "question": "提问",
    "completed": "完成",
    "failed": "失败",
    "interrupted": "中断",
}
_CABLE_TITLE = "键盘需要有线连接"
_APP_ID = "OpenCode 状态灯"
_SHORT_ID_LEN = 12
_MISSING_WINOTIFY = "未安装 winotify，通知降级为日志"


@dataclass(frozen=True)
class NotifyRequest:
    """一条通知请求；slot 为 None 表示无编号，project 为目录文件夹名。"""

    kind: str
    session_id: str | None = None
    slot: str | None = None
    title: str | None = None
    project: str | None = None
    detail: str | None = None


def format_notify(req: NotifyRequest) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    if req.kind == "cable":
        head = _CABLE_TITLE
    else:
        prefix = _PREFIX.get(req.kind, req.kind)
        head = f"{prefix} · #{req.slot}" if req.slot else prefix

    if req.project and req.title:
        body = f"{req.project} — {req.title}"
    elif req.project or req.title:
        body = req.project or req.title or ""
    elif req.session_id:
        body = req.session_id[:_SHORT_ID_LEN]
    else:
        body = ""

    if req.kind == "permission" and req.detail:
        body = f"{body}\n{req.detail}"
    return head, body


class DryRunNotifier:
    """干跑适配器：通知只写日志，不弹窗。"""

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def send(self, req: NotifyRequest) -> None:
        title, body = format_notify(req)
        self._log.info("[通知] %s ｜ %s", title, body)


class WinotifyNotifier:
    """winotify 适配器：惰性导入；缺库后本实例一律退回日志。"""

    def __init__(self, log: logging.Logger) -> None:
        self._log = log
        self._degraded = False

    def send(self, req: NotifyRequest) -> None:
        title, body = format_notify(req)
        if self._degraded:
            self._log.info("[通知] %s ｜ %s", title, body)
            return
        try:
            import winotify
        except ImportError:
            self._degraded = True
            self._log.warning(_MISSING_WINOTIFY)
            self._log.info("[通知] %s ｜ %s", title, body)
            return
        try:
            toast = winotify.Notification(
                app_id=_APP_ID, title=title, msg=body, duration="short"
            )
            toast.set_audio(winotify.audio.Default)
            toast.show()
        except Exception:
            # 通知是锦上添花，失败只记 debug，绝不拖垮主流程
            self._log.debug("winotify 通知发送失败", exc_info=True)


def create_notifier(
    dry_run: bool, log: logging.Logger
) -> DryRunNotifier | WinotifyNotifier:
    """dry_run → 干跑适配器；否则 winotify 适配器。"""
    return DryRunNotifier(log) if dry_run else WinotifyNotifier(log)