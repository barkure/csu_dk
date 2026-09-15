"""检查并更新楼栋坐标缓存。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import buildings, db


def grouped(accounts: list[dict]) -> dict[str, int]:
    groups: dict[str, int] = {}
    for account in accounts:
        name = (account.get("dkdz") or "").strip()
        if name and buildings.cacheable(name):
            groups[name] = groups.get(name, 0) + 1
    return groups


def main() -> int:
    accounts = db.all_enabled_accounts()
    groups = grouped(accounts)
    seed = dict(buildings._seed()["points"])
    learned = buildings._learned()

    print(f"账号 {len(accounts)} 个，用到 {len(groups)} 种楼栋名\n")
    print(f"{'楼栋（学校写法）':<12}{'坐标':<26}{'账号':>4}  来源")
    print("-" * 68)
    for name in sorted(set(groups) | set(seed) | set(learned)):
        coord = buildings._coord(learned.get(name)) or buildings._coord(seed.get(name))
        where = "学到" if name in learned else ("种子" if name in seed else "**缺坐标**")
        text = f"({coord[0]:.6f}, {coord[1]:.6f})" if coord else ""
        print(f"{name:<14}{text:<26}{groups.get(name, 0):>4}  {where}")

    missing = [name for name in sorted(groups) if name not in seed and name not in learned]
    print(f"\n还没测过的楼栋：{missing if missing else '无 ✅'}（下次有学生用就自动测）")

    if "--write" in sys.argv:
        points = {**seed, **learned}
        doc = {**buildings._seed(), "points": points}
        buildings.SEED_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
        buildings.reload()
        print(f"\n已写入 {buildings.SEED_PATH}（{len(points)} 条）")
    else:
        print("\n（没加 --write，只报告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
