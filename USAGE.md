# 小米全屋播放使用说明

适用于 Windows 0.6 预览版。用电脑播放音乐、视频或游戏时，可以把走 Windows 默认输出的媒体声音送到同一局域网内支持 AirPlay 2 的音箱。查看支持范围和功能概览，见 [项目介绍](README.md)。

## 一、准备环境

| 项目 | 要求 |
| --- | --- |
| 电脑 | Windows 10/11，安装含 Tcl/Tk 的 [64 位 Python 3.12](https://www.python.org/downloads/windows/)。 |
| 音箱 | 先用一台小米 Sound 或 Sound Pro 试播。最多可配置 8 台；0.6 已在两台小米 Sound 和一台 Sound Pro 上进行三台联播测试。 |
| 网络 | 电脑和音箱在同一局域网且能互相访问。电脑可用 Wi-Fi 或网线。避免隔离设备的访客网络。此版本需要手动填写音箱 IPv4 地址。 |
| 虚拟声卡 | 安装 [VB-Audio VB-CABLE 标准版](https://vb-audio.com/Cable/) 并重启。确认 Windows 中有播放设备 `CABLE Input` 和录音设备 `CABLE Output`。 |
| 音频采集 | 从 [FFmpeg 的 Windows 下载入口](https://ffmpeg.org/download.html) 获取包含 [DirectShow（dshow）](https://ffmpeg.org/ffmpeg-devices.html#dshow) 的构建。 |

解压完整源码包，在含有 `room_audio.py` 的目录中打开 PowerShell，运行：

```powershell
py -3.12 --version
py -3.12 -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .\settings.json)) {
    Copy-Item -LiteralPath .\settings.example.json -Destination .\settings.json
}
```

最后一步只在没有 `settings.json` 时创建配置。已有配置时直接编辑。

## 二、填写音箱地址

从路由器的已连接设备列表或音箱的设备信息中查到各台音箱的 IPv4 地址，再编辑 `settings.json`。例如：

```json
{
  "hosts": ["192.0.2.10", "192.0.2.11"],
  "ffmpeg": "",
  "speaker_names": {
    "192.0.2.10": "客厅",
    "192.0.2.11": "书房"
  },
  "speaker_max_db": {}
}
```

- 示例 IP 专供文档使用。把 `hosts` 和其他配置中作为键的示例 IP 换成对应音箱的真实地址，地址不能重复。如地址经常变化，可在路由器中为音箱保留固定的 DHCP 地址。
- `speaker_names` 可省略，用来指定面板上的名称。名称不会用来发现设备。
- 默认把 FFmpeg 的 `ffmpeg.exe` 放在项目的 `bin` 目录，或将它所在目录加到 Windows 的 PATH，再打开新的 PowerShell。若不想改 PATH，把 `ffmpeg` 填为准确路径，例如 `"C:/Tools/ffmpeg/bin/ffmpeg.exe"`。也可填相对于 `settings.json` 的路径。
- 初次使用时将 `speaker_max_db` 留空，所有音箱都使用默认值 `-12` dB。需要调整某台的接收上限时，以它的 IP 为键填入 `-20` 到 `0` 之间的数字。提高这个值会使该音箱在相同滑杆百分比下更响；保存后退出并重新启动应用，从低音量试听。

启动前可运行 `py -3.12 room_audio.py --check`。它会检查配置和本机音频端点，不切换输出，也不测试音箱连通性。

## 三、开始播放与调整音量

1. 双击 `小米全屋播放.vbs`。如果 Windows 没有运行启动脚本，在项目目录执行 `py -3.12 room_audio.py`。
2. 点绿色的「AP2 直推」，等待界面显示「AP2 播放中」及在线台数。初次试播时让 Windows 保持低音量，在电脑上播放一段熟悉的音乐。应用起播不超过 10%，并保留原来的静音状态。
3. 用每台音箱的滑杆单独调整房间之间的平衡。之后用 Windows 音量键或任务栏音量调节整组。例如黑色、Pro、白色分别为 4%、5%、3%，Windows 总音量从 5% 调到 10% 时，三台变为 8%、10%、6%。调回 5% 后恢复原比例。

只要调大后有一台会超过 100%，整组会一起限制在仍能保留比例的范围内。Windows 静音或主音量归零时三台都会静音，恢复后保留原平衡。100% 表示已到该台的配置上限；不同型号或房间里相同的百分比不代表耳朵听到一样响。

## 四、后台播放、退出与恢复

| 操作 | 结果 |
| --- | --- |
| 点击窗口的「最小化」 | 留在任务栏，继续投放。 |
| 点击窗口右上角的「×」 | 隐藏到通知区域，继续投放。 |
| 点击通知区域的绿色图标 | 重新打开窗口。可在任务栏的「显示隐藏的图标」中找到它。 |
| 点击主窗口或托盘右键菜单里的「退出」 | 停止投放并恢复投放前的 Windows 默认媒体输出。 |

麦克风、默认通话设备和蓝牙配对保持原设置。投放期间若在 Windows 切换输出设备，投放会结束并保留新选择。退出时原输出如果已离线，软件会尝试切回电脑扬声器。

如果电脑或软件异常退出，先确认托盘没有正在运行的实例，再执行：

```powershell
py -3.12 room_audio.py --restore
```

该命令根据恢复记录归还被软件切换的输出。若原设备不可用，可在 **设置 → 系统 → 声音 → 输出** 中手动选择扬声器或其他已连接设备。

## 五、常见问题

| 现象 | 检查方法 |
| --- | --- |
| 启动脚本不打开窗口 | 在项目目录运行 `py -3.12 room_audio.py` 看报错。如果提示找不到 `py` 或 Python 3.12，检查 Python 安装；可运行 `py -3.12 -m tkinter` 确认 Tk 可用。 |
| 找不到 FFmpeg 或采集启动失败 | 使用所配置的 FFmpeg 执行 `ffmpeg -hide_banner -devices`，检查是否列出 `dshow`。FFmpeg 不在 PATH 时在命令中填写它的实际路径。检查 VB-CABLE 的两个端点是否都已启用。 |
| 音箱未全部连接 | 确认音箱在线、支持 AirPlay 2、IP 没有变化。先仅保留一台已知可用的音箱；试播成功后再逐台添加。 |
| 显示播放中，某个应用却没投放 | 检查 Windows 音量混合器，将该应用的输出设备设为「默认」。应用若仍沿用先前的音频会话，停止后重新开始播放。 |
| 显示播放中但无声或太小 | 查看 Windows 是否静音、总音量是否为 0、对应音箱是否调到 0%。如果 VB-CABLE 打开了 `Enable Windows Volume Control`，且虚拟声卡本身已经对采集信号做衰减，可关闭该选项，以免软件再次衰减。 |
| 点击了「×」，声音没回到电脑 | 点击托盘图标找回窗口，或右击托盘图标后选「退出」。 |

应用的本机日志位于 `%LOCALAPPDATA%\RoomAudio`，日志不含录音。提交问题时可说明 Windows 版本、音箱型号、软件版本、复现步骤和相关日志片段；分享前请移除局域网 IP、Wi-Fi 名称和本机文件路径。
