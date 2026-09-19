# LiveSub CLI：Codex 开发执行文档

> 文档版本：v4.1（精简版）  
> 修订日期：2026-09-04  
> 项目英文名：`LiveSub`  
> 目标项目根目录：`C:\Users\29279\LiveSub`  
> 目标系统：Windows 11 x64 原生环境  
> 目标硬件基线：AMD Radeon RX 9070 XT 16GB、系统内存 32GB  
> 主要语言：英语、日语 → 简体中文

## 0. 给执行 Codex 的直接指令

本文只定义 CLI 首版的产品范围、关键技术契约和验收标准，不要求照搬示例代码或建立通用框架。

执行时遵守以下原则：

1. 先读取实际项目文件和用户改动；实际代码与本文不一致时，先说明差异。
2. 环境和模型按 `docs\02_模型与环境部署指南..md` 准备。开发任务不负责重装驱动、下载全部模型或升级后端。
3. 先打通最短离线链路，再接实时采集；不要同时铺开所有模块。
4. 采用普通函数、少量数据类和明确顺序调用。不要建立插件、注册表、依赖注入、工作流引擎或未来后端抽象。
5. 只在启动、外部进程、模型调用、字幕写入这几个真实边界处理错误；不要在每个函数重复检查和包装异常。
6. 每个阶段只运行对应测试。未实际运行的项目写“未验证”，不要用重复全量测试代替定位问题。
7. 默认部署和完整验收组合是 `Hy-MT2 7B + GPU`。1.8B 是可选轻量模型，CPU 翻译是可选运行模式。
8. 普通实现细节由 Codex 作出最小、可逆决定；只有需求冲突、关键依赖缺失或需要扩大范围时才询问用户。

## 1. 产品目标与首版范围

实现一个 Windows 命令行程序，共用同一条处理链完成：

1. **离线视频**：读取本地视频第一个音轨，生成简体中文 `.srt`。
2. **实时系统音频**：持续采集 Windows 正在播放的声音，在完整语义单元确认后追加简体中文 `.srt`。

“实时”指连续采集并在一句话完成后输出最终字幕。首版不显示逐词临时结果，也不改写已经写出的字幕。

首版必须完成英语和日语。目标语言固定为简体中文。

字幕始终是独立外挂文件。CLI 只读输入视频，不覆盖、重封装、转码或生成替代视频文件。

## 2. 固定处理链

```text
视频首音轨 / DirectShow 系统音频
              │
              v
       FFmpeg：16 kHz / mono / PCM S16LE
              │
              v
       Silero VAD：发现候选暂停
              │
              v
       Smart Turn：确认表达是否完成
              │ COMPLETE
              v
       Qwen3-ASR：生成原语言最终文本
              │
              v
       SaT：把最终文本切成完整句子
              │
              v
       ForcedAligner：生成真实时间戳
              │
              v
       Hy-MT2：整句翻译为简体中文
              │
              v
       追加 SRT；同时写简短诊断 JSONL
```

### 2.1 模型职责

| 环节 | 固定实现 | 设备 |
|---|---|---|
| 语音活动检测 | Silero VAD ONNX | CPU |
| 表达完成判断 | Smart Turn v3.2 ONNX | CPU |
| 语音识别 | Qwen3-ASR-1.7B-hf | AMD GPU |
| 句子切分 | SaT `sat-3l-sm` ONNX | CPU |
| 时间对齐 | Qwen3-ForcedAligner-0.6B-hf | AMD GPU |
| 翻译 | Hy-MT2-7B/1.8B Q4_K_M，经 llama.cpp | GPU 或 CPU |

Qwen3-ASR 只负责“音频 → 原文”，Hy-MT2 只负责“完整原句 → 简体中文”。不要让一个模型替代另一个环节。

### 2.2 AMD 与翻译进程

Qwen3-ASR 和 ForcedAligner 使用 AMD ROCm 版 PyTorch。代码使用兼容 API `cuda:0`，启动时只需确认：

```python
torch.cuda.is_available() is True
torch.version.hip is not None
```

首版使用 FP16。不加入 vLLM、FlashAttention、FP8、DirectML、Vulkan 或自动 CPU 回退。

Hy-MT2 由一个常驻 `llama-server` 子进程提供：

- GPU：`-ngl 99`；
- CPU：`--device none -ngl 0`；
- 两种模式都启用 `--jinja`，使用 GGUF 内置模板；
- 一次任务只加载一个模型，不逐句重启，也不在运行中热切换。

llama.cpp 的本地 ROCm 运行时只加入该子进程的 `PATH`，不得修改系统或父进程的 `PATH`。

## 3. 语句提交契约

### 3.1 固定流程

1. Silero VAD 发现候选暂停，但不决定句末。
2. Smart Turn 检查当前发言是否完整。
3. 未完整时继续保留并追加当前发言音频，不调用 ASR 和翻译。
4. 完整时冻结整个音频单元并调用 Qwen3-ASR。
5. SaT 对最终原文切句并返回句子字符串。
6. ForcedAligner 对完整音频和完整原文做对齐。
7. 从 SaT 句子恢复文本范围，并映射到 ForcedAligner 的有序对齐项，得到每句起止时间。
8. 只有取得真实时间戳的完整句子才交给 Hy-MT2，并写入 SRT。

Smart Turn 预处理沿用上游 v3.2：16 kHz 单声道 `float32`，只观察末尾最多 8 秒，不足时左侧补零，并由 `WhisperFeatureExtractor(chunk_length=8)` 生成输入。8 秒只是观察窗，不得截断随后交给 ASR 的完整音频。

### 3.2 SaT 与时间对齐的固定映射

SaT 的 `split()` 返回句子字符串，不直接返回字符范围。首版按以下唯一规则恢复范围：

1. 对 ASR 原文只做一次首尾空白删除，得到 `alignment_text`；不得再做 Unicode 归一化、标点替换、空白折叠或逐句 `strip()`。
2. 同一个 `alignment_text` 同时交给 SaT 和 ForcedAligner；SaT 固定使用 `split_on_input_newlines=False`。
3. 必须满足 `"".join(sentences) == alignment_text`，再按各句字符串长度累加得到半开字符范围 `[start, end)`。
4. 只对完整 `alignment_text` 使用 ForcedAligner 自带的文本处理器取得对齐单元，不逐句重新分词，也不另写英语/日语分词器。按处理器的字符保留规则从原文恢复单元位置；保留字符的拼接必须与完整文本的对齐单元一致，完整对齐项也必须逐项一致。
5. SaT 句界恰好落在完整对齐单元之间，且句界两侧都有对齐单元时，才在该处切分对齐结果。句界穿过单元或产生无对齐单元的片段时，合并相邻 SaT 句子，保留全部原文。每个输出句子的时间取其第一个对齐项的 `start_time` 和最后一个对齐项的 `end_time`。

任一步无法严格对应、句子没有有效对齐项或时间非单调时，阶段名记为 `sentence_mapping`，任务以失败结束；不得按字符比例、平均时长或上一句时间估算。测试至少覆盖英语重复词、日语无空格文本、标点以及换行。

对齐器的时间戳来自固定 token 栅格（Qwen3-ASR 处理器为每格 80 ms），单调性修补还会把越序值吸附到相邻格点，因此末尾句可能比实际 PCM 晚不到一格，短于该栅格的句子则会把整句的对齐项压在同一个时间点上。两者都只是对齐器的分辨率下限，不是识别或映射错误，处理方式唯一：

1. 越界不超过一格（≤ 0.2 秒）时不算失败：把该句的 `start`/`end` 收回到已采集音频长度内，其余时间戳一律不动，并写 `stage=sentence_mapping, status=alignment_clamped` 记录对齐末尾、越界幅度、句数与无时间句数，任务继续。
2. 越界超过一格时判定为识别文本与音频不匹配，仍按 `sentence_mapping` 失败结束，不得静默拉伸字幕。
3. 整句被压在同一时间点上（`end <= start`）的句子在该段音频里没有任何时间，直接丢弃并计入 `untimed_sentences`，任务继续；不得给它补一个时间点，也不得按字符比例或句长把它摊到音频上。丢弃后该段若无任何句子，再写 `status=alignment_dropped`。
4. 只有整段发言连一句都没有有效时间时，`alignment has no positive duration` 仍按 `sentence_mapping` 失败结束：此时连句界都无从映射。

收回只裁剪边界，丢弃只去掉没有时间的句子，两者都不得据此重新估算任何一句话的时间。

### 3.3 禁止的替代规则

不得用以下规则决定一句话结束：

- 固定静音毫秒数；
- 标点符号；
- 固定字数或固定音频时长；
- 关键词表或手写逐词合并状态机；
- 因积压而强制截断未完成表达。

文件结束或用户停止采集时，只对残余音频做一次最终完整性判断。仍未确认完整的内容写入诊断，不强制生成中文字幕。

Qwen3-ForcedAligner 单次输入硬上限为 300 秒。当前保留发言达到 300 秒仍未提交时，立即停止本次任务，记录 `utterance_too_long`、发言时长和失败阶段，保留此前已完成的外挂字幕并返回非零退出码。不得跳过后继续生成一份看似完整的字幕，也不得把 300 秒当作普通切句规则；首版不另建长音频分段协议。

## 4. 最小代码结构

```text
C:\Users\29279\LiveSub\
├─ docs\
│  ├─ 01_Codex开发执行文档..md
│  ├─ 02_模型与环境部署指南..md
│  └─ 验证记录.md
├─ README.md
├─ pyproject.toml
├─ requirements.lock
├─ config.toml
├─ src\subtitle_cli\
│  ├─ __init__.py
│  ├─ cli.py
│  ├─ audio.py
│  ├─ models.py
│  ├─ translator.py
│  ├─ subtitles.py
│  └─ pipeline.py
├─ tests\
├─ testdata\
└─ outputs\
```

这是建议上限，不要求为了目录对称创建文件。职责保持简单：

- `cli.py`：参数和退出码；
- `audio.py`：FFmpeg 视频解码、DirectShow 采集和 PCM 块；
- `models.py`：Silero、Smart Turn、SaT、ASR 和对齐器的固定加载与调用；
- `translator.py`：启动和复用一个 llama-server；
- `subtitles.py`：SRT 与诊断 JSONL；
- `pipeline.py`：按第 2 节顺序串联。

不要为这些唯一实现创建抽象基类、Provider、Factory、容器或事件总线。只有共享资源确实需要统一释放时才增加一个小型上下文对象。

### 4.1 最小配置

`config.toml` 只保存实际使用的相对路径：

```toml
[paths]
ffmpeg = "tools/ffmpeg/bin/ffmpeg.exe"
llama_server = "tools/llama.cpp-hip/llama-server.exe"
llama_runtime_bin = "tools/rocm-7.14/bin"
qwen_asr = "models/Qwen3-ASR-1.7B-hf"
qwen_aligner = "models/Qwen3-ForcedAligner-0.6B-hf"
smart_turn = "models/smart-turn-v3.2/smart-turn-v3.2-cpu.onnx"
sat = "models/sat-3l-sm"
sat_tokenizer = "models/xlm-roberta-base-tokenizer"
hymt_1_8b = "models/Hy-MT2-1.8B-Q4_K_M.gguf"
hymt_7b = "models/Hy-MT2-7B-Q4_K_M.gguf"
output_dir = "outputs"
```

路径以 `config.toml` 所在目录为基准。启动时只检查本次命令实际需要的文件；默认选择 7B 时不要求 1.8B 已安装。

## 5. 输入、实时采集与输出

### 5.1 离线视频

使用参数数组启动项目内 `ffmpeg.exe`，流式读取第一个音轨：

```powershell
ffmpeg.exe -nostdin -i "C:\Users\29279\LiveSub\testdata\example.mkv" -map 0:a:0 -vn -af "aresample=16000:async=1:first_pts=0" -ac 1 -ar 16000 -f s16le pipe:1
```

`aresample=...:first_pts=0` 负责在音轨晚于视频开始时补前置静音、在负时间戳时裁去编码延迟，并按输入时间戳处理音频间隙。这样输出 PCM 的样本 0 对应视频时间轴 `00:00:00.000`。

每个冻结发言保存其绝对 `start_sample`。某个对齐项的最终媒体时间固定为：

```text
media_time_seconds = (utterance_start_sample / 16000) + aligner_local_seconds
```

处理等待时间、队列积压和系统墙钟不得加入字幕时间。输入视频只以只读方式交给 FFmpeg，命令不得包含输出视频路径；CLI 只创建用户指定的 `.srt` 和同名 `.jsonl`。

### 5.2 实时系统音频

使用 DirectShow 设备名启动 FFmpeg。设备按其**自身格式**打开，唯一一次转换到模型的 16 kHz 单声道由我们自己的滤波器完成：

```powershell
# 先探测设备引脚格式（本机 virtual-audio-capturer 只报 ch= 2, bits=16, rate= 96000）
ffmpeg.exe -nostdin -hide_banner -list_options true -f dshow -i audio="virtual-audio-capturer"
# 再按该格式采集
ffmpeg.exe -nostdin -hide_banner -f dshow -i audio="virtual-audio-capturer" -ar 96000 -ac 2 ^
  -af "highpass=f=20,aresample=16000:phase_shift=24:filter_size=256:cutoff=0.97:dither_method=shibata,aformat=sample_fmts=s16:channel_layouts=mono" ^
  -f s16le pipe:1
```

要求：

1. 不得要求设备直接输出 16 kHz：那会让设备滤波器替我们重采样，而它不受本项目控制；采集质量按设备支持的最高采样率取，探测失败时退回 48 kHz 立体声，探测失败绝不能让任务失败。
2. 唯一一次降采样必须显式给出抗混叠参数（`phase_shift=24`、`filter_size=256`、`cutoff=0.97`）与 16 位抖动（`dither_method=shibata`）。每次降采样都是把 8 kHz 以上整段折回语音频带的地方，默认参数不足以依赖。
3. `highpass=f=20` 用于去掉部分回环设备自带的直流偏置与次低频隆隆声，Silero VAD 会把它读成能量。
4. 不使用 `resampler=soxr`：本机 FFmpeg 在 `-h filter=aresample` 里列出该选项，但构建时未包含它，一用就让整个 filter graph 失败（"Requested resampling engine is unavailable"）。
5. 管道内一律 16 kHz 单声道 S16LE，样本计数与 `media_time()` 的关系不变。

### 5.3 为什么模型输入必须是 16 kHz（不得提高）

"提高采集采样率"只能加在链路的**前端**（设备原生格式、视频源格式），不能加在模型输入上。三个模型都是 16 kHz 训练的，实测不接受其它采样率：

| 环节 | 约束 | 依据 |
| --- | --- | --- |
| VAD | 只接受 8000 / 16000 | `silero_vad/utils_vad.py:493` `VADIterator does not support sampling rates other than [8000, 16000]` |
| ASR | mel 前端固定 16000 | `WhisperFeatureExtractor(chunk_length=8)`，传 `sampling_rate=96000` 直接抛错："was trained using a sampling rate of 16000" |
| Smart Turn | 同上，固定 16000 | `models.py:143` 以 `sampling_rate=SAMPLE_RATE` 调用同一个 Whisper mel 前端 |
| ForcedAligner | 与 ASR 同一音频前端 | 同源处理器 |

把 96 kHz 样本直接喂给 16 kHz 前端（不重采样）不会报错，但会把语音按 6 倍时长摊开——6.98 秒日语在 96 kHz 输入下转写结果为空，16 kHz 输入下正常（`tools\probe_rate_96k.py` 实测）。因此**提高内部采样率只会得到垃圾字幕**；音质只能在"降采样做得好不好"上争取，不属于可调参数。

16 bit 中间管线的量化噪声约 −96 dBFS，低于任何真实录音的底噪，故中间格式仍为 S16LE，不做浮点管道。

界面在「采集中」一行里显示实际采集格式（如 `采集 96000 Hz / 2 ch`），使"是否按最高质量采集"可以从窗口本身判断，而不必回看命令行。

实时模式只需要两个执行角色：

- 采集线程持续读取 PCM，为每块数据写入采集时的 `start_sample`，再放入有界队列；
- 处理线程顺序完成断句、识别、对齐、翻译和写入。

采用媒体管线常见的“有界队列 + 背压 + 明确过载失败”：

- 队列容量固定为 30 秒 PCM，按样本数计算，不暴露为首版 CLI 参数；
- 采集线程写队列时最多阻塞 1 秒，给处理线程正常释放空间；
- 仍无法写入时置为 `audio_queue_overrun`，立即停止 FFmpeg 采集并关闭生产端；
- 处理线程继续排空已经成功入队的数据，保留此前生成的 SRT/JSONL，最后返回非零退出码并明确说明字幕不完整；
- 禁止丢弃最旧块、最新块或未完成发言，也不加入磁盘队列、动态批处理、优先级调度或多级队列。

这是一个正确性边界：在持续处理速度低于实时速度时，“无限连续采集、有限内存、绝不丢数据”无法同时保证。首版选择在上限处明确失败，不能静默生成缺段或错位字幕。队列等待只增加字幕出现延迟，不改变块的 `start_sample`，因此不得改变字幕时间轴。

### 5.3 输出

SRT 使用 UTF-8、连续序号和标准 `HH:MM:SS,mmm` 时间。起点向下取整到毫秒、终点向上取整到毫秒；每完成一句立即追加并刷新。

`.srt` 是最终外挂字幕，不写回视频容器，也不烧录到画面。首版 SRT 只保存序号、时间和文本；字体、字号、颜色和位置由播放器决定，因此首版无需安装或分发字体，也不承诺跨播放器显示完全一致。若以后明确要求统一视觉样式，应另行输出外挂 `.ass`，默认字体只允许使用可再分发的 `Noto Sans CJK SC`（SIL Open Font License 1.1）并随字体保留许可证；仍不得修改原视频。

同名 JSONL 只记录排错所需内容：字幕序号、起止时间、源语言、原文、译文、所选模型/设备、总耗时、积压秒数，以及失败阶段和错误摘要。它不是公共 API，不要求建立日志模式框架。

必须保持：

1. 音频始终为 16 kHz、单声道、PCM S16LE；
2. 已确认的发言不可继续追加；
3. 翻译输入来自 SaT 完整句；
4. 字幕时间来自 ForcedAligner，不按字符比例估算；
5. 同一任务只运行一个 Hy-MT2；
6. 子进程、线程和文件句柄在正常退出、错误和 Ctrl+C 后都能释放。

## 6. 命令行契约

```powershell
subtitle-cli offline `
  --input "C:\Users\29279\LiveSub\testdata\example.mkv" `
  --source-lang auto `
  --mt-model 7b `
  --mt-device gpu `
  --output "C:\Users\29279\LiveSub\outputs\example.zh.srt"
```

```powershell
subtitle-cli live `
  --audio-device "virtual-audio-capturer" `
  --source-lang auto `
  --mt-model 7b `
  --mt-device gpu `
  --output "C:\Users\29279\LiveSub\outputs\live.zh.srt"
```

| 参数 | 允许值 |
|---|---|
| `--source-lang` | `auto`、`en`、`ja` |
| `--mt-model` | `7b`、`1.8b`，默认 `7b` |
| `--mt-device` | `gpu`、`cpu`，默认 `gpu` |
| `--input` | `offline` 的视频路径 |
| `--audio-device` | `live` 的 DirectShow 设备名 |
| `--output` | `.srt` 路径 |

不提供目标语言、ASR 设备、断句阈值、后端注册或 UI 参数。

翻译请求固定使用空 system prompt，用户消息为：

```text
将以下文本翻译为简体中文，注意只需要输出翻译后的结果，不要额外解释：

{source_text}
```

## 7. 开发顺序与最少闸门

### G0：一次启动预检

只检查：

- `.venv` 的 Python 3.12 可运行；
- PyTorch 为 AMD HIP 且一次 FP16 运算成功；
- 本次默认链路需要的模型和工具存在；
- FFmpeg 与 llama-server 能启动并看到预期设备。

失败时指出缺失项并回到部署指南。不要在这里重复下载、全量哈希或跑四种翻译组合。

### G1：离线纵向闭环

按实际顺序完成：视频音频流 → VAD/Smart Turn → ASR → SaT → 对齐 → 7B GPU 翻译 → SRT。

闸门：一段英语和一段日语短视频成功生成可加载的外挂 SRT；时间单调；另用一段“音轨 PTS 比视频起点晚 2 秒、开头和末尾各有一句话”的校准视频验证，两句起止时间与人工标注误差均不超过 300 ms 且没有累计漂移；输入视频运行前后 SHA-256 不变；错误能定位到具体阶段；进程正常回收。

### G2：实时闭环

复用 G1 组件，只新增 DirectShow 采集、一个有界队列和有序停止。

闸门：默认 7B GPU 连续运行 10 分钟，字幕持续追加，无逐词/半句输出、`audio_queue_overrun`、静默丢音频和残留子进程；停止输入后 30 秒内排空队列。记录端到端 p95 延迟与最大积压，但首版不建立复杂性能分级。若队列达到 30 秒或 7B 无法通过，按默认交付阻塞报告，不得静默改用 1.8B。

### G3：可选模式与发布收尾

- 对已安装的 1.8B GPU、7B CPU、1.8B CPU 各做一次“启动、翻译英/日短句、停止”冒烟；
- 不具备实时性能的组合只在 README 标为离线/对照，不建立自动调度；
- 在全新 `.venv` 按锁文件复现一次默认链路；
- 完成 README、第三方通知和验证记录。

闸门：默认组合完整通过；可选组合有真实结果或明确写“未验证/未安装”。

## 8. 测试与检查原则

必须保留的纯逻辑测试只有：

- SaT 文本范围到 ForcedAligner 时间范围的映射；
- PCM `start_sample`、发言局部时间到视频绝对时间的换算；
- SRT 序号、格式和单调时间；
- CLI 参数到模型路径和 llama 参数的映射；
- 队列正常背压、满载失败、排空、取消和子进程回收；
- 配置只要求本次选择的模型存在。

集成测试只在相关边界变化后运行：

- 改音频输入：重跑一个离线样本或一个实时样本；
- 改断句、ASR 或对齐：重跑英/日短样本；
- 改翻译器：重跑受影响模型/设备的短句；
- 改完整管线：重跑 G1；
- 最终发布：重跑 G1、G2 和干净环境默认冒烟。

不要把 30 分钟语料、100 句压力、四组合长测、完整模型哈希和全仓扫描设为每次开发闸门。需要研究质量或正式性能声明时再单独执行，并把结果作为报告附件，而不是扩展业务框架。

## 9. 错误处理边界

只需在以下位置补充上下文并向上返回错误：

- 配置和必需文件加载；
- FFmpeg/llama-server 启动、请求和退出；
- 模型加载与推理；
- 文本到时间戳映射；
- SRT/JSONL 写入；
- CLI 顶层。

不在每个小函数捕获异常，不定义通用 `Result` 框架，不静默切换模型或设备。日志保留阶段、输入、模型/设备、退出码和首个根因，但不保存原始音频或敏感信息。

## 10. 明确不做

首版不创建也不预留：

- 图形界面、Web、服务端 API 或后台常驻服务；
- 插件式 ASR、翻译、断句或设备体系；
- 模型自动下载、安装器或自动升级；
- Whisper、云模型或其他自动回退；
- 说话人分离、多音轨、字幕编辑、内嵌/烧录字幕、ASS 样式输出或多目标语言；
- 数据库、任务系统、分布式推理或容器部署；
- 为性能测试建立监控平台。

UI 安装启动器由 `docs\03_UI安装启动器开发执行文档..md` 单独处理，CLI 不为其预建框架。

## 11. 最终验收

以下条件满足即可交付 CLI 首版：

1. Windows 11 原生环境中，英语和日语短视频都能生成与视频时间轴对齐、可加载的简体中文外挂 SRT，输入视频 SHA-256 不变。
2. 系统音频连续采集 10 分钟并持续追加字幕；队列未满载，处理延迟不改变采集样本时间。
3. 断句遵循 Silero + Smart Turn + SaT 流程，没有手工句末规则。
4. ASR/Aligner 确认使用 AMD HIP；时间戳来自 ForcedAligner。
5. 默认 `7b + gpu` 完整链路通过；失败时不得自动降级到 1.8B。
6. `--mt-model` 与 `--mt-device` 能正确选择已安装的模式；缺少可选模型时给出清晰错误。
7. 一次任务只加载一个 Hy-MT2，退出后无残留子进程。
8. 从锁文件能在新 `.venv` 复现默认链路。
9. README、验证记录和实际结果一致；未验证内容没有被写成支持。

## 12. CLI 交付产物

开发完成后应交付：

```text
C:\Users\29279\LiveSub\
├─ src\subtitle_cli\          # 可运行 CLI 源码
├─ pyproject.toml              # 命令入口和项目元数据
├─ requirements.lock           # 已验证 Python 依赖
├─ config.toml                 # 相对路径配置
├─ tests\                      # 最小逻辑与集成测试
├─ README.md                   # 安装完成后的运行说明
├─ docs\验证记录.md            # G0-G3 实际结果
└─ THIRD_PARTY_NOTICES.md      # 第三方来源与许可证通知
```

模型、`.venv`、GPU 运行时和测试媒体是本机部署内容，不是源码交付包的一部分。

最终报告只需列出：变更文件、G0-G3 结果、英/日离线与实时结果、可选翻译模式结果、运行示例、未验证项和已知限制。只有第 11 节全部满足后，才称“CLI 首版完成”。

## 13. 关键依据

- [GStreamer `queue`：有界队列默认阻塞、满载发出 overrun，丢弃是显式可选行为](https://gstreamer.freedesktop.org/documentation/coreelements/queue.html)
- [FFmpeg `aresample` 与时间戳同步](https://ffmpeg.org/ffmpeg-filters.html#aresample-1)
- [FFmpeg `first_pts`：用静音补齐晚于视频开始的音轨](https://ffmpeg.org/ffmpeg-resampler.html)
- [SaT / wtpsplit 官方用法](https://github.com/segment-any-text/wtpsplit#usage)
- [Qwen3-ForcedAligner 官方说明](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B)
- [Noto Sans CJK 字体许可证（SIL OFL 1.1）](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE)
