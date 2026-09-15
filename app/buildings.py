"""楼栋坐标缓存与探点定位。"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from . import config as cfg
from .errors import UpstreamError
from .log import log_event

SEED_PATH = Path(__file__).with_name("data") / "buildings.json"

DEFAULT_BASE = (112.936833, 28.157238)
PROBE_RADIUS_M = 3000.0
PROBE_RADIUS_RATIO = 0.05
PROBE_BEARINGS = (0.0, 120.0, 240.0)
LOCATE_ATTEMPTS = 3
VERIFY_MAX_M = 50.0
LABEL_HINTS = ("租房", "申报", "地址", "你", "我")
MAX_NAME_LEN = 12

_R = 6371000.0
_DEG = math.pi / 180.0
_DEG_LAT_M = _R * _DEG


def _deg_lon_m(lat: float) -> float:
    return _DEG_LAT_M * math.cos(math.radians(lat))


def shift(point: tuple[float, float], east: float, north: float) -> tuple[float, float]:
    return (point[0] + east / _deg_lon_m(point[1]), point[1] + north / _DEG_LAT_M)


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * _R * math.asin(min(1.0, math.sqrt(h)))


def _learned_path() -> Path:
    return cfg.config.data_dir / "buildings.learned.json"


def _read(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _coord(value) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return (float(value[0]), float(value[1]))
        except (TypeError, ValueError):
            return None
    return None


@lru_cache(maxsize=1)
def _seed() -> dict:
    data = _read(SEED_PATH)
    if not data.get("points"):
        raise UpstreamError("楼栋种子文件缺失或损坏：app/data/buildings.json")
    return data


@lru_cache(maxsize=1)
def _learned() -> dict:
    return _read(_learned_path())


def reload() -> None:
    _seed.cache_clear()
    _learned.cache_clear()


def base() -> tuple[float, float]:
    return _coord(_seed().get("base")) or DEFAULT_BASE


def names() -> list[str]:
    return sorted(set(_seed()["points"]) | set(_learned()))


def resolve(school_name: str) -> tuple[float, float] | None:
    name = (school_name or "").strip()
    if not name:
        return None
    return _coord(_learned().get(name)) or _coord(_seed()["points"].get(name))


def cacheable(school_name: str) -> bool:
    """排除不能跨账号复用的通用地址标签。"""
    name = (school_name or "").strip()
    if not name or len(name) > MAX_NAME_LEN:
        return False
    return not any(hint in name for hint in LABEL_HINTS)


def learn(school_name: str, coord: tuple[float, float]) -> None:
    """原子更新运行时缓存。"""
    name = (school_name or "").strip()
    if not cacheable(name):
        return
    learned = _learned()
    previous = _coord(learned.get(name))
    learned[name] = [coord[0], coord[1]]
    path = _learned_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(learned, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    _learned.cache_clear()
    if previous and distance(previous, coord) > 1000:
        log_event("building.relocated", level="warning", building=name,
                  previous=f"{previous[0]:.6f},{previous[1]:.6f}",
                  current=f"{coord[0]:.6f},{coord[1]:.6f}",
                  distance_m=round(distance(previous, coord)))


def check(client, point: tuple[float, float]) -> dict:
    body = client.check_location(point[0], point[1])
    data = body.get("data") if str(body.get("code")) == "200" else None
    if not isinstance(data, dict) or data.get("pcMi") is None:
        raise UpstreamError("学校接口没有返回定位距离，请稍后重试")
    data.setdefault("fwMi", 300)
    return data


def _ecef(point: tuple[float, float]) -> tuple[float, float, float]:
    """按球形地球模型将经纬度转换为 ECEF 直角坐标。"""
    lat, lon = math.radians(point[1]), math.radians(point[0])
    return (_R * math.cos(lat) * math.cos(lon), _R * math.cos(lat) * math.sin(lon), _R * math.sin(lat))


def _geo(vector) -> tuple[float, float]:
    x, y, z = vector
    return (math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y))))


def _solve(points, dists) -> tuple[float, float]:
    """在球形 ECEF 坐标系中用三点交会求经纬度。"""
    p = [_ecef(point) for point in points]
    chord = [2 * _R * math.sin(d / (2 * _R)) for d in dists]
    rows = []
    for i in (1, 2):
        normal = tuple(2 * (p[i][k] - p[0][k]) for k in range(3))
        rhs = sum(v * v for v in p[i]) - sum(v * v for v in p[0]) + chord[0] ** 2 - chord[i] ** 2
        rows.append((normal, rhs))
    (n1, k1), (n2, k2) = rows
    n1n2 = sum(a * b for a, b in zip(n1, n2, strict=True))
    det = sum(v * v for v in n1) * sum(v * v for v in n2) - n1n2 ** 2
    if det <= 1e-6 * sum(v * v for v in n1) * sum(v * v for v in n2):
        raise UpstreamError("三个探点重合了，无法测定坐标")
    along = (n1[1] * n2[2] - n1[2] * n2[1], n1[2] * n2[0] - n1[0] * n2[2], n1[0] * n2[1] - n1[1] * n2[0])
    a = (k1 * sum(v * v for v in n2) - k2 * n1n2) / det
    b = (k2 * sum(v * v for v in n1) - k1 * n1n2) / det
    anchor = tuple(a * n1[k] + b * n2[k] for k in range(3))

    qa = sum(v * v for v in along)
    qb = 2 * sum(x * y for x, y in zip(anchor, along, strict=True))
    qc = sum(v * v for v in anchor) - _R * _R
    disc = qb * qb - 4 * qa * qc
    if disc <= 0:
        roots = [-qb / (2 * qa)]
    else:
        root = math.sqrt(disc)
        roots = [(-qb + root) / (2 * qa), (-qb - root) / (2 * qa)]

    best, best_error = None, None
    for t in roots:
        candidate = _geo(tuple(anchor[k] + t * along[k] for k in range(3)))
        error = max(abs(distance(candidate, point) - d)
                    for point, d in zip(points, dists, strict=True))
        if best_error is None or error < best_error:
            best, best_error = candidate, error
    return best


def locate(client, start: tuple[float, float], *,
           radius: float = PROBE_RADIUS_M) -> tuple[tuple[float, float], str]:
    """通过三点测距和一次验证定位申报点。"""
    base_point, current = start, radius
    for _ in range(LOCATE_ATTEMPTS):
        points, dists = [], []
        for bearing in PROBE_BEARINGS:
            angle = math.radians(bearing)
            point = shift(base_point, current * math.sin(angle), current * math.cos(angle))
            dists.append(float(check(client, point)["pcMi"]))
            points.append(point)
        guess = _solve(points, dists)
        verified = check(client, guess)
        if float(verified["pcMi"]) <= VERIFY_MAX_M:
            return guess, str(verified.get("yxMc") or "")
        base_point = guess
        current = max(PROBE_RADIUS_M, PROBE_RADIUS_RATIO * min(dists))
    raise UpstreamError("没能测定这个地址的位置，请稍后重试")


def for_student(client, name: str = "") -> tuple[tuple[float, float], str, dict, str]:
    """返回经学校验证的坐标及其来源。"""
    school_name = (name or "").strip()
    if not school_name:
        school_name = str(check(client, base()).get("yxMc") or "")
    cached = resolve(school_name)
    if cached is not None:
        verdict = check(client, cached)
        if verdict.get("canDk"):
            return cached, school_name, verdict, "cache"
    coord, measured = locate(client, base())
    learn(measured or school_name, coord)
    return coord, measured or school_name, check(client, coord), "located"
