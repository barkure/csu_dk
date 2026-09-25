from __future__ import annotations

import math
import random

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
    def test_seed_coordinates_are_in_changsha(self):
        for name, coord in buildings._seed()["points"].items():
            assert 112.8 < coord[0] < 113.1, f"{name} 经度离谱：{coord}"
            assert 28.0 < coord[1] < 28.3, f"{name} 纬度离谱：{coord}"

    def test_base_point_is_in_changsha(self):
        base = buildings.base()
        assert 112.8 < base[0] < 113.1 and 28.0 < base[1] < 28.3

    def test_resolve_by_school_name(self):
        assert buildings.resolve("升华24栋") == pytest.approx((112.936237, 28.158935), abs=1e-5)
        assert buildings.resolve("不存在栋") is None
        assert buildings.resolve("") is None


class TestCacheable:
    @pytest.mark.parametrize("name", ["升华24栋", "3舍-1", "桃B-2", "湘雅B栋", "研北1"])
    def test_building_names_are_cacheable(self, name):
        assert buildings.cacheable(name)

    @pytest.mark.parametrize("name", ["你申报的租房地址", "校外实习地址", "住宿地址不详", ""])
    def test_shared_labels_are_not_cacheable(self, name):
        assert not buildings.cacheable(name)


class TestLearn:
    def test_learned_building_is_recorded(self):
        assert buildings.resolve("升华2栋") is None
        buildings.learn("升华2栋", (112.9361, 28.1571))
        assert buildings.resolve("升华2栋") == (112.9361, 28.1571)

    def test_shared_labels_are_not_learned(self):
        buildings.learn("你申报的租房地址", (112.9, 28.1))
        assert buildings.resolve("你申报的租房地址") is None

    def test_broken_file_is_treated_as_empty(self):
        buildings._learned_path().write_text("{ 不是 json", encoding="utf-8")
        buildings.reload()
        assert buildings.resolve("升华8栋")
        assert buildings.resolve("升华2栋") is None

    def test_learned_wins_over_seed(self):
        buildings.learn("升华8栋", (112.9, 28.1))
        assert buildings.resolve("升华8栋") == (112.9, 28.1)


class TestGeometry:
    def test_shift_and_distance_are_inverse(self):
        point = (112.936833, 28.157238)
        moved = buildings.shift(point, 1234.0, -567.0)
        assert buildings.distance(point, moved) == pytest.approx(math.hypot(1234.0, 567.0), abs=1.0)

    def test_identical_probe_points_raise(self):
        point = (112.936833, 28.157238)
        with pytest.raises(UpstreamError):
            buildings._solve([point, point, point], [1.0, 2.0, 3.0])

    def test_spherical_solution_is_accurate(self):
        target = (112.927056, 28.175114)
        center = (112.936833, 28.157238)
        points = [buildings.shift(center, 3000 * math.sin(math.radians(b)),
                                  3000 * math.cos(math.radians(b))) for b in (0, 120, 240)]
        dists = [round(buildings.distance(p, target)) for p in points]
        assert haversine(buildings._solve(points, dists), target) < 2.0


class TestScatter:
    CENTER = (112.936833, 28.157238)

    def test_points_stay_within_radius(self):
        rng = random.Random(20260918)
        offsets = [buildings.distance(self.CENTER, buildings.scatter(self.CENTER, 50.0, rng))
                   for _ in range(500)]
        assert all(0.0 <= offset <= 50.0 for offset in offsets)

    def test_nonpositive_radius_returns_center(self):
        assert buildings.scatter(self.CENTER, 0) == self.CENTER
        assert buildings.scatter(self.CENTER, -1.0) == self.CENTER

    def test_points_are_random(self):
        assert len({buildings.scatter(self.CENTER, 50.0) for _ in range(20)}) > 1

    def test_points_are_area_uniform(self):
        rng = random.Random(7)
        offsets = [buildings.distance(self.CENTER, buildings.scatter(self.CENTER, 100.0, rng))
                   for _ in range(2000)]
        inner = sum(1 for offset in offsets if offset < 50.0) / len(offsets)
        assert 0.20 < inner < 0.30
        assert max(offsets) > 90.0


class TestLocate:
    @pytest.mark.parametrize("target", [
        (112.935978, 28.158930),
        (112.927096, 28.175085),
        (112.940000, 28.220000),
    ])
    def test_locates_declared_point(self, target):
        school = FakeSchool(target)
        found, name = buildings.locate(school, (112.936833, 28.157238))
        assert haversine(found, target) < 5.0, f"误差 {haversine(found, target):.1f} 米"
        assert name == "升华24栋"
        assert school.calls == 4

    def test_locates_remote_point(self):
        target = (116.425305, 39.862858)
        school = FakeSchool(target)
        found, _ = buildings.locate(school, (112.936833, 28.157238))
        assert haversine(found, target) < 20.0
        assert school.calls == 8

    def test_raises_when_location_api_is_broken(self):
        class Broken:
            def check_location(self, jd, wd):
                return {"code": "500", "data": None}

        with pytest.raises(UpstreamError):
            buildings.locate(Broken(), (112.936833, 28.157238))


class TestForStudent:
    def test_cache_hit_costs_two_queries(self):
        school = FakeSchool((112.936833, 28.157238), name="升华8栋")
        coord, name, verdict, source = buildings.for_student(school)
        assert (source, name) == ("cache", "升华8栋")
        assert coord == buildings.resolve("升华8栋")
        assert verdict["canDk"]
        assert school.calls == 2

    def test_known_cached_point_needs_no_base_probe_or_distance(self):
        cached = buildings.resolve("升华8栋")

        class School:
            def __init__(self):
                self.calls = []

            def check_location(self, jd, wd):
                self.calls.append((jd, wd))
                if (jd, wd) != cached:
                    raise UpstreamError("基准点查询失败")
                return {"code": "200", "data": {"canDk": True}}

        school = School()
        coord, name, judged, source = buildings.for_student(school, "升华8栋")

        assert (coord, name, source) == (cached, "升华8栋", "cache")
        assert judged["canDk"] is True
        assert school.calls == [cached]

    def test_unseen_building_is_measured_and_learned(self):
        school = FakeSchool((112.936292, 28.156628), name="升华2栋")
        assert buildings.resolve("升华2栋") is None
        coord, name, _, source = buildings.for_student(school)
        assert (source, name) == ("located", "升华2栋")
        assert haversine(coord, (112.936292, 28.156628)) < 5.0
        assert buildings.resolve("升华2栋") == coord

    def test_falls_back_to_probe_when_cached_point_rejected(self):
        school = FakeSchool((112.936292, 28.156628), name="升华8栋", can_dk=False)
        coord, _, _, source = buildings.for_student(school)
        assert source == "located"
        assert haversine(coord, (112.936292, 28.156628)) < 5.0

    def test_off_campus_label_is_not_cached(self):
        school = FakeSchool((112.927056, 28.175114), name="你申报的租房地址")
        coord, name, _, source = buildings.for_student(school)
        assert (source, name) == ("located", "你申报的租房地址")
        assert haversine(coord, (112.927056, 28.175114)) < 5.0
        assert buildings.resolve("你申报的租房地址") is None
        assert "你申报的租房地址" not in buildings._learned()

    def test_accepted_base_point_needs_no_distance(self):
        class BaseAccepted:
            def check_location(self, jd, wd):
                return {"code": "200", "data": {"canDk": True, "bxwz": 1}}

        coord, name, verdict, source = buildings.for_student(BaseAccepted())

        assert (coord, name, source) == (buildings.base(), "", "base_accepted")
        assert verdict["canDk"]

    def test_distance_does_not_use_base_shortcut(self):
        school = FakeSchool((112.936833, 28.157238), name="升华8栋")
        _, _, _, source = buildings.for_student(school)
        assert source != "base_accepted"


class TestVerdict:
    class NoDistance:
        def __init__(self, data):
            self.data = data

        def check_location(self, jd, wd):
            return {"code": "200", "data": self.data}

    def test_verdict_does_not_require_distance(self):
        school = self.NoDistance({"canDk": True, "bxwz": 1})
        assert buildings.verdict(school, buildings.base())["canDk"] is True

    def test_check_still_requires_distance(self):
        school = self.NoDistance({"canDk": True})
        with pytest.raises(UpstreamError):
            buildings.check(school, buildings.base())

    def test_raises_on_upstream_error(self):
        school = self.NoDistance(None)
        with pytest.raises(UpstreamError):
            buildings.verdict(school, buildings.base())
