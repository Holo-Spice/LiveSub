# LiveSub

LiveSub 是一个 Windows 本地字幕工具，基于具备多语种能力的本地语音识别与翻译模型，把视频文件或系统音频转换为简体中文字幕。当前程序的语言选项、时间对齐和验证流程只开放并验证了英语、日语；其他语言尚未接入，不能视为当前版本已支持。程序在本机运行模型，输出外挂 `.srt` 字幕和同名诊断 `.jsonl` 文件，不修改原视频。

## 主要功能

- 离线视频转字幕
- 系统音频录制与分段字幕处理
- 当前开放英语、日语识别与简体中文翻译
- 自定义识别热词和翻译要求
- Windows 图形界面与命令行入口
- 把字幕烧进视频，或封装成 MKV 字幕轨道用于手机观看

## 技术流程

`FFmpeg → Silero VAD → Smart Turn / SaT 断句 → Qwen3-ASR → Forced Aligner → Hy-MT2 → SRT`

界面使用 C# / WPF，字幕管线使用 Python、PyTorch、Transformers、ONNX Runtime 和 llama.cpp。

## 当前限制

当前管线需要依次完成断句、识别、时间对齐和翻译，更适合离线视频处理。系统音频可以分段处理，但会有明显延迟，暂不作为实时翻译功能。

## 运行环境

当前版本只适配并实测了 **Windows 11 x64 + AMD Radeon RX 9070 XT**，使用 AMD HIP / ROCm GPU 推理。NVIDIA 显卡版本暂不支持。

运行前需要：

- Python 3.12 x64
- .NET 10 SDK（仅从源码编译需要；发布版自带运行时）
- Microsoft Visual C++ 2015–2022 x64 运行库
- AMD 显卡驱动
- 约 40 GB 可用磁盘空间（建议预留 50 GB）

模型和第三方运行时会安装到项目目录，占用空间较大；模型文件不包含在源码仓库中。

## 使用

从 Releases 下载 `LiveSub.Launcher.exe`，放到可写目录后双击。首次启动按界面提示安装环境、运行时和模型；安装完成后即可使用 LiveSub 图形界面。

已有源码环境可以运行：

```powershell
.\LiveSub.Launcher.exe
```

## 把字幕用到视频上

生成的只是外挂 `.srt`，播放器要单独加载它。要用到视频上、或拷到手机看，用 `tools\deliver_video.py`：

```powershell
# 硬字幕：字幕烧进画面，任何播放器（包括手机自带相册/播放器）都能看到
python tools\deliver_video.py burn outputs\offline\1.zh.srt --video testdata\1.mp4

# 软字幕：装进 MKV 作为字幕轨道，不重编码、无画质损失、几秒完成（手机用 VLC/MX Player 播放）
python tools\deliver_video.py mux  outputs\offline\1.zh.srt --video testdata\1.mp4

# 手机下载：本机开一个 HTTP 服务，手机浏览器打开提示的地址即可
python tools\deliver_video.py serve outputs\offline
```

常用参数：

- `--encoder h264_amf`：用 AMD 显卡硬件编码，比默认的 `libx264` 快数倍（画质略低，可配合 `--crf` 调整）。
- `--margin-v 130`：把新字幕抬高 130 像素。**源视频画面里已经烧死字幕时必用**（很多下载来的片子属于这种），否则两套字幕会叠在一起。
- `--style "FontName=...,FontSize=..."`：整套 libass 样式；默认字体为微软雅黑。
- 输出视频带 `+faststart`，手机上可以边下边播。

安卓手机传输三种方式，按方便程度排序：

1. **WiFi 下载**：手机与本机连同一个 WiFi，运行上面的 `serve`，用手机浏览器打开提示的 `http://192.168.x.x:8765/`，长按文件下载。
2. **USB 数据线**：手机选「传输文件 / MTP」，把输出视频拷到 `内部存储\Movies`，相册或播放器即可看到。
3. **ADB**：`adb push 视频.mp4 /sdcard/Movies/`（需要手机开启 USB 调试）。

手机播放建议装 VLC 或 MX Player：软字幕（mux 出来的 MKV）由它们选轨显示，硬字幕（burn 出来的 MP4）任何播放器都直接可见。

