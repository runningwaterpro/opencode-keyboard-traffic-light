# opencode-keyboard-traffic-light

OpenCode 键盘状态灯守护：把 OpenCode V2 会话状态（执行中=紫、失败=红、等待=黄呼吸、空闲=绿）
映射到 Hi86 键盘数字键 1–0，并发送系统通知。

## 安装 / 启动

- 目标：登录后自动常驻。安装自启（无管理员权限、可随时撤销）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
```

- 手动运行（前台，便于观察）：

```powershell
python tools\run_guard.py            # 真实模式：驱动键盘 + 发通知
python tools\run_guard.py --dry-run  # 干跑：只写日志，不碰键盘、不发通知
python tools\run_guard.py -v         # debug 日志
```

（开发时等价写法：`$env:PYTHONPATH = "src"; python -m traffic_light [--dry-run] [-v]`）

## 停止 / 卸载

- 停止：任务管理器结束 `pythonw.exe`，或 `taskkill /IM pythonw.exe /F`。
- 撤销自启：

```powershell
powershell -ExecutionPolicy Bypass -File tools\uninstall_autostart.ps1
```

- 结束守护后，键盘约 1 秒自动回到自身灯效（固件重绘），无残留。

## 日志

- `%LOCALAPPDATA%\opencode-keyboard-traffic-light\guard.log`（自动轮转，1 MB × 3 份）。

## 与旧 Notifier 交接

- 通知触发、文案要素与旧 Notifier 对齐；确认守护的通知工作正常后，**关掉旧 Notifier 插件即可退役**。
- 已知差距（v1 接受）：点击跳转、焦点抑制不做。

## 测试（离线）

```powershell
python -m pytest -q
```

## 结构

- `src/traffic_light/core.py`——纯状态机（事件 → 灯态/槽位/通知效果）
- `src/traffic_light/frame.py`——组帧纯函数（灯态 → 520 字节）
- `src/traffic_light/device.py`——唯一碰键盘 HID 的设备层
- `src/traffic_light/{sse,discovery,rest}.py`——事件流/服务发现/REST 重建
- `src/traffic_light/{keyboard,notify,dryrun,daemon,__main__,runtime}.py`——端口与主循环
- `tools/fake_opencode.py`——冒烟用假 OpenCode 服务；`tools/run_guard.py`——启动器
- 决策与术语：`CONTEXT.md`、`docs/adr/`
