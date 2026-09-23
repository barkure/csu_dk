"""命令行入口。"""
from __future__ import annotations

import socket
import sys

COMMANDS = ("all", "db-init", "db-check")

USAGE = """用法：python -m app [子命令]

  db-init                 按最新结构建库（已存在则只核对结构）
  db-check                核对当前库结构
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
    from .netinfo import proxy_startup_options

    _banner()
    options = proxy_startup_options()
    if cfg.config.trust_proxy and not options.get("proxy_headers"):
        print("[startup] 设置了 CSU_DK_TRUST_PROXY 但没有 CSU_DK_TRUSTED_PROXIES，"
              "代理头信任未启用（fail closed）", flush=True)
    uvicorn.run("app.main:app", host=cfg.config.host, port=cfg.config.port,
                log_level="warning", **options)


def _run_db(command: str, argv: list[str]) -> int:
    from . import config as cfg

    try:
        from . import dbtools
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
        print(f"  结构不符（{'、'.join(missing)}）")
        print("  请删除旧数据库并重新启动")
        return 1


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
