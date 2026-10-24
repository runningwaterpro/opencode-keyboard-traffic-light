# opencode-keyboard-traffic-light

OpenCode 键盘状态灯守护：把 OpenCode V2 会话状态（执行中=紫、失败=红、等待=黄呼吸、空闲=绿）
映射到键盘数字键 1–0 和系统托盘图标，并发送系统通知。托盘常驻；键盘可有可无、可多把。

支持机型：

| 机型 | 状态 | 说明 |
|---|---|---|
| Hi86（258A:010C） | 已验证 | 有线连接；auto 模式自动识别 |
| 新盟 M75V2（0C45:8006/8007） | 未验证 | 协议仅静态分析，暂不写入；先做只读探测 |

- 键盘自动识别：默认 `--profile auto`，插拔即生效；同型号多把都驱动，未验证机型只记录不驱动。
- 指定档案：`python tools\run_guard.py --profile m75v2`（未验证档案只等待，不碰设备）。
- 只读探测（不打开写入、不发报文）：

```powershell
python tools\probe_keyboard.py          # 只看候选机型（Hi86 / M75V2）
python tools\probe_keyboard.py --all    # 看全部厂商自定义集合
python tools\probe_keyboard.py --json   # 机器可读输出
```

## 安装 / 启动

- 目标：登录后自动常驻。安装自启（无管理员权限、可随时撤销）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
```

- 手动运行（前台，便于观察）：

```powershell
python tools\run_guard.py            # 真实模式：托盘 + 键盘 + 通知
python tools\run_guard.py --dry-run  # 干跑：托盘标灰，只写日志，不碰键盘、不发通知
python tools\run_guard.py --no-tray  # 不创建托盘图标（排查用）
python tools\run_guard.py -v         # debug 日志
```

（开发时等价写法：`$env:PYTHONPATH = "src"; python -m traffic_light [--dry-run] [-v]`）

## 停止 / 卸载

- 停止：右键托盘图标 → 退出；或任务管理器结束 `pythonw.exe`。
- 撤销自启：

```powershell
powershell -ExecutionPolicy Bypass -File tools\uninstall_autostart.ps1
```

- 结束守护后，键盘约 1 秒自动回到自身灯效（固件重绘），无残留。

## 日志

- `%LOCALAPPDATA%\opencode-keyboard-traffic-light\guard.log`（自动轮转，1 MB × 3 份）。

## 托盘与通知

- 托盘图标：一个，颜色 = 全局最高优先级（黄 > 红 > 紫 > 绿）；未连接或无会话时灰色。
- 右键菜单：退出 / 打开日志 / 打开 OpenCode。
- 通知：托盘气泡（无需 winotify）；气泡不可用时退回日志。

## 与旧 Notifier 交接

- 通知触发、文案要素与旧 Notifier 对齐；确认守护的通知工作正常后，**关掉旧 Notifier 插件即可退役**。
- 已知差距（v1 接受）：点击跳转、焦点抑制不做。

## 测试（离线）

```powershell
python -m pytest -q
```

## 结构

- `src/traffic_light/core.py`——纯状态机（事件 → 灯态/槽位/通知效果）
- `src/traffic_light/frame.py`——Hi86 组帧纯函数（灯态 → 520 字节）
- `src/traffic_light/device.py`——唯一碰键盘 HID 的设备层
- `src/traffic_light/profiles.py`——机型档案：能力声明 + 协议适配；未验证档案拒绝写入
- `src/traffic_light/{sse,discovery,rest}.py`——事件流/服务发现/REST 重建
- `src/traffic_light/keyboard.py`——单把键盘的灯光口（状态机 + 发帧）
- `src/traffic_light/keyboard_hub.py`——多键盘调度：热插拔、故障隔离、退避重试
- `src/traffic_light/tray.py`——托盘图标（纯逻辑 + ctypes Win32 壳）
- `src/traffic_light/{notify,dryrun,daemon,__main__,runtime}.py`——通知/干跑/主循环/入口/常驻设施
- `tools/fake_opencode.py`——冒烟用假 OpenCode 服务；`tools/run_guard.py`——启动器
- `tools/probe_keyboard.py`——只读探测键盘 HID 集合（排查新机型用）
- 决策与术语：`CONTEXT.md`、`docs/adr/`
