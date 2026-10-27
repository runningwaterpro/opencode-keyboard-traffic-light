# 验证与排查工具

这些是**一次性工具**，不属于守护程序本身。它们直接跟键盘或接收器打交道，
用来回答「这个硬件到底能做什么」。

跑之前请先**退出守护**（托盘菜单 → 退出）：设备是独占打开的，两边同时开会抢不到。

## 硬件协议探针

| 脚本 | 干什么 | 需要什么 |
|---|---|---|
| `probe_keyboard.py` | 只读列出键盘的 HID 集合，用来认新机型 | 无 |
| `dongle_probe/probe_dongle.py` | 2.4G 接收器握手 + 试点亮 | 接收器 |
| `verify_24g_light.py` | 2.4G 下走一遍灯态（黑/绿/黄/红/黑） | 接收器 + 键盘 |

## 2.4G 特性实测（回答「一帧能保持多久」「会不会吞键」）

这些脚本都有 `--dry-run`，可以先不看设备跑一遍流程。

| 脚本 | 回答什么问题 |
|---|---|
| `dongle_probe/hold_test.py` | 发一帧后不补发，灯多久灭、多久断链 |
| `dongle_probe/keepalive_test.py` | 每 N 秒补一帧，能不能一直亮着 |
| `dongle_probe/wake_test.py` | 键盘睡过去之后，光发帧能不能叫醒 |
| `dongle_probe/keydrop_test.py` | 补帧时打字，多久出现一次重复字符 |
| `dongle_probe/typing_hold_test.py` | 一直打字、零补帧，灯会不会自己灭 |
| `dongle_probe/interference_test.py` | 高频重发对打字手感的影响 |

日常只需要一个入口：

```
dongle_probe\验证-单帧保持.cmd     →  菜单（单帧保持 / 补帧 / 休眠唤醒 / 吞键 …）
dongle_probe\验证-2.4G干扰.cmd     →  直接跑干扰测试
```

每次运行的完整输出会写到 `dongle_probe/logs/`，文件名带时间戳。

## 有线侧

| 脚本 | 干什么 |
|---|---|
| `wired_walkthrough.py` | 在真机上走一遍灯态：黑 → 绿 → 黄呼吸 → 红 → 黑 |
| `wired_hold_test.py` | 有线一帧能保持多久（答案：约 2 秒，所以有线必须持续刷新） |
| `probe_channel_switch.py` | 观察插拔线时的通道切换 |
| `probe_keystate.py` | 看键盘活动检测有没有被某个键卡住 |

有线的走查可以直接双击：`验证-有线灯态走查.cmd`。

## 背景

这些脚本得出的结论已经写进 `docs/2.4G-LED-验证记录.md`，不用重跑。
只有在换键盘、换接收器、或者怀疑结论变了的时候才需要再跑。