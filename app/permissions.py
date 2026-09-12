"""数据目录/文件权限：密码虽加密落库，但邮箱、学号、坐标、记录都是明文。"""
from __future__ import annotations

import os
from pathlib import Path


def harden_dir(path: Path, mode: int = 0o700) -> bool:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if (path.stat().st_mode & 0o777) == mode:
        return False
    path.chmod(mode)
    return True


def harden_file(path: Path, mode: int = 0o600) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    if (stat.st_mode & 0o777) == mode:
        return False
    path.chmod(mode)
    return True


def harden_files(paths: list[Path], mode: int = 0o600) -> list[Path]:
    changed: list[Path] = []
    for path in paths:
        try:
            if harden_file(path, mode):
                changed.append(path)
        except OSError as error:
            print(f"[permissions] 无法修正权限 {path}：{error}")
    return changed


# 最早执行：之后创建的文件（含 SQLite 的 -wal/-shm）默认 600
os.umask(0o077)
