# -*- coding: utf-8 -*-
"""只读探测：列出候选键盘的 HID 集合。绝不写入、绝不独占打开。

用法：
    python tools/probe_keyboard.py            # 只看已登记的候选机型（Hi86 / M75V2）
    python tools/probe_keyboard.py --all      # 看所有厂商自定义集合（usage page >= 0xFF00）
    python tools/probe_keyboard.py --json     # 机器可读输出

安全：只调用 device.list_devices()（0 权限打开、只读、探测完立即关闭）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from traffic_light import device, profiles  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_keyboard",
        description="只读探测键盘 HID 集合（不写入、不独占打开）")
    parser.add_argument("--all", action="store_true",
                        help="列出全部厂商自定义集合（usage page >= 0xFF00）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    try:
        devices = device.list_devices()
    except Exception as exc:
        print("枚举失败：%s" % exc, file=sys.stderr)
        return 2

    rows = profiles.probe_rows(devices, include_all=args.all)
    if args.json:
        print(json.dumps([{
            "vid": "0x%04X" % d.vid, "pid": "0x%04X" % d.pid,
            "usage_page": "0x%04X" % d.usage_page, "usage": "0x%04X" % d.usage,
            "feature": d.feature_length, "input": d.input_length,
            "output": d.output_length, "path": d.path,
        } for d in rows], ensure_ascii=False, indent=2))
    else:
        print("VID:PID       usage(page:usage)   feat input output  路径")
        for d in rows:
            print("0x%04X:0x%04X  0x%04X:0x%04X      %4d %5d %5d   %s"
                  % (d.vid, d.pid, d.usage_page, d.usage,
                     d.feature_length, d.input_length, d.output_length, d.path))
        if not rows:
            print("（没有匹配的集合：键盘未连接，或不在候选列表；可加 --all 看全部厂商集合）")

    print()
    for name in sorted(profiles.PROFILES):
        p = profiles.PROFILES[name]
        cap = p.capability
        try:
            mode = p.detect_mode(devices)
        except Exception as exc:
            mode = "探测失败：%s" % exc
        print("档案 %-6s 模式=%-5s 已验证=%s%s"
              % (cap.name, mode, "是" if cap.verified else "否",
                 "" if cap.verified else "  （%s）" % cap.note))
    return 0


if __name__ == "__main__":
    sys.exit(main())