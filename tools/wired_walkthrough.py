# -*- coding: utf-8 -*-
"""真机走一遍新灯态：黑 → 绿 → 黄呼吸 → 红 → 黑。

用真实的 profiles.Hi86Profile + frame + device 路径，只是灯态由时间轴驱动，
不需要接 OpenCode 会话。用来确认「平时全黑、只有需要你的时候才亮」在实物上是什么样。

用法（有线模式 + 数据线）：
    python tools/wired_walkthrough.py
    python tools/wired_walkthrough.py --dry     # 不碰设备，只跑时间轴
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from traffic_light import device, frame, profiles   # noqa: E402
from traffic_light.core import Light                # noqa: E402

# (持续秒数, 灯表, 屏幕上要你注意什么)
STEPS = [
    (3, {},                                  "① 全黑：没有事，键盘不该有一点光"),
    (7, {"3": Light.IDLE},                   "② 键 3 亮绿：一个会话完成了"),
    (14, {"5": Light.WAITING},               "③ 键 5 亮黄并呼吸：在等你批权限"),
    (7, {"7": Light.FAILED},                 "④ 键 7 亮红：一个会话失败了"),
    (3, {},                                  "⑤ 全黑：结束了"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="不碰设备，只跑时间轴")
    args = ap.parse_args()

    profile = profiles.Hi86Profile()
    handle = None
    if not args.dry:
        if profile.detect_mode() != profiles.MODE_WIRED:
            print("没检测到有线键盘。请把开关拨到 USB 并插数据线。")
            return 1
        info = profile.find_channel()
        ok, why = profile.validate(info)
        if not ok:
            print("通道校验不过：%s" % why)
            return 1
        handle = profile.open(info)
        print("有线通道已打开。全程约 %d 秒，请看键盘。"
              % sum(s[0] for s in STEPS))
    else:
        print("（试运行，不写设备）")

    print("-" * 56)
    start = time.monotonic()
    try:
        for secs, lights, note in STEPS:
            print("%5.1fs  %s" % (time.monotonic() - start, note), flush=True)
            end = time.monotonic() + secs
            while time.monotonic() < end:
                if handle is not None:
                    now = time.monotonic() - start
                    profile.write(handle, profile.render(lights, int(now * 1000)))
                # 黄在呼吸 → 20 fps；其余静态 → 0.5 秒兜底（有线一帧只活约 2 秒）
                time.sleep(0.05 if Light.WAITING in lights.values() else 0.5)
    except KeyboardInterrupt:
        print("\n手动中断。")
    finally:
        if handle is not None:
            profile.write(handle, profile.render({}, 0))
            profile.close(handle)
            print("已全灭并关闭设备。")
    return 0


if __name__ == "__main__":
    sys.exit(main())