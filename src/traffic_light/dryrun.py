"""dry-run 灯光口：把灯态效果写进日志（不碰真实键盘）。"""
from __future__ import annotations

import logging

from .core import Light, SetKeyLight

LIGHT_CN = {
    Light.RUNNING: "紫",
    Light.FAILED: "红",
    Light.WAITING: "黄",
    Light.IDLE: "绿",
    Light.OFF: "灭",
}


class DryRunLightPort:
    """灯口替身：只把效果渲染成日志，供实机前的全流程冒烟。"""

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def apply(self, effects: list[SetKeyLight]) -> None:
        for e in effects:
            self._log.info("键%s → %s", e.key, LIGHT_CN[e.light])