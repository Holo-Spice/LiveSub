"""Compares translation instructions on real Japanese sentences, through the real model.

The prompt is the only knob on the translation stage, so it is chosen by measurement rather
than by taste: every candidate instruction translates the same sentences with the same
llama-server and the results are printed side by side.

    .venv\\Scripts\\python.exe -u tools\\probe_mt_prompts.py [--limit 12] [--only 0,1]

Input: the distinct Japanese originals already recorded in outputs\\live-ja.zh.jsonl (a real
10 minute capture), so nothing has to be re-transcribed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The candidates. Each one is a complete instruction; the sentence is appended after a blank
# line by Translator.translate, exactly as the built-in default does.
PROMPTS = [
    (
        "内置默认",
        "",
    ),
    (
        "电影字幕-1",
        "你是日本电影与电视剧的专业字幕译者，请把下面的日语台词翻译成简体中文。"
        "要求：贴合人物身份与说话场合，敬语要译出礼貌与上下关系；台词口语化、简练，符合中文观众的字幕阅读习惯；"
        "人名、地名、专有名词保留日文汉字写法；只输出译文，不要解释，不要加引号。",
    ),
    (
        "电影字幕-2",
        "将下面的日语台词翻译成简体中文字幕。保持原句的语体：敬语译得客气，粗鲁的话译得粗鲁，"
        "不要一律译成书面语；一句话能说完就不要拆成两句；术语、人名、机构名前后保持一致；只输出译文。",
    ),
    (
        "动漫字幕",
        "你是日本动画的专业字幕译者，请把下面的日语台词翻译成简体中文字幕。"
        "要点：保留角色的语体与性格，男性化、女性化、孩子气、老人腔的说话方式要用中文读出区别；"
        "感叹词与语气词译成中文习惯的说法，不要逐字直译；招式名、组织名、称呼（如「さん」「先輩」「様」「ちゃん」）"
        "按中文动画字幕的通行做法处理，通篇保持一致；只输出译文，不要解释。",
    ),
    (
        "口语白话",
        "把下面的日语翻成简体中文。用日常说话的口吻，不要书面语，越自然越好。只输出译文。",
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=12, help="how many sentences to compare")
    parser.add_argument("--only", default="", help="comma separated prompt indexes to run")
    parser.add_argument("--model", default="7b")
    args = parser.parse_args()

    source = ROOT / "outputs" / "live-ja.zh.jsonl"
    if not source.is_file():
        print(f"missing {source}; nothing to translate")
        return 1
    sentences: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        text = (record.get("original") or "").strip()
        if text and text not in sentences:
            sentences.append(text)
    # Skip the one-word acknowledgements: they cannot show a register difference.
    sentences = [text for text in sentences if len(text) >= 12][: args.limit]
    print(f"sentences: {len(sentences)}")

    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    from subtitle_cli.translator import Translator

    chosen = [int(part) for part in args.only.split(",")] if args.only else list(range(len(PROMPTS)))
    results: dict[str, list[str]] = {}
    with Translator(paths["llama_server"], paths[f"hymt_{args.model}"], paths["llama_runtime_bin"], "gpu", slots=1) as translator:
        for index in chosen:
            label, prompt = PROMPTS[index]
            translator.prompt = prompt.strip() or translator.prompt  # empty means the built-in default
            texts, started = [], time.monotonic()
            for sentence in sentences:
                texts.append(translator.translate(sentence))
            results[label] = texts
            print(f"{label:12} {time.monotonic() - started:5.1f} s for {len(sentences)} sentences")

    for position, sentence in enumerate(sentences):
        print("\n" + "=" * 100)
        print(f"[{position + 1}] 原文: {sentence}")
        for label, _ in (PROMPTS[index] for index in chosen):
            print(f"  {label:12} {results[label][position]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
