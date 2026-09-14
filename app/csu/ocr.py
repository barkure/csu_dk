"""图形验证码识别（可选依赖 ddddocr）。

没装 ddddocr、模型加载失败、识别结果不像验证码，都返回 None —— 调用方据此走人工路径，
绝不会拿空字符串去提交一次注定失败的登录（每次失败登录都在喂学校的风控）。
"""
from __future__ import annotations

_engine = None
_loaded = False


def _load():
    """惰性加载：模块导入时不碰模型（加载要一两秒、几十兆内存）。"""
    global _engine, _loaded
    if _loaded:
        return _engine
    _loaded = True
    try:
        import ddddocr

        _engine = ddddocr.DdddOcr(show_ad=False)
    except Exception as error:  # noqa: BLE001 - 没装、模型坏了都不能影响打卡主流程
        print(f"[ocr] ddddocr 不可用（{error}），验证码只能人工处理", flush=True)
        _engine = None
    return _engine


def available() -> bool:
    return _load() is not None


# CAS 验证码为 4–6 位字母或数字
MIN_LENGTH = 4
MAX_LENGTH = 6


def solve(image: bytes) -> str | None:
    engine = _load()
    if engine is None or not image:
        return None
    try:
        text = engine.classification(image)
    except Exception as error:  # noqa: BLE001 - 识别失败按"没识别出来"处理
        print(f"[ocr] 识别失败：{error}", flush=True)
        return None
    cleaned = "".join(ch for ch in str(text or "") if ch.isalnum())
    if not MIN_LENGTH <= len(cleaned) <= MAX_LENGTH:
        return None
    return cleaned
