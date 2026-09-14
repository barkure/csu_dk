"""合并外部打卡结果。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dbtools


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else "-"
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except ValueError:
        print("结果文件不是合法 JSON", file=sys.stderr)
        return 2
    applied = dbtools.merge_results(payload)
    print(f"并入：账号 {applied['accounts']} 个、记录 {applied['records']} 条、"
          f"登录态 {applied['sessions']} 个，跳过 {applied['skipped']} 个（账号不存在）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
