"""敏感文本脱敏。"""
from __future__ import annotations

import re

_SECRET_KEYS = r"CASTGC|JSESSIONID|authorization|token|password|passwd|pwd|cookies?|secret|casual"
_QUOTABLE_KEYS = rf"(?:{_SECRET_KEYS}|set-cookie)"
_SECRET_IN_TEXT = re.compile(
    r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"
    r"|[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}"
    r"|v1\.[A-Za-z0-9+/=_\-.]{8,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]+"
    rf"|[\"']?(?:{_QUOTABLE_KEYS})[\"']?\s*[=:]\s*\"(?:[^\"\\]|\\.)*\""
    rf"|[\"']?(?:{_QUOTABLE_KEYS})[\"']?\s*[=:]\s*'(?:[^'\\]|\\.)*'"
    r"|[\"']?(?:set-cookie|castgc|jsessionid|cookies?|authorization)[\"']?\s*[=:]\s*[^,，。)\n]+"
    rf"|[\"']?(?:{_SECRET_KEYS})[\"']?\s*[=:]\s*[\"']?[^\s\"'&,，。;；)\n]+"
    r"|(?<=[?&])[A-Za-z0-9_]+=[^\s&,，。;；]+",
    re.IGNORECASE,
)
_DETAIL_LIMIT = 160


def scrub_detail(detail: str) -> str:
    return _SECRET_IN_TEXT.sub("***", str(detail or ""))[:_DETAIL_LIMIT]


def scrub_optional(detail: str | None) -> str | None:
    """脱敏旧记录，保留空值。"""
    return scrub_detail(detail) if detail else detail
