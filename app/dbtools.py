"""数据库结构检查。"""
from __future__ import annotations

from . import config as cfg
from . import db


def init() -> str:
    """建库：新文件按最新结构一次建好；已存在的库只做结构核对。"""
    existed = cfg.DB_PATH.exists() and any(db._structure(db._conn).values())
    db._ensure_schema()
    return "已存在，结构核对通过" if existed else "已按最新结构创建"


def check() -> tuple[bool, list[str]]:
    expected = db._reference_structure()
    actual = db._structure(db._conn)
    differences = db.missing_structure(expected, actual) + db.unexpected_structure(expected, actual)
    return not differences, differences
