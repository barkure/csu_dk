"""检查并更新楼栋坐标缓存。"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import buildings, db


def meters(a, b) -> float:
    return math.hypot((a[0] - b[0]) * 111320 * math.cos(math.radians(28.16)), (a[1] - b[1]) * 110574)


def minimax(points: list[tuple[float, float]]) -> tuple[float, float]:
    if len(points) == 1:
        return points[0]
    return min(points, key=lambda p: max(meters(p, q) for q in points))


def grouped(accounts: list[dict]) -> dict[str, list[tuple[float, float]]]:
    groups: dict[str, list[tuple[float, float]]] = {}
    for account in accounts:
        name = (account.get("dkdz") or "").strip()
        if name and buildings.cacheable(name):
            groups.setdefault(name, []).append((float(account["jd"]), float(account["wd"])))
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
        coord = seed.get(name) or learned.get(name)
        where = "种子" if name in seed else ("学到" if name in learned else "**缺坐标**")
        text = f"({coord[0]:.6f}, {coord[1]:.6f})" if coord else ""
        print(f"{name:<14}{text:<26}{len(groups.get(name, [])):>4}  {where}")

    missing = [name for name in sorted(groups) if name not in seed and name not in learned]
    stale = [name for name in sorted(set(seed) & set(groups))
             if meters(tuple(seed[name]), minimax(groups[name])) > 200]
    print(f"\n还没测过的楼栋：{missing if missing else '无 ✅'}（下次有学生用就自动测）")
    print(f"种子坐标与账号实际位置差得较多的：{stale if stale else '无 ✅'}")

    if "--write" in sys.argv:
        points = {name: list(minimax(pts)) for name, pts in sorted(groups.items())}
        points.update({name: seed[name] for name in seed if name not in points})
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
