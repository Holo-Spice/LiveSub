# Third-party components

LiveSub uses separately installed tools, Python packages and model files. They are not included in this source package. Retain their upstream license files when distributing them.

| Component | Source | Stated license |
| --- | --- | --- |
| Qwen3-ASR and Qwen3-ForcedAligner | [Qwen models](https://huggingface.co/Qwen/Qwen3-ASR-1.7B-hf), [aligner](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B-hf) | Apache-2.0 |
| Hy-MT2-7B GGUF | [Tencent model](https://huggingface.co/tencent/Hy-MT2-7B-GGUF) | Apache-2.0 |
| Smart Turn v3 | [Pipecat model](https://huggingface.co/pipecat-ai/smart-turn-v3) | BSD-2-Clause |
| SaT `sat-3l-sm` | [Model page](https://huggingface.co/segment-any-text/sat-3l-sm) | MIT |
| llama.cpp | [Project license](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) | MIT |
| FFmpeg | [FFmpeg legal information](https://ffmpeg.org/legal.html) | Depends on selected build and configuration |
| AMD ROCm and PyTorch | [AMD installation guide](https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/docs/install/installryz/windows/install-pytorch.html) | See upstream distribution notices |

Python dependency versions are captured in `requirements.lock`; license terms for each installed distribution remain with its upstream package. This file is a source and license pointer, not a replacement for full notices in a redistributed binary bundle.
