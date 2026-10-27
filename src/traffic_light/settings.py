# -*- coding: utf-8 -*-
"""用户设置：与日志同目录（%LOCALAPPDATA%\\opencode-keyboard-traffic-light）。

只存几个开关，不存密钥、不存会话。两条硬要求：
  1. **坏文件不能挡住启动**：读不到 / 不是 JSON 对象 / 字段类型不对 → 用默认值。
  2. **断电不写半个文件**：先写临时文件再 os.replace，替换是原子的。

纯文件读写，没有设备、没有时间源，测试用 tmp_path 直接覆盖。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

SETTINGS_FILE_NAME = "settings.json"


@dataclass
class Settings:
    """全部字段都要有默认值：这就是「文件不存在」时的行为。"""

    wireless_light: bool = True          # 托盘开关「无线键盘也亮灯」
    wireless_notice_shown: bool = False  # 「打字时不发帧」的一次性说明弹过没


def _as_bool(value, default: bool) -> bool:
    """只认真正的 bool；'false' / 0 / None 一律回默认值，不猜用户意思。"""
    return value if isinstance(value, bool) else default


class SettingsStore:
    def __init__(self, path: str | Path, log: logging.Logger | None = None):
        self.path = Path(path)
        self._log = log or logging.getLogger("traffic_light.settings")

    def load(self) -> Settings:
        """读设置；任何问题都退回默认值，绝不抛异常。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return Settings()
        except (OSError, ValueError) as exc:
            self._log.warning("设置文件读取失败，用默认值：%s", exc)
            return Settings()
        if not isinstance(raw, dict):
            self._log.warning("设置文件不是 JSON 对象，用默认值")
            return Settings()
        return Settings(
            wireless_light=_as_bool(raw.get("wireless_light"), True),
            wireless_notice_shown=_as_bool(raw.get("wireless_notice_shown"), False),
        )

    def save(self, settings: Settings) -> bool:
        """原子写；失败只记 warning 并返回 False（开关失效不该拖垮守护）。"""
        tmp_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent),
                                            prefix=".settings-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(asdict(settings), fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())     # 先落盘再替换：断电不留空文件
            os.replace(tmp_path, self.path)
            return True
        except OSError as exc:
            self._log.warning("设置保存失败：%s", exc)
            return False
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass