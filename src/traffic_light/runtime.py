"""常驻化基础设施：单实例互斥（Windows 命名互斥体）+ 日志轮转。"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "opencode-keyboard-traffic-light"
LOG_FILE_NAME = "guard.log"
LOG_MAX_BYTES = 1_048_576
LOG_BACKUP_COUNT = 3

ERROR_ALREADY_EXISTS = 183

# 句柄存模块级变量：进程活着 = 句柄活着 = 互斥体一直有效。
_mutex_handle: int | None = None
_kernel32: object | None = None


def _default_log_dir() -> Path:
    """%LOCALAPPDATA%\\opencode-keyboard-traffic-light；变量缺失时退回系统临时目录。"""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path(tempfile.gettempdir())
    return root / APP_NAME


DEFAULT_LOG_DIR: Path = _default_log_dir()


def _load_kernel32():
    """按需加载 kernel32 并修正签名（64 位下句柄必须 c_void_p，默认 c_int 会截断）。"""
    global _kernel32
    if _kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32 = kernel32
    return _kernel32


def acquire_single_instance(name: str = APP_NAME) -> bool:
    """拿命名互斥体；False = 已有实例持有同名互斥体。

    名字加 Local\\ 前缀：只在同一登录会话内互斥，普通用户无需特权。
    """
    global _mutex_handle
    kernel32 = _load_kernel32()
    handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def setup_logging(verbose: bool = False, log_dir: Path | None = None) -> None:
    """重建根 logger：轮转文件 + stderr 控制台。可重复调用。"""
    target_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        target_dir / LOG_FILE_NAME,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    root.setLevel(logging.DEBUG if verbose else logging.INFO)
