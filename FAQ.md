# Track Player 常见问题（FAQ）

## 安装与启动

**Q：提示「找不到 trackplayer / trackplayer-gui 命令」？**
A：Python 的 Scripts 目录没进 PATH。用不依赖 PATH 的兜底写法：

```bash
python -m trackplayer_gui      # 图形界面
python -m trackplayer devices  # 命令行
```

或把 Python 的 `Scripts` 目录加入 PATH 后重开终端。

**Q：启动 GUI 报「tkinter is not available」？**
A：用的是不含 tcl/tk 的极简 Python 发行版。改装 [python.org](https://www.python.org/downloads/windows/) 的 Windows 安装包（自带 tkinter）。命令行不受影响。

**Q：怎么安装？**
A：`pip install git+https://github.com/Mingle180/Track-Player.git`，装好即有 `trackplayer` 与 `trackplayer-gui` 命令（自动带上 pymobiledevice3 依赖）。

## 设备连接

**Q：未检测到 iPhone？**
A：逐项检查：
1. 装 **iTunes**（提供 Apple Mobile Device Support / usbmux 服务），确认其后台服务在运行。
2. iPhone 开启**开发者模式**：设置 → 隐私与安全性 → 开发者模式 → 打开 → 重启确认。
3. USB 连接，手机解锁，弹窗点「**信任此电脑**」。
4. 换一根数据线 / 换一个 USB 口（有些线只能充电）。

**Q：连了多台设备怎么办？**
A：`trackplayer devices` 列出所有 UDID，播放/恢复时用 `--udid <UDID>` 指定；GUI 在下拉框里选。

**Q：需要管理员权限吗？**
A：**iOS 17.4+ 不需要**——自动建立免管理员的 userspace 隧道。iOS 17.0–17.3 在 Windows 需管理员 tunneld + 第三方驱动；iOS ≤16 走旧命令（详见 pymobiledevice3 文档）。

## 定位效果

**Q：蓝点不动 / 回弹到真实位置（iOS 27）？**
A：本工具现在**持有常开 DVT 通道并周期性 re-assert 当前坐标（keep-alive）**，能显著抑制系统 Wi-Fi/基站融合定位的纠偏回弹。若仍回弹：
- 开**飞行模式 10 秒再关** + 彻底重启目标 App（清掉融合定位缓存）；
- 备用：直接**关 Wi-Fi**（减少融合定位源）。

**Q：播放一会儿就失效？**
A：模拟依赖进程持有通道。**保持 USB 连接、保持 trackplayer/GUI 运行**。断开 USB 或结束进程后模拟失效；退出时会自动 `clear` 恢复真实定位。

**Q：模拟会影响计步 / 运动状态吗？**
A：只替换 **GPS 坐标**，不模拟计步器 / 运动传感器。若 App 融合步频判断运动状态需注意。

**Q：模拟期间别的 App 定位也变了吗？**
A：是的，**全设备**定位都被替换。用 `trackplayer` / GUI 退出会自动恢复；手动方式结束后需 `clear`；异常时**重启手机必恢复**。

## 坐标系

**Q：轨迹在高德底图上偏移约 500 m？**
A：多半是**双重偏移**。注入文件始终用 **WGS-84**：
- 轨迹源本身是 **GCJ-02**（高德/腾讯导出）→ 加 `--from-gcj02`（GUI 勾「源是 GCJ-02」）转回 WGS-84。
- 标准 GPX（WGS-84）→ 无需转换。

**Q：高德 SDK 的 App 显示正确吗？**
A：国内（含港澳台）高德/腾讯 SDK 返回 GCJ-02，会自动给注入的 WGS-84 加偏，在高德底图上显示正确；海外原样返回 WGS-84。

## 速度与配置

**Q：速度怎么设？**
A：GUI 用「速度预设」下拉（步行/跑步/骑行/驾车，单位 km/h，自动换算 m/s），也可选「自定义」手动填 m/s；命令行用 `--speed <m/s>`、`--preset <名称>` 或 `--scale <倍数>`。

**Q：能改预设 / 默认值吗？**
A：能。编辑 `~/.trackplayer/config.json`（Windows 为 `C:\Users\<你>\.trackplayer\config.json`），覆盖内置默认：

```json
{
  "speed_presets": [
    {"label": "步行", "kmh": 5},
    {"label": "跑步", "kmh": 10},
    {"label": "骑行", "kmh": 20},
    {"label": "驾车", "kmh": 50}
  ],
  "default_speed_mps": 3.0,
  "keepalive_interval_s": 1.0
}
```

GUI 点「刷新设备」即热加载，无需重启。文件缺失或损坏时回退内置默认。

## 许可

**Q：为什么是 GPL-3.0？**
A：核心直接 import [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)（GPL-3.0）以持有常开定位通道，构成其衍生作品，故整体以 GPL-3.0-or-later 分发。

## 用途声明

本工具仅供开发者在**自有设备 / 自有 App** 上做定位轨迹测试。请遵守当地法规与苹果开发者条款，勿用于作弊、欺诈或侵犯他人权益。