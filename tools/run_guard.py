"""启动器：登录自启快捷方式的入口（不依赖 PYTHONPATH）。

用法（在仓库根目录）：
    python tools\\run_guard.py [--dry-run] [-v]
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 把仓库 src 插到最前，等价于 $env:PYTHONPATH = "src"
sys.path.insert(0, str(REPO_ROOT / "src"))

from traffic_light.__main__ import main  # noqa: E402  必须在 sys.path 调整之后导入

if __name__ == "__main__":
    sys.exit(main())
