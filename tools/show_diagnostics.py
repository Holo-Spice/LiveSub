"""Read the structure of a diagnostic JSONL: which stage records exist and in what order.

The failed run's log ends with `unsupported detected source language: Chinese` and nothing about
the utterance that caused it. This prints every record that is not a subtitle, so the point where
the run stopped can be seen (an empty transcript is recorded, a language failure is not).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
print(f"records={len(records)}")
for index, record in enumerate(records, 1):
    if "subtitle" in record:
        print(f"{index:4d} subtitle {record['subtitle']:>3} {record['start']:8.3f}-{record['end']:8.3f} {record.get('source_language')!r} {record.get('original','')[:40]!r}")
    elif record.get("stage") == "startup_detail":
        continue
    else:
        fields = {key: value for key, value in record.items() if key not in ("stage", "startup_detail")}
        print(f"{index:4d} {record.get('stage')} {fields}")
