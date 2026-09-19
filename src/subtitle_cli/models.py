"""The fixed CPU turn detectors and AMD HIP speech models."""

from __future__ import annotations

import importlib
import os
import time
from typing import Callable

import numpy as np

from .audio import SAMPLE_RATE


def pcm_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def configure_convolution_backend(torch) -> bool:
    """Avoid MIOpen's convolution path, which fails on this ROCm/gfx1201 build.

    Qwen3-ASR's audio encoder calls conv2d, and MIOpen raises
    "RuntimeError: miopenStatusInternalError" for it on the RX 9070 XT, which aborts the
    whole task at the first utterance. ATen's fallback convolution returns the identical
    transcript. Set LIVESUB_ENABLE_MIOPEN=1 to go back to MIOpen once the ROCm stack is fixed.
    """
    if os.environ.get("LIVESUB_ENABLE_MIOPEN") == "1":
        return bool(torch.backends.cudnn.enabled)
    torch.backends.cudnn.enabled = False
    return bool(torch.backends.cudnn.enabled)


class Models:
    """Loads every model once per task.

    ``on_detail(label, seconds)`` is called before each import/constructor with ``None`` and
    again with its cost afterwards. Without it a start that never finishes leaves no trace of
    which item is responsible, and a stalled load is indistinguishable from a slow one.
    """

    def __init__(self, paths: dict, on_progress: Callable[[str], None] | None = None, on_detail: Callable[[str, float | None], None] | None = None, hotwords: str = ""):
        """``hotwords`` is the context that biases recognition towards a word list.

        It is free text, not a list: the model reads it as context, so a sentence describing
        the domain works as well as comma separated terms. Empty means no system message at
        all, which is what the CLI does unless the user asks for one.
        """
        self.hotwords = (hotwords or "").strip()

        def progress(stage: str) -> None:
            if on_progress is not None:
                on_progress(stage)

        def detail(label: str, work):
            if on_detail is not None:
                on_detail(label, None)
            started = time.monotonic()
            try:
                return work()
            finally:
                if on_detail is not None:
                    on_detail(label, time.monotonic() - started)

        def load_transformers():
            from transformers import AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor, WhisperFeatureExtractor

            return AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor, WhisperFeatureExtractor

        def load_sat():
            from wtpsplit import SaT

            return SaT

        self._detail = detail
        self._note_reporter = on_progress
        progress("model_deps")
        ort = detail("onnxruntime", lambda: importlib.import_module("onnxruntime"))
        torch = detail("torch", lambda: importlib.import_module("torch"))
        available, hip_version, devices = detail("torch_cuda", lambda: (torch.cuda.is_available(), torch.version.hip, torch.cuda.device_count()))
        detail("torch_devices", lambda: [torch.cuda.get_device_name(index) for index in range(devices)])
        if not available or hip_version is None:
            raise RuntimeError("AMD HIP GPU unavailable; Qwen models require cuda:0 on ROCm")
        configure_convolution_backend(torch)
        self.torch = torch
        silero_vad = detail("silero_vad", lambda: importlib.import_module("silero_vad"))
        self.vad = detail("silero_vad_model", lambda: silero_vad.VADIterator(silero_vad.load_silero_vad(onnx=True), sampling_rate=SAMPLE_RATE))
        self.turn = detail("smart_turn", lambda: ort.InferenceSession(str(paths["smart_turn"]), providers=["CPUExecutionProvider"]))
        AutoModelForMultimodalLM, AutoModelForTokenClassification, AutoProcessor, WhisperFeatureExtractor = detail("transformers", load_transformers)
        self.features = detail("whisper_features", lambda: WhisperFeatureExtractor(chunk_length=8))
        SaT = detail("wtpsplit", load_sat)
        self.sat = detail("sat_model", lambda: SaT(str(paths["sat"]), tokenizer_name_or_path=str(paths["sat_tokenizer"]), ort_providers=["CPUExecutionProvider"]))
        progress("model_asr")
        self.asr_processor = detail("asr_processor", lambda: AutoProcessor.from_pretrained(paths["qwen_asr"], local_files_only=True))
        weights = detail("asr_weights", lambda: AutoModelForMultimodalLM.from_pretrained(paths["qwen_asr"], dtype=torch.float16, attn_implementation="eager", local_files_only=True))
        self.asr_model = detail("asr_to_gpu", lambda: weights.to("cuda:0").eval())
        progress("model_aligner")
        self.aligner_processor = detail("aligner_processor", lambda: AutoProcessor.from_pretrained(paths["qwen_aligner"], local_files_only=True))
        weights = detail("aligner_weights", lambda: AutoModelForTokenClassification.from_pretrained(paths["qwen_aligner"], dtype=torch.float16, attn_implementation="eager", local_files_only=True))
        self.aligner_model = detail("aligner_to_gpu", lambda: weights.to("cuda:0").eval())
        detail("warmup", self.warmup)
        if self._note_reporter is not None:
            # Recorded like every other startup step, so a warm-up that did not work is
            # visible in the diagnostic JSONL instead of being silently assumed to have run.
            self._note_reporter(f"warmup: {self._warmup_notes}")
        progress("model_ready")

    def warmup(self) -> str:
        """Run one tiny call through each GPU model before the first real utterance.

        The first call into either model costs several seconds on this ROCm build (measured
        4.95 s for ASR and 1.34 s for the aligner) and every later call is an order of
        magnitude cheaper (0.70 s / 0.06 s). Paying it here, while the user is still watching
        the startup progress, hands that time back to the first subtitle instead of hiding it
        inside the first utterance. It never fails the task: a warm-up that does not work
        changes nothing except that the first real call stays slow.
        """
        self._warmup_notes = ""
        notes = []
        silence = np.zeros(int(0.6 * SAMPLE_RATE), dtype="<i2").tobytes()
        try:
            inputs = self.asr_processor.apply_transcription_request(audio=pcm_float(silence), language="Japanese").to(self.asr_model.device, self.asr_model.dtype)
            with self.torch.inference_mode():
                self.asr_model.generate(**inputs, max_new_tokens=4)
            notes.append("asr")
        except Exception as exc:
            notes.append(f"asr failed: {type(exc).__name__}")
        try:
            inputs, words = self.aligner_processor.prepare_forced_aligner_inputs(audio=pcm_float(silence), transcript="テスト", language="Japanese")
            self.aligner_model(**inputs.to(self.aligner_model.device, self.aligner_model.dtype))
            notes.append("aligner")
        except Exception as exc:
            notes.append(f"aligner failed: {type(exc).__name__}")
        self._warmup_notes = ", ".join(notes)
        return self._warmup_notes

    def vad_event(self, pcm: bytes):
        return self.vad(self.torch.from_numpy(pcm_float(pcm).copy()))

    def turn_complete(self, pcm: bytes) -> bool:
        audio = pcm_float(pcm)[-8 * SAMPLE_RATE:]
        if len(audio) < 8 * SAMPLE_RATE:
            audio = np.pad(audio, (8 * SAMPLE_RATE - len(audio), 0))
        features = self.features(audio, sampling_rate=SAMPLE_RATE, return_tensors="np", padding="max_length", max_length=8 * SAMPLE_RATE, truncation=True, do_normalize=True).input_features.astype(np.float32)
        probability = float(self.turn.run(None, {"input_features": features})[0][0].item())
        return probability > 0.5

    def transcribe(self, pcm: bytes, source_lang: str) -> tuple[str, str]:
        language = {"auto": None, "en": "English", "ja": "Japanese"}[source_lang]
        audio = pcm_float(pcm)
        if self.hotwords:
            # The documented `prompt=` argument of apply_transcription_request is dropped by
            # this transformers build ("Keyword argument `prompt` is not a valid argument"),
            # so the vocabulary goes in as a system message through the chat template. The
            # model does read it: measured on the 6.98 s Japanese sample, a system message
            # shifted the transcript's wording (`パック牛乳` stayed correct with the word list
            # and degraded to `バック牛乳` without it), and a deliberately wrong "the audio is
            # English" instruction also changed the output. It costs 30 prompt tokens and
            # about 0.02 s per utterance.
            chat = [
                {"role": "system", "content": [{"type": "text", "text": self.hotwords}]},
                {"role": "user", "content": [{"type": "audio", "audio": audio}]},
                {"role": "assistant", "content": [{"type": "text", "text": f"language {language}<asr_text>"}]} if language else {"role": "assistant", "content": [{"type": "text", "text": ""}]},
            ]
            inputs = self.asr_processor.apply_chat_template(chat, tokenize=True, return_dict=True, continue_final_message=True).to(self.asr_model.device, self.asr_model.dtype)
        else:
            inputs = self.asr_processor.apply_transcription_request(audio=audio, language=language).to(self.asr_model.device, self.asr_model.dtype)
        with self.torch.inference_mode():
            ids = self.asr_model.generate(**inputs, max_new_tokens=1024)
        parsed = self.asr_processor.decode(ids[:, inputs["input_ids"].shape[1]:], return_format="parsed")[0]
        actual = language or parsed["language"]
        if actual not in ("English", "Japanese"):
            raise ValueError(f"unsupported detected source language: {actual}")
        return parsed["transcription"], actual

    def split(self, text: str) -> list[str]:
        return self.sat.split(text, split_on_input_newlines=False, strip_whitespace=False)

    def align(self, pcm: bytes, text: str, language: str) -> list[dict]:
        inputs, words = self.aligner_processor.prepare_forced_aligner_inputs(audio=pcm_float(pcm), transcript=text, language=language)
        inputs = inputs.to(self.aligner_model.device, self.aligner_model.dtype)
        with self.torch.inference_mode():
            result = self.aligner_model(**inputs)
        return self.aligner_processor.decode_forced_alignment(logits=result.logits, input_ids=inputs["input_ids"], word_lists=words, timestamp_token_id=self.aligner_model.config.timestamp_token_id)[0]

    def alignment_units(self, text: str, language: str) -> list[str]:
        return self.aligner_processor.split_words_for_alignment(text, language)
