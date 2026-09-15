from __future__ import annotations

import math

import pytest

from app import buildings
from app.errors import UpstreamError


def haversine(a, b):
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[0] - a[0]) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(h))


class FakeSchool:
    def __init__(self, target=(112.935978, 28.158930), name="升华24栋", can_dk=True):
        self.target, self.name, self.can_dk, self.calls = target, name, can_dk, 0

    def check_location(self, jd, wd):
        self.calls += 1
        d = round(haversine((jd, wd), self.target))
        return {"code": "200", "data": {"pcMi": d, "yxMc": self.name,
                                        "canDk": self.can_dk and d <= 300, "fwMi": 300}}


class TestSeed:
    def test_种子里的坐标都在长沙范围内(self):
        for name, coord in buildings._seed()["points"].items():
            assert 112.8 < coord[0] < 113.1, f"{name} 经度离谱：{coord}"
            assert 28.0 < coord[1] < 28.3, f"{name} 纬度离谱：{coord}"

    def test_基准点在长沙(self):
        base = buildings.base()
        assert 112.8 < base[0] < 113.1 and 28.0 < base[1] < 28.3

    def test_按学校返回的名字就能查到坐标(self):
        assert buildings.resolve("升华24栋") == pytest.approx((112.936237, 28.158935), abs=1e-5)
        assert buildings.resolve("不存在栋") is None
        assert buildings.resolve("") is None


class TestCacheable:
    @pytest.mark.parametrize("name", ["升华24栋", "3舍-1", "桃B-2", "湘雅B栋", "研北1"])
    def test_楼栋名可以缓存(self, name):
        assert buildings.cacheable(name)

    @pytest.mark.parametrize("name", ["你申报的租房地址", "校外实习地址", "住宿地址不详", ""])
    def test_公用标签不缓存(self, name):
        assert not buildings.cacheable(name)


class TestLearn:
    def test_测到的楼栋会被记下来(self):
        assert buildings.resolve("升华2栋") is None
        buildings.learn("升华2栋", (112.9361, 28.1571))
        assert buildings.resolve("升华2栋") == (112.9361, 28.1571)

    def test_公用标签不会被写进缓存(self):
        buildings.learn("你申报的租房地址", (112.9, 28.1))
        assert buildings.resolve("你申报的租房地址") is None

    def test_写坏的文件当空处理(self):
        buildings._learned_path().write_text("{ 不是 json", encoding="utf-8")
        buildings.reload()
        assert buildings.resolve("升华8栋")
        assert buildings.resolve("升华2栋") is None

    def test_学到的优先于种子(self):
        buildings.learn("升华8栋", (112.9, 28.1))
        assert buildings.resolve("升华8栋") == (112.9, 28.1)


class TestGeometry:
    def test_平移与反算互逆(self):
        point = (112.936833, 28.157238)
        moved = buildings.shift(point, 1234.0, -567.0)
        assert buildings.distance(point, moved) == pytest.approx(math.hypot(1234.0, 567.0), abs=1.0)

    def test_探点重合时报错(self):
        point = (112.936833, 28.157238)
        with pytest.raises(UpstreamError):
            buildings._solve([point, point, point], [1.0, 2.0, 3.0])

    def test_球面解是精确的(self):
        target = (112.927056, 28.175114)
        center = (112.936833, 28.157238)
        points = [buildings.shift(center, 3000 * math.sin(math.radians(b)),
                                  3000 * math.cos(math.radians(b))) for b in (0, 120, 240)]
        dists = [round(buildings.distance(p, target)) for p in points]
        assert haversine(buildings._solve(points, dists), target) < 2.0


class TestLocate:
    @pytest.mark.parametrize("target", [
        (112.935978, 28.158930),
        (112.927096, 28.175085),
        (112.940000, 28.220000),
    ])
    def test_能测定申报点(self, target):
        school = FakeSchool(target)
        found, name = buildings.locate(school, (112.936833, 28.157238))
        assert haversine(found, target) < 5.0, f"误差 {haversine(found, target):.1f} 米"
        assert name == "升华24栋"
        assert school.calls == 4

    def test_异地也能测出来(self):
        target = (116.425305, 39.862858)
        school = FakeSchool(target)
        found, _ = buildings.locate(school, (112.936833, 28.157238))
        assert haversine(found, target) < 20.0
        assert school.calls == 8

    def test_定位读不通就报错(self):
        class Broken:
            def check_location(self, jd, wd):
                return {"code": "500", "data": None}

        with pytest.raises(UpstreamError):
            buildings.locate(Broken(), (112.936833, 28.157238))


class TestForStudent:
    def test_缓存命中只花两次查询(self):
        school = FakeSchool((112.936833, 28.157238), name="升华8栋")
        coord, name, verdict, source = buildings.for_student(school)
        assert (source, name) == ("cache", "升华8栋")
        assert coord == buildings.resolve("升华8栋")
        assert verdict["canDk"]
        assert school.calls == 2

    def test_没测过的楼栋测一次并记住(self):
        school = FakeSchool((112.936292, 28.156628), name="升华2栋")
        assert buildings.resolve("升华2栋") is None
        coord, name, _, source = buildings.for_student(school)
        assert (source, name) == ("located", "升华2栋")
        assert haversine(coord, (112.936292, 28.156628)) < 5.0
        assert buildings.resolve("升华2栋") == coord

    def test_缓存点学校不认就改走探点(self):
        school = FakeSchool((112.936292, 28.156628), name="升华8栋", can_dk=False)
        coord, _, _, source = buildings.for_student(school)
        assert source == "located"
        assert haversine(coord, (112.936292, 28.156628)) < 5.0

    def test_校外生的公用标签不入缓存(self):
        school = FakeSchool((112.927056, 28.175114), name="你申报的租房地址")
        coord, name, _, source = buildings.for_student(school)
        assert (source, name) == ("located", "你申报的租房地址")
        assert haversine(coord, (112.927056, 28.175114)) < 5.0
        assert buildings.resolve("你申报的租房地址") is None
        assert "你申报的租房地址" not in buildings._learned()
