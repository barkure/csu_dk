"""批量执行待打卡账号。"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as cfg
from app import db, scheduler
from app.clock import local_now, to_local_iso
from app.domain import Trigger

_RESULT_FIELDS = ("last_run_at", "last_status", "last_message", "dkdz")
_SESSION_FIELDS = ("token", "casual", "cookies", "token_at", "auth_error")


def results_payload(accounts: list[dict], since: str) -> dict:
    out: dict = {"accounts": {}}
    for account in accounts:
        row = db.get_account_by_id(account["id"])
        records = [r for r in db.list_records(account["id"], 20) if str(r["run_at"]) >= since]
        if not records:
            continue
        item = {key: row.get(key) for key in (*_RESULT_FIELDS, *_SESSION_FIELDS)}
        item["records"] = [
            {"run_at": r["run_at"], "trigger": r["trigger"], "status": r["status"],
             "message": r["message"], "dksj": r["dksj"]}
            for r in records
        ]
        out["accounts"][str(account["id"])] = item
    return out


def main() -> int:
    today = to_local_iso(local_now(cfg.config.tz))[:10]
    accounts = db.all_enabled_accounts()
    if "--all" not in sys.argv:
        accounts = [a for a in accounts if str(a.get("last_run_at") or "")[:10] != today]
    if not accounts:
        print("今天没有待打卡的账号")
        return 0

    print(f"跑 {len(accounts)} 个账号（{local_now(cfg.config.tz):%H:%M:%S}）")
    counts: Counter[str] = Counter()
    started = time.time()
    since = to_local_iso(local_now(cfg.config.tz))
    for index, (account, result) in enumerate(
        scheduler.run_batch(Trigger.MANUAL, force=True, accounts=accounts), 1
    ):
        counts[result["status"]] += 1
        print(f"  [{index:>2}/{len(accounts)}] acc={account['id']:<4} "
              f"{result['status']:<8} {result['message'][:46]}", flush=True)
        if index < len(accounts):
            time.sleep(1)
    print(f"\n耗时 {time.time() - started:.0f} 秒   结果：{dict(counts)}")
    return _report_if_asked(accounts, since)


def _report_if_asked(accounts: list[dict], since: str) -> int:
    path = push = ""
    for index, arg in enumerate(sys.argv):
        if arg == "--report" and index + 1 < len(sys.argv):
            path = sys.argv[index + 1]
        if arg == "--push" and index + 1 < len(sys.argv):
            push = sys.argv[index + 1]
    if not path and not push:
        return 0

    text = json.dumps(results_payload(accounts, since), ensure_ascii=False, indent=2)
    if path:
        target = Path(path)
        target.write_text(text, encoding="utf-8")
        target.chmod(0o600)
        print(f"结果已写入 {target}（含登录态，权限 600）")
    if push:
        command = "cd /opt/csu_dk && .venv/bin/python tools/apply_results.py -"
        done = subprocess.run(["ssh", push, command], input=text, text=True, check=False)
        print("已回传" if done.returncode == 0 else f"回传失败（退出码 {done.returncode}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
