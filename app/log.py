"""结构化日志 + 敏感字段脱敏（structlog 的 processor 链）。"""
from __future__ import annotations

import re

import structlog

# 白名单放行计数/状态类字段，否则宽泛的 /code/ 会把 codes 也打成 ***
_SAFE_KEYS = {
    "event", "t", "level", "ms", "count", "status", "reason", "accounts", "flagged",
    "codes", "sessions", "records", "paths", "key_exists", "key_created",
    "account_id", "csu_username_tail", "message",
}

_SENSITIVE_KEY = re.compile(r"pass|pwd|secret|token|cookie|castgc|authorization|email|code|casual", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(r"^v1\.|^re_")


def _scrub(key: str, value: object, depth: int = 0) -> object:
    if isinstance(value, str):
        return "***" if _SENSITIVE_VALUE.search(value) else value
    if isinstance(value, dict):
        if depth > 3:
            return "[deep]"
        return {
            item_key: "***" if item_key not in _SAFE_KEYS and _SENSITIVE_KEY.search(str(item_key))
            else _scrub(str(item_key), item, depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(key, item, depth + 1) for item in list(value)[:20]]
    return value


def _redact(_logger, _method, event_dict: dict) -> dict:
    return {
        key: ("***" if key not in _SAFE_KEYS and _SENSITIVE_KEY.search(str(key)) else _scrub(str(key), value))
        for key, value in event_dict.items()
    }


structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="t"),
        _redact,
        structlog.processors.JSONRenderer(ensure_ascii=False),
    ],
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

_logger = structlog.get_logger("csu-dk")


def redact(fields: dict) -> dict:
    return {
        key: ("***" if key not in _SAFE_KEYS and _SENSITIVE_KEY.search(str(key)) else _scrub(str(key), value))
        for key, value in fields.items()
    }


def log_event(event: str, **fields: object) -> None:
    _logger.info(event, **fields)


def warn_block(lines: list[str]) -> None:
    print("\n" + "\n".join(lines) + "\n", flush=True)
