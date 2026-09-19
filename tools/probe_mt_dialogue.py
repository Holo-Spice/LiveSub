"""Same comparison as probe_mt_prompts but on dialogue that actually has character voice.

The podcast capture is all polite です/ます narration, which cannot show whether an instruction
preserves register, so this one uses casual, blunt, feminine, masculine, honorific and
tearful lines — the things a Japanese film or anime subtitle actually has to carry.
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SENTENCES = [
    "おい、てめぇ。ここで何やってんだ、コラ。",
    "あたし、そんなの知らないもん。勝手にすれば？",
    "先輩、その件につきましては、明日までにご報告いたします。",
    "課長、恐れ入りますが、もう一度ご確認いただけますでしょうか。",
    "うわっ、なんだよこれ、マジかよ……。",
    "行くぞ、みんな！ 遅れたら置いてくからな！",
    "……ごめん。わたし、ずっとあなたに嘘をついてたの。",
    "いいか、よく聞け。ここから先は俺一人で行く。",
    "お兄ちゃん、それわたしのプリン！ 返してよ！",
    "ばかやろう。そんなこと言われなくたって、わかってるよ。",
    "すみません、少々お待ちいただけますでしょうか。ただいま担当者を呼んでまいります。",
    "この野郎……よくもやってくれたな。覚えてろよ。",
]

PROMPTS = [
    ("内置默认", ""),
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
        "你是日本动画的专业字幕译者，请把下面的日语台词翻译成简体中文。"
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
    parser.add_argument("--model", default="7b")
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    from subtitle_cli.translator import Translator

    chosen = [int(part) for part in args.only.split(",")] if args.only else list(range(len(PROMPTS)))
    results: dict[str, list[str]] = {}
    with Translator(paths["llama_server"], paths[f"hymt_{args.model}"], paths["llama_runtime_bin"], "gpu", slots=1) as translator:
        for index in chosen:
            label, prompt = PROMPTS[index]
            translator.prompt = prompt.strip() or translator.prompt
            texts, started = [], time.monotonic()
            for sentence in SENTENCES:
                texts.append(translator.translate(sentence))
            results[label] = texts
            print(f"{label:12} {time.monotonic() - started:5.1f} s for {len(SENTENCES)} lines")

    for position, sentence in enumerate(SENTENCES):
        print("\n" + "=" * 100)
        print(f"[{position + 1}] {sentence}")
        for label, _ in (PROMPTS[index] for index in chosen):
            print(f"  {label:12} {results[label][position]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
