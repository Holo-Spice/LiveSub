# NVIDIA 安装测试流程（尚未支持）

> 本文是移植和验证清单，不代表当前版本已经支持 NVIDIA。当前一键安装器、依赖锁和运行前检查均针对 AMD RX 9070 XT；直接在 NVIDIA 电脑上运行会被前提检查拒绝。

## 需要完成的适配

1. 安装 Python 3.12 x64、VC++ x64 运行库和最新 NVIDIA 驱动。
2. 新建干净 `.venv`。按 [PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/) 选择 Windows、Pip、Python 和与驱动兼容的 CUDA wheel；不要安装 `requirements.lock` 中的 AMD/ROCm 包。
3. 从中性依赖清单安装 Transformers、ONNX Runtime、Silero VAD、wtpsplit、nagisa、httpx 等依赖，并用 `torch.cuda.is_available()` 验证 CUDA。
4. 从 [llama.cpp Releases](https://github.com/ggml-org/llama.cpp/releases) 下载同一版本的 Windows x64 CUDA 主程序包和对应 CUDA runtime 包，解压到同一目录。把 `config.toml` 的 `llama_server` 和 `llama_runtime_bin` 指向该目录。
5. 修改 `src/subtitle_cli/models.py`：GPU 检查应接受 `torch.version.cuda`，不能只接受 `torch.version.hip`。
6. 修改 `src/subtitle_cli/cli.py`：移除 RX 9070 XT 名称硬编码，改为接受受支持的 CUDA 设备并记录实际显卡名。
7. 修改 WPF 安装器：按显卡厂商选择 CUDA 或 ROCm 依赖、llama.cpp 包、空间估算和探针；不要让两套 GPU 运行时混装。

## 最小测试顺序

每一步通过后再进入下一步，避免一次跑完整套：

1. `torch.cuda.is_available()` 为 `True`，FP16 矩阵运算成功。
2. `llama-server --list-devices` 能看到 NVIDIA 显卡，Hy-MT2 7B 能翻译一条日语短句。
3. Qwen3-ASR 和 Forced Aligner 能加载到 `cuda:0`，一段 10 秒日语视频能生成时间轴。
4. 跑一段 1–2 分钟离线视频，检查 SRT、JSONL、显存峰值和退出行为。
5. 最后再测试实时回环、长视频、取消任务和安装器全新安装。

完成这些适配与测试前，README 和 Release 页面应继续标注“NVIDIA 未支持”，不能只写“未测试”。
