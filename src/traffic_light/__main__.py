"""入口：python -m traffic_light"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .daemon import run
from .discovery import DEFAULT_SERVICE_JSON


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="traffic_light",
        description="OpenCode 键盘状态灯守护（dry-run：只写日志，不碰键盘、不发通知）",
    )
    parser.add_argument(
        "--service-json",
        type=Path,
        default=None,
        help="service.json 路径（默认读用户目录 .local/state/opencode/service.json；环境变量 KTL_SERVICE_JSON 同效）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出 debug 日志")
    args = parser.parse_args(argv)

    service_json = args.service_json
    if service_json is None:
        env = os.environ.get("KTL_SERVICE_JSON")
        if env:
            service_json = Path(env)
    if service_json is None:
        service_json = DEFAULT_SERVICE_JSON

    return run(service_json=service_json, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())