"""命令行入口。"""
from __future__ import annotations

import sys

COMMANDS = ("all", "db-init", "db-check")

USAGE = """用法：python -m app [子命令]

  db-init                 按最新结构建库（已存在则只核对结构）
  db-check                核对当前库结构
  -h, --help              显示帮助
  不带子命令                启动服务（HTTP + 调度器同进程）
"""


def _banner() -> None:
    from . import config as cfg

    host = f"[{cfg.config.host}]" if ":" in cfg.config.host else cfg.config.host
    print("\n" + "=" * 64)
    print("  中南大学妙妙道具服务端（Python + FastAPI）")
    print("=" * 64)
    print(f"  数据目录   : {cfg.DATA_DIR}")
    print(f"  数据库     : {cfg.DB_PATH}")
    print(f"  时区       : {cfg.config.tz}")
    print(f"  监听地址   : http://{host}:{cfg.config.port}/")
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


def _run_db(command: str) -> int:
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
    if argv in (["-h"], ["--help"]):
        print(USAGE)
        return
    command = argv[0] if argv else "all"
    if len(argv) > 1 or command not in COMMANDS:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)

    if command.startswith("db-"):
        raise SystemExit(_run_db(command))

    _run()


if __name__ == "__main__":
    main()
