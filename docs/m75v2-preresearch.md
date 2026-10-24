# 新盟 M75V2 预研（2026-10-24）

目标：让键盘状态灯守护支持第二把键盘 新盟 M75V2（三模 75% 81 键，7000mAh）。
状态：**预研完成，协议待实机确认**。本文所有协议细节均为候选假设，未在设备上验证。

## 1. 硬件身份（来自官方驱动 config.xml）

| 模式 | VID | PID | 接口 | 说明 |
|---|---|---|---|---|
| USB 有线 | 0C45 | 8006 | MI_00 | 桌面驱动主通道 |
| 2.4G 接收器 | 05AC | 024F | MI_03 | dongle 模式 |
| 蓝牙 | - | - | - | 官方明确：蓝牙模式不支持设置 |

- 桌面驱动：M75 keyboard Driver（MFC 原生，HidD_SetFeature/HidD_GetFeature，SetupDi 枚举）
- 网页驱动：Xinmeng.driveall.cn（WebHID，sendReport，Sonix 平台）
- 网页驱动机型表里 M75 的条目同样是 `3141(0x0C45)-32774(0x8006)` 与 `3141-32775(0x8007)`
  → 两套驱动指向**同一固件**，协议大概率同源。

## 2. 网页驱动协议（已从 JS 还原，可作为候选）

传输：WebHID `sendReport(reportId=0, packet)`；应答 inputreport 以 `0x55` 开头；每包等应答，超时 500ms，重试 3 次。

组帧（32 字节包）：

| 偏移 | 含义 |
|---|---|
| 0 | 0xAA 帧头 |
| 1 | 命令 |
| 2 | 本包数据长度 |
| 3-4 | 地址（低字节在前） |
| 5-7 | 可选参数；[6] 为末包标志 |
| 8.. | 数据 |

分块：每包数据 = reportCount - 8（默认 24 字节），地址按块递增。

命令表（节选）：
- 1 COMMUNICATION_START / 2 END
- 35 SET_LED_EFFECT（16 字节：模式,R,G,B,255,副色RGB,colorMode,亮度,速度,方向,type,0,0xAA,0x55）
- 36 SET_CUSTOM_LED_DATA（512 字节 = 128 × [LED序号,R,G,B]）← 逐键颜色
- 50 GET_LED_DATA / 51 GET_ALL_LIGHTS_RGB / 54 CLEAR_LED_DATA / 66 SET_LED_DATA（点阵）

## 3. 灯位映射（来自桌面驱动 layouts/rgb-keyboard.xml）

- 81 键，col_count=19 / row_count=6
- 数字键灯位：`1`=20, `2`=21, `3`=22, `4`=23, `5`=24, `6`=25, `7`=26, `8`=27, `9`=28, `0`=29
- Esc=1，F1..F12=2..13，Delete=119
- 驱动内命令间隔 `cmd_delaytime=35`(ms)，亮度上限 5，速度上限 3

## 4. 结论与假设

1. 桌面驱动与网页驱动认同一组有线 PID（0C45:8006/8007），协议很可能一致。
2. 桌面驱动走**特性报告**（HidD_SetFeature），网页驱动走**输出报告**（sendReport）。固件可能两者都收；实机分别试探。
3. 灯位映射 20..29 可直接用于数字键 1..0。
4. 2.4G 模式也可用（dongle VID 05AC:024F），但按既有红线优先有线。

## 5. 未确认项（必须实机验证）

- M75V2 有线插入后的实际 VID/PID（是否仍是 0C45:8006）。
- 厂商集合的 usage page 与 report 长度（feature/output/input）。
- 特性报告 vs 输出报告，哪种能被固件接受。
- 0xAA 帧格式是否适用于 M75V2（若不是，需 USBPcap 抓桌面驱动报文）。
- 数字键灯位是否仍为 20..29（81 键布局应相同，需实测）。

## 6. 实机探测计划（只读优先）

1. 枚举 0C45:8006，打印所有集合（usage page、report 长度）。
2. 只读：HidD_GetFeature 取设备信息类命令，看是否回 0x55。
3. 单帧试探：SET_LED_EFFECT（静态恒亮）→ 观察灯是否变。
4. 通过后再做逐键（cmd 36）验证数字键 20..29。
5. 失败即停，不重试、不改其他对象。

## 7. 实现方案（待协议确认后）

- 抽出机型档案：`profile = (match: vid/pid/usage, protocol: frame builder, keymap: key->light_index)`
- Hi86 档案保持现状；M75V2 新增一份，复用现有状态机/槽位/通知/守护逻辑。