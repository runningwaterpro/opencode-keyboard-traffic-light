# -*- coding: utf-8 -*-
"""有线持久性测试：只写一帧，之后完全不重发，看灯能保持多久。

要回答的问题：
  keyboard.py 现在是「内容不变就每秒兜底重发一次」。这个 1 Hz 有必要吗？
  如果一帧能自己保持几分钟，就能把 STATIC_REFRESH_S 放大，甚至只在变化时发。

做法：
  写一帧（键 1 红）→ 之后一次都不写 → 每 15 秒打印已过时间
  到 5 分钟或你按 Ctrl+C 结束，期间观察键盘什么时候灭。

用法（先切到有线并插线）：
    python tools/wired_hold_test.py
    python tools/wired_hold_test.py --seconds 600
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from traffic_light import device, frame   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--tick", type=int, default=15)
    args = ap.parse_args()

    mode = device.detect_mode()
    print("detect_mode = %s" % mode)
    if mode != "wired":
        print()
        print("没找到有线配置通道（VID 0x258A / 0x010C）。")
        print("请把键盘开关拨到有线（USB）位置，并插上数据线，再跑一次。")
        print("注意：只插线、开关仍在 2.4G 时只会充电，不会出现这个通道。")
        return 1

    info = device.find_config_channel()
    ok, why = device.validate_channel(info)
    if not ok:
        print("通道校验不过：%s" % why)
        return 1

    handle = device.open_exclusive(info)
    payload = frame.build_frame({frame.NUMBER_ROW_LED["1"]: (255, 0, 0)})
    device.write_feature(handle, payload)
    print("已写入 1 帧（键 1 红），之后不再重发。")
    print("观察：键 1 的红灯能保持多久？灭了回窗口按 Ctrl+C。")
    print("-" * 50)

    start = time.monotonic()
    nxt = args.tick
    try:
        while time.monotonic() - start < args.seconds:
            elapsed = time.monotonic() - start
            if elapsed >= nxt:
                print("  已过 %3d 秒：还亮着吗？" % int(elapsed), flush=True)
                nxt += args.tick
            time.sleep(0.2)
        print("到 %d 秒仍未手动结束。" % args.seconds)
    except KeyboardInterrupt:
        print("\n手动结束于 %d 秒。" % int(time.monotonic() - start))
    finally:
        try:
            device.write_feature(handle, frame.build_frame({}))   # 全灭
        except Exception:
            pass
        device.close(handle)
        print("已全灭并关闭设备。")
    return 0


if __name__ == "__main__":
    sys.exit(main())