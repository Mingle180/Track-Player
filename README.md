# Virtual Runner

在 **Windows** 上让 **iPhone 真机**沿指定 GPX/KML 轨迹移动定位，用于测试自研 App 的轨迹渲染/记录效果。**无需 Mac、无需越狱、无需修改 App 代码。**

原理：[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) 通过苹果官方 DVT `LocationSimulation` 调试接口向设备注入 GPS 坐标（全设备生效），配合本仓库的 `trackprep.py` 做轨迹预处理（格式转换 / 变速 / 循环 / 坐标系转换）。

## 功能

`trackprep.py`（纯 Python 标准库，无第三方依赖）把 GPX/KML 统一转成「带 1 Hz 时间戳的标准 WGS-84 GPX」，再交给官方 CLI 播放：

- 解析 GPX（`trkpt`/`rtept`）与 KML（`LineString`、`gx:Track`）
- `--speed <m/s>`：按固定速度沿轨迹插值，每秒一个点（跑步 3、骑车 5）
- `--scale <倍数>`：相对原始时间戳加速（2 = 两倍速）
- `--repeat N`：轨迹重复 N 遍
- `--from-gcj02`：GCJ-02 源（高德/腾讯导出）转回 WGS-84
- `-o out.gpx`：输出并打印点数 / 里程 / 播放时长

播放与恢复使用 pymobiledevice3 官方命令（见下）。

## 环境要求

- Windows 10/11
- **iTunes**（提供 Apple Mobile Device Support / usbmux 服务，pymobiledevice3 在 Windows 依赖它识别 USB 设备）—— 苹果官网下载
- Python 3.10+（实测 3.14）
- iPhone：开启**开发者模式**（设置 → 隐私与安全性 → 开发者模式 → 打开 → 重启确认），USB 连接并「信任此电脑」
- **iOS 17.4+**：自动建立免管理员 userspace 隧道，无需 tunneld / Xcode
  - iOS 17.0–17.3 在 Windows 需管理员 tunneld + 第三方驱动；iOS ≤16 走旧命令 `developer simulate-location` + `mounter auto-mount`（详见 pymobiledevice3 文档）

## 安装

```bash
python -m venv .venv
.venv\Scripts\activate
pip install pymobiledevice3
```

## 用法

**1. 预处理**

```bash
python trackprep.py route.kml -o route_play.gpx --speed 3 --repeat 2
python trackprep.py amap_track.gpx -o out.gpx --from-gcj02 --speed 3
```

**2. 播放**（保持 USB 连接与进程运行）

```bash
pymobiledevice3 developer dvt simulate-location play route_play.gpx
```

后台运行时**必须保持 stdin 打开**，否则进程读到 EOF 立即退出、模拟失效：

```bash
sleep 2400 | pymobiledevice3 developer dvt simulate-location play route_play.gpx
```

**3. 恢复真实定位**

```bash
pymobiledevice3 developer dvt simulate-location clear
```

多设备时给 pymobiledevice3 命令加 `--udid <UDID>`。

## 坐标系（重要）

- **注入文件始终用 WGS-84。**
- 若 App 使用**高德/腾讯 SDK**：国内（含港澳台）SDK 返回 GCJ-02，会自动给注入的 WGS-84 加偏，在高德底图上显示正确；海外原样返回 WGS-84。
- 若**轨迹源本身是 GCJ-02**（高德/腾讯导出），先用 `--from-gcj02` 转回 WGS-84，否则双重偏移约 500 m。
- 标准 GPX（WGS-84）无需转换。

## 常见问题

- **蓝点不动 / 回弹**（iOS 27 系统 Wi-Fi/基站融合定位纠偏）：开飞行模式 10 秒再关 + 彻底重启目标 App；备用：直接关 Wi-Fi。
- 模拟**只替换 GPS 坐标**，不模拟计步器 / 运动传感器；若 App 融合步频判断运动状态需注意。
- 模拟期间**全设备**定位都被替换；结束后 `clear`，异常时**重启手机必恢复**。
- 播放期间断开 USB / Ctrl+C 后模拟失效。

## 示例

- `examples/pku54_loop.gpx`：北京大学五四操场 400 m 环线（国内 GCJ-02 对齐验证用），`examples/generate_pku54.py` 可复现该几何。
- `examples/whitehouse_run.py`：白宫环线生成器（海外 WGS-84 示例），运行后生成 `whitehouse_run.gpx/.kml`。

```bash
python trackprep.py examples/pku54_loop.gpx -o pku.play.gpx --speed 5 --repeat 3
pymobiledevice3 developer dvt simulate-location play pku.play.gpx
```

## 用途声明

本工具仅供开发者在**自有设备 / 自有 App** 上做定位轨迹测试。请遵守当地法规与苹果开发者条款，勿用于作弊、欺诈或侵犯他人权益。

## 许可

- `trackprep.py` 及本仓库文档：**MIT**（见 [LICENSE](LICENSE)）。
- pymobiledevice3 为独立外部依赖，**GPL-3.0**；本仓库仅调用其 CLI、不 import 它，不构成衍生作品。

## 参考

- pymobiledevice3 CLI 用法：https://doronz88.github.io/pymobiledevice3/latest/guides/cli-recipes/
- iOS 17+ 隧道机制：https://doronz88.github.io/pymobiledevice3/latest/guides/ios17-tunnels/
- 命令参数：https://doronz88.github.io/pymobiledevice3/latest/cli/developer/