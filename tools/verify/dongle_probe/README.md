# 2.4G 接收器技术验证

一次性、独立于守护的技术验证。不改 src/、不进 git。

## 结论（2026-10-26 首测 + 2026-10-27 复测）

**2.4G 无线模式可以控制 LED，也能当常亮状态牌用，代价是周期性闪一下。**

- 单个数字键单独亮绿：键 1（LED 7）、键 5（LED 31）、键 9（LED 55）。
- 全部 126 个 LED 一起亮绿。
- 标准权限即可，不需要管理员。
- 每次发送都逐包收到接收器 ACK，不是「WriteFile 成功」的假象。
- **单帧能保持约 60 秒**，之后键盘空闲关灯，1–2 分钟后链路断开。
- **每 30 秒补一帧可以一直亮着**：实测连跑 6 分钟，链路每 15 秒查一次全程正常。
- **代价：每次补帧灯会黑约 1 秒**。30 秒一次，无法避免。

## 必须记住的五个坑

1. **`WriteFile` 返回 True 不代表接收器收到。** 它只表示 20 字节写进了 HID 驱动。
   必须逐包读 ACK：回包 `byte0` 等于命令号，且 `byte1 >> 7 == 0`。
2. **读回包必须带硬超时。** 用 `FILE_FLAG_OVERLAPPED` + `WaitForSingleObject`，
   否则 `ReadFile` 在没有数据时会永久阻塞，整个 27 包流程卡死。
3. **键盘空闲约 60 秒会自己关灯，然后休眠。** 灯灭不是「回落到内置灯效」，
   是关灯；链路可能还活几十秒，`GetDongleStatus` 随后变成**无回包**。
   要让它一直亮就必须补帧，30 秒一次实测有效。
4. **按任意键会唤醒键盘，并把上次那一帧颜色恢复出来。**
   所以观测时按回车等于「把灯点亮」，会让人误判成「灯没灭过」。
   10-26 那轮「保持 ≥7 分钟」的结论就是这么错的。
5. **不要照上游注释做 1.5–4 秒高频重发。** 那是 R87 Pro 的注释，对 Hi86 不成立。
   高频重发会让打字非常滞涩、按键重复——**卡顿是自己造成的**。

## 协议来源

RK R87 Pro 的 2.4G 接收器协议（同为 `3554:FA09` "CX 2.4G Wireless Receiver"）。
见 `https://github.com/myxk1792/openrgb-rk-r87pro` 的
`docs/rf-dongle-protocol.md` 和 `src/R87ProDongle.{h,cpp}`。

**注意**：该仓库的文档说颜色缓冲是「扁平 RGB 三元组」，这是错的。
`src/R87ProDongle.cpp` 的实现是三平面：

```
buffer[led]                       = R
buffer[126 + led]                 = G
buffer[252 + led]                 = B
```

按文档写的扁平布局去发，灯会亮在完全无关的键上。

## 前提

- 接收器插好，键盘开关在 2.4G，键盘已和接收器配对。
- 不要开官方程序 OemDrv.exe（会独占设备）。
- 键盘空闲约 1 分钟关灯、1–2 分钟断链。要长时间展示必须补帧。

## 用法

```
cd "D:\Program Files (x86)\OpenCodeKeyboardLight\tools\verify\dongle_probe"

python menu.py                                # 菜单：1=单帧保持  2=补帧保活

python dongle_led.py                          # 键 1/5/9 绿色，10 秒后熄灭
python dongle_led.py --keys 1,2,3 --color 255,0,0 --duration 20
python dongle_led.py --all --color 0,200,0 --duration 10
python dongle_led.py --off                    # 只发全灭帧

python hold_test.py                           # 不补帧，看灯多久灭
python keepalive_test.py --interval 30        # 每 30 秒补一帧，看能否常亮
python interference_test.py --interval 4      # 高频重发对打字的影响
```

设备路径不再写死：`open_dongle()` 会现场枚举 `3554:FA09` 的接口，
逐个握手验证，换 USB 口也不会失效。

所有测试脚本都会把完整输出落盘：`hold_test_*.txt` / `keepalive_*.txt`。

## 干扰数据（2026-10-26 实测）

| 重发间隔 | 链路占用率 | 打字手感 |
|---|---|---|
| 1.5s | ~17% | 非常滞涩，明显延迟，Shift 切输入法无响应，按键重复 |
| 4s   | ~6%  | 仍滞涩，有延迟，按键重复现象仍在 |
| 30s  | ~0.8% | 演示场景没人打字，未测手感；链路 6 分钟无异常 |

结论：**高频保活不可用于输入场景**。30 秒级是另一个量级，可以接受，
但「用户正在打字」时是否补帧要先实测。

## 判定

1. 握手：`GetDongleStatus` 回包 byte 4 非 0（键盘已连），
   `GetPassword` 回包 `[8..9]` = `0x01 0x8D`（Hi86）。
2. 写入：27 包全部收到 ACK，键盘上出现预期画面。
3. 存活：`GetDongleStatus` 有回包且 byte4 非 0。无回包 = 已休眠断链。

## 未验证的部分

- `GetLedColors`（命令号 66）读回颜色缓冲：接收器不回包，读不回来。
- 逐键改色的上限：只测了 126 个槽位里的数字行索引，其余索引未逐个核对。
- 补帧那 1 秒黑的成因，以及能否用更小的帧避免。
- 45 秒间隔是否也够压住休眠（30 秒有效，不补帧 60 秒必灭）。
- 2.4G 模式下插 USB 充电线是否不休眠。

## 详细记录

完整验证过程、三组保持实验、源码依据、后续接入建议见
`D:\Program Files (x86)\OpenCodeKeyboardLight\docs\2.4G-LED-验证记录.md`。