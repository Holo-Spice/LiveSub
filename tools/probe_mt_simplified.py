"""Checks whether a prompt keeps the output in Simplified Chinese, which is a hard requirement.

The comparison run showed that an instruction asking for "简体中文" and consistency still
produced 繁体 characters on some lines (`稍等一下嗎？我這就去叫負責人過來。`), because the
model inherited them from the wording around them. This probe counts traditional-only
characters per candidate prompt so the recommended text is the one that actually holds.

    .venv\\Scripts\\python.exe -u tools\\probe_mt_prompts.py --only 0,1 > outputs\\a.txt   # for reference
    .venv\\Scripts\\python.exe -u tools\\probe_mt_simplified.py
"""

from __future__ import annotations

import re
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# A character is "traditional only" when it never appears in Simplified Chinese text but is a
# common traditional form. Small, explicit list: no library and no guessing.
TRADITIONAL = "們個這來對時說話沒麼樣為東車馬國學點還開關實現發後從裡隻幾應該與專業變問題覺讓於關於"
PATTERN = re.compile("[" + TRADITIONAL + "]")

SENTENCES = [
    "すみません、少々お待ちいただけますでしょうか。ただいま担当者を呼んでまいります。",
    "先輩、その件につきましては、明日までにご報告いたします。",
    "課長、恐れ入りますが、もう一度ご確認いただけますでしょうか。",
    "おい、てめぇ。ここで何やってんだ、コラ。",
    "いいか、よく聞け。ここから先は俺一人で行く。",
    "ばかやろう。そんなこと言われなくたって、わかってるよ。",
    "……ごめん。わたし、ずっとあなたに嘘をついてたの。",
    "この野郎……よくよやってくれたな。覚えてろよ。",
    "お兄ちゃん、それわたしのプリン！ 返してよ！",
    "あたし、そんなの知らないもん。勝手にすれば？",
    "行くぞ、みんな！ 遅れたら置いてくからな！",
    "うわっ、なんだよこれ、マジかよ……。",
]

FILM = (
    "你是日本电影与电视剧的专业字幕译者，请把下面的日语台词翻译成简体中文。"
    "要求：贴合人物身份与说话场合，敬语要译出礼貌与上下关系；台词口语化、简练，符合中文观众的字幕阅读习惯；"
    "人名、地名、专有名词保留日文汉字写法；只输出译文，不要解释，不要加引号。"
)
STRICT = "只能用简体中文，绝对不能出现繁体字；不要加解释、括号注释或原文注音；只输出译文。"

PROMPTS = [
    ("内置默认", ""),
    ("电影字幕-1", FILM),
    ("电影字幕-2", "将下面的日语台词翻译成简体中文字幕。保持原句的语体：敬语译得客气，粗鲁的话译得粗鲁，不要一律译成书面语；一句话能说完就不要拆成两句；术语、人名、机构名前后保持一致；只输出译文。"),
    ("电影-加强", FILM.replace("只输出译文，不要解释，不要加引号。", STRICT)),
    (
        "动漫-加强",
        "你是日本动画的专业字幕译者，请把下面的日语台词翻译成简体中文字幕。"
        "要点：保留角色的语体与性格，男性化、女性化、孩子气、老人腔的说话方式要用中文读出区别；"
        "感叹词与语气词译成中文习惯的说法，不要逐字直译；称呼按中文动画字幕的通行做法处理，通篇保持一致。"
        + STRICT,
    ),
]


def scan(text: str) -> list[str]:
    return PATTERN.findall(text)


def main() -> int:
    config = tomllib.loads((ROOT / "config.toml").read_text("utf-8"))["paths"]
    paths = {key: ROOT / value for key, value in config.items()}
    from subtitle_cli.translator import Translator

    totals: dict[str, list[str]] = {}
    with Translator(paths["llama_server"], paths["hymt_7b"], paths["llama_runtime_bin"], "gpu", slots=1) as translator:
        for label, prompt in PROMPTS:
            translator.prompt = prompt.strip() or translator.prompt
            hits: list[str] = []
            started = time.monotonic()
            for sentence in SENTENCES:
                hits.extend(scan(translator.translate(sentence)))
            totals[label] = hits
            print(f"{label:12} {time.monotonic() - started:5.1f} s  繁体字命中 {len(hits)}: {''.join(hits) or '无'}")
    print()
    best = min(totals, key=lambda label: len(totals[label]))
    print("繁体字最少的提示词:", best)
    for label, hits in totals.items():
        print(f"  {label:12} {len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
