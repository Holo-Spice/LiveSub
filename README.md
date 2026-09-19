# LiveSub

LiveSub 是一个 Windows 本地字幕工具，可以把视频文件或系统音频中的英语、日语语音识别并翻译成简体中文字幕。程序在本机运行模型，输出外挂 `.srt` 字幕和同名诊断 `.jsonl` 文件，不修改原视频。

## 主要功能

- 离线视频转字幕
- 实时系统音频字幕
- 英语、日语识别与简体中文翻译
- 自定义识别热词和翻译要求
- Windows 图形界面与命令行入口

## 技术流程

`FFmpeg → Silero VAD → Smart Turn / SaT 断句 → Qwen3-ASR → Forced Aligner → Hy-MT2 → SRT`

界面使用 C# / WPF，字幕管线使用 Python、PyTorch、Transformers、ONNX Runtime 和 llama.cpp。

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
