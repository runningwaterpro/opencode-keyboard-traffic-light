"""常驻化测试：全部离线。互斥用子进程验证，日志写临时目录。"""
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from traffic_light.runtime import (
    DEFAULT_LOG_DIR,
    LOG_FILE_NAME,
    acquire_single_instance,
    setup_logging,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_GUARD = ROOT / "tools" / "run_guard.py"

# 子进程代码：拿到互斥体 → 写就绪标记 → 睡住不放。sys.argv 依次是 src、互斥体名、标记文件。
HOLDER_CODE = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from traffic_light.runtime import acquire_single_instance\n"
    "if not acquire_single_instance(sys.argv[2]):\n"
    "    raise SystemExit('child: mutex already held')\n"
    "Path(sys.argv[3]).write_text('held', encoding='utf-8')\n"
    "time.sleep(60)\n"
)


def wait_for_file(path: Path, timeout: float = 15.0) -> None:
    """等文件出现；超时就报错，绝不无限等。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"等 {path.name} 出现超时（{timeout}s）")


@pytest.fixture
def keep_root_logger():
    """用完恢复根 logger，避免影响同进程里的其他测试。"""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if handler not in saved_handlers:
            handler.close()
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def test_two_different_names_in_same_process_are_both_true():
    base = f"opencode-ktl-test-{os.getpid()}"
    assert acquire_single_instance(base + "-a") is True
    assert acquire_single_instance(base + "-b") is True


def test_second_process_is_rejected(tmp_path):
    name = f"opencode-ktl-test-mutex-{os.getpid()}"
    flag = tmp_path / "held.flag"
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER_CODE, str(ROOT / "src"), name, str(flag)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        try:
            wait_for_file(flag)
        except AssertionError:
            proc.kill()
            _, err = proc.communicate(timeout=10)
            raise AssertionError(f"子进程没能持有互斥体：{err}") from None
        # 子进程持有同名互斥体 → 父进程必须被拒
        assert acquire_single_instance(name) is False
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # 子进程已死 → 互斥体释放 → 这次必须拿到
    assert acquire_single_instance(name) is True

def test_default_log_dir_named_after_app():
    assert "opencode-keyboard-traffic-light" in str(DEFAULT_LOG_DIR)


def test_setup_logging_writes_file(tmp_path, keep_root_logger):
    log_dir = tmp_path / "logs"          # 故意不建：setup_logging 应自建
    setup_logging(log_dir=log_dir)
    assert logging.getLogger().level == logging.INFO
    logging.getLogger("traffic_light.test").info("轮转日志测试一行")
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_file = log_dir / LOG_FILE_NAME
    assert log_file.exists()
    text = log_file.read_text(encoding="utf-8")
    assert "轮转日志测试一行" in text
    assert "INFO" in text


def test_setup_logging_repeatable(tmp_path, keep_root_logger):
    setup_logging(log_dir=tmp_path)
    setup_logging(verbose=True, log_dir=tmp_path)
    assert logging.getLogger().level == logging.DEBUG
    assert len(logging.getLogger().handlers) == 2   # 1 文件 + 1 控制台


def test_run_guard_compiles(tmp_path):
    env = {**os.environ, "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache")}
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(RUN_GUARD)],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
