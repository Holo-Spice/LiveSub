# LiveSub UI ↔ CLI 契约（G0）

## 实际入口与参数

安装根目录是 `LiveSub.exe` 所在目录。WPF 从此目录启动 `.venv\Scripts\python.exe -u -m subtitle_cli.ui_bridge`，工作目录也设为安装根目录。其后只传公共 CLI 参数：`offline --input <绝对视频路径>` 或 `live --audio-device <真实 DirectShow 名称>`，再传 `--source-lang auto|en|ja --mt-model 7b|1.8b --mt-device gpu|cpu --mt-slots <1-8> --output <绝对 .srt 路径>`。默认 auto、7b、gpu。WPF 离线传 `--mt-slots 3`（离线并行翻译），实时传 `--mt-slots 1`。配置相对路径以 `config.toml` 所在安装根目录为基准。Python 包由 pip 安装到该 `.venv`。

输出分成两个目录，两种模式互不共用文件：实时建议 `outputs\live\live-<时间戳>.zh.srt`，离线建议 `outputs\offline\<视频名>.zh.srt`。这样「实时与离线切换时已保存的字幕」不会混在同一份文件或同一个预览里。

原 CLI 入口在源码树中通过 `__file__` 找项目根；桥接入口明确使用工作目录。公共 CLI 在工作目录有 `config.toml` 时也使用该目录，因此非可编辑安装仍可运行；在其他目录执行现有开发命令时继续使用源码根。

## stdout 消息

仅一行一个 UTF-8 JSON；库输出、普通日志写 stderr。字段只包括：

- `{"type":"status","stage":"asr"}`；实时样本进度可附 `captured_seconds`，最多每秒一次。真实阶段有 `preflight`、`model_load`、`model_deps`、`model_asr`、`model_aligner`、`model_warmup`、`model_ready`、`translator_start`、`translator_ready`、`capturing`、`vad`、`smart_turn`、`asr`、`sat`、`aligner`、`sentence_mapping`、`translation`、`subtitle_write`。
- `{"type":"status","detail":"wtpsplit","seconds":5.03}`：模型加载内部的单个步骤，`detail` 是步骤名，`seconds` 为 null 表示刚开始、为数字表示已完成耗时。只有 `detail` 没有 `stage` 时，UI 保留当前阶段标题，只更新下方进度文字。UI 用这些真实事件推进启动进度条，不猜测百分比。
- `{"type":"subtitle","index":1,"start_ms":418,"end_ms":6178,"text":"中文句子"}`；SRT 和 JSONL 都刷新成功后发送。毫秒取值与 SRT 相同。离线并行时序号仍按媒体顺序，因为只有写盘这一步被串行化。
- `{"type":"progress","processed_seconds":281.2,"total_seconds":527.7,"eta_seconds":68.4,"speed":7.7,"decoded_seconds":520.1,"segments_done":41,"segments_open":2}`：仅离线、每 `--progress-seconds`（默认 2 秒）一条。所有数字都是实测值：`processed_seconds` 是已完成识别+对齐+翻译的音频秒数，`decoded_seconds` 是 FFmpeg 已解出的音频秒数，`total_seconds` 来自 ffprobe（读不到时该字段缺失），`speed` 是 `processed_seconds / 已用时间`，`eta_seconds` 是 `speed` 外推到剩余音频。UI 的进度条与「预计剩余」只用这些数，不猜测百分比。
- `{"type":"listening","waited_seconds":15.0,"timeout_seconds":300.0,"remaining_seconds":285.0}`：仅实时、尚未检测到第一段语音时每 5 秒一条。`--no-speech-timeout`（默认 300 秒）内没有检测到语音即以 `no_speech` 失败结束，并保留已写的（空）输出与原因。`listening` 不是 CLI 阶段名，不会出现在 `status.stage` 里。
- `{"type":"finished","result":"completed","message":"生成完成"}`；`result` 只为 `completed`、`stopped`、`incomplete`、`failed`；失败附 `stage`。桥接进程完成资源清理后发送一次。没有该消息、消息错误或非零退出码均不算完成。

WPF 向 stdin 写一行 `{"command":"stop"}`。**stdin EOF 不再表示停止**：启动器重定向的 stdin 关闭或为空时也请求停止，会在实时采集开始一秒内以「已停止」结束且零字幕；只有显式 `{"command":"stop"}` 才停止。停止请求在模型加载阶段同样有效：`startup_step` 每 0.25 秒检查一次，收到后放弃正在加载的线程并以 `stopped` 结束，进程用 `os._exit(0)` 退出，因为被放弃的线程仍在原生调用里。这样避免启动器在 60 秒后强杀进程树——强杀发生在 HIP 上下文拆除中间，是最容易让下一次启动变慢的时刻。离线停止读取后对剩余发言调用现有完整性判断；实时先停 FFmpeg 生产者，再排空队列与残余音频。WPF 最多等待 60 秒，逾时回收进程树并标记不完整。WPF 不解析 stderr 或诊断 JSONL 来驱动状态。

诊断 JSONL 另记启动细节，供排查「一直停在正在加载模型」：`startup`（模式与参数）、`startup_host`（python、工作目录、CPU 数、PATH 长度、HIP/MIOpen/HF 相关环境变量）、`startup_step`（阶段与累计秒数）、`startup_detail`（每个导入或模型构造的 `start`/`done` 与秒数）、`startup_cancelled`、`offline_pipeline`（离线模式的分段并行参数与文件时长）。每步先写 `start` 再写 `done`，所以最后一条没有配对的 `start` 就是卡住的位置。

加载的最后一步是 `startup_detail` 的 `warmup`：用 0.6 秒静音先跑一次 ASR 与对齐器，把本机 ROCm 构建上「第一次调用要 5 秒、之后只要 0.7 秒」的一次性算子初始化开销从第一句字幕挪到启动阶段；失败只记录不中止（`startup_step` 里带 `warmup: asr, aligner`）。

## 离线为什么可以不等播放

离线文件已经是完整的，所以识别的下一段、对齐的下一段和翻译的上一段可以同时在跑：解码在一个线程里持续产出整句，识别+对齐在一个工作线程里串行占用 GPU，翻译通过 llama-server 的 `--parallel` 多个槽并发，只有写盘按媒体顺序串行。停止时保留的是一份「完整运行的前缀」：当前句之后的句子与之后的所有发言都丢弃，因此 SRT 不会出现中间空洞（`--pipeline sequential` 可退回逐段串行，两条路径在 `1.mp4` 上输出逐字节相同）。

## 组件来源与当前验证边界

FFmpeg 使用部署指南的 gyan.dev essentials 归档；llama.cpp 使用 `b10708` ROCm Windows 归档；ROCm 使用 `7.14.1` multiarch 与 gfx120X 两份归档。现有本地下载的大小和 SHA-256 已核对。模型使用部署指南列出的 Hugging Face 仓库和本地下载缓存中记录的固定提交、文件大小与 SHA-256；7B 为必需，1.8B 为可选。Python/AMD 包版本来自 `requirements.lock`，AMD 索引为 `https://stable.repo.amd.com/rocm/whl-next/`。

现有验证记录证明公共 CLI 的默认 7B GPU 英日短视频及受控实时采集；用户另已手测日语句界修复。独立系统 Python 3.12、全新 `.venv` 按锁文件复现、WPF 界面、固定清单的基础下载字节数、全新安装及 UI 短流程尚未实测，不能用现有 CLI 记录代替 G3。发布前还需用户确定项目代码许可证。
