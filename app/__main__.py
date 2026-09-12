"""入口：python -m app [db-init | db-check | db-import --from 旧库]

服务按**单进程**设计：调度器、账号锁、CAS 并发闸门、限流都在进程内，
所以没有 web / worker 这类子命令 —— 拆开跑会让这些保护失效。
"""
from __future__ import annotations

import socket
import sys

COMMANDS = ("all", "db-init", "db-check", "db-import")

USAGE = """用法：python -m app [子命令]

  db-init                 按最新结构建库（已存在则只核对结构）
  db-check                核对当前库结构
  db-import --from 旧库    把旧库数据导入当前库，并校验密文能否解开
  不带子命令                启动服务（HTTP + 调度器同进程）
"""


def _lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _banner() -> None:
    from . import config as cfg

    lan = _lan_ip()
    print("\n" + "=" * 64)
    print("  中南大学妙妙道具服务端（Python + FastAPI）")
    print("=" * 64)
    print(f"  数据目录   : {cfg.DATA_DIR}")
    print(f"  数据库     : {cfg.DB_PATH}")
    print(f"  时区       : {cfg.config.tz}")
    print(f"  本机访问   : http://127.0.0.1:{cfg.config.port}/   ← 功能齐全（含定位）")
    if lan and cfg.config.host not in ("127.0.0.1", "localhost"):
        print(f"  局域网访问 : http://{lan}:{cfg.config.port}/   ← 手机/平板可用，但定位按钮不可用")
    print("=" * 64 + "\n")


def _run() -> None:
    import uvicorn

    from . import config as cfg

    _banner()
    uvicorn.run("app.main:app", host=cfg.config.host, port=cfg.config.port, log_level="warning")


def _run_db(command: str, argv: list[str]) -> int:
    import pathlib

    from . import config as cfg

    try:
        from . import dbtools  # 导入 db 时会核对结构，不符则在这里抛出
    except RuntimeError as error:
        print(f"数据库：{cfg.DB_PATH}")
        print(f"  {error}")
        return 1

    if command == "db-init":
        print(f"数据库：{cfg.DB_PATH}")
        print(f"  {dbtools.init()}")
        return 0

    if command == "db-check":
        ok, missing = dbtools.check()
        print(f"数据库：{cfg.DB_PATH}")
        if ok:
            print("  结构核对通过")
            return 0
        print(f"  结构不符（缺少 {'、'.join(missing)}）")
        print("  请按最新结构重建数据库后再导入旧数据")
        return 1

    index = argv.index("--from") if "--from" in argv else -1
    if index < 0 or index + 1 >= len(argv) or argv[index + 1].startswith("-"):
        print("db-import 需要 --from 旧库路径", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    source = pathlib.Path(argv[index + 1]).expanduser()
    print(f"来源库：{source}")
    print(f"目标库：{cfg.DB_PATH}")
    report = dbtools.import_from(source)
    for key, value in report.items():
        print(f"  {key}: {value if not isinstance(value, list) else ('、'.join(value) or '—')}")
    return 1 if report["unreadable"] else 0


def main() -> None:
    argv = sys.argv[1:]
    command = argv[0] if argv and not argv[0].startswith("-") else "all"
    if command not in COMMANDS:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    if command.startswith("db-"):
        raise SystemExit(_run_db(command, argv))

    _run()


if __name__ == "__main__":
    main()
