"""保管链 / 越温责任拆分单元测试。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app import chain
from app.chain import holder_at, rebuild_chain
from app.models import Event, EventType, TripCargo, VehicleReading

T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def meas(eid, minute, temp):
    return Event(
        event_id=eid,
        cargo_id="C1",
        event_type=EventType.measurement,
        occurred_at=T0 + timedelta(minutes=minute),
        temperature=Decimal(temp),
    )


def handover(eid, minute, giver, receiver):
    return Event(
        event_id=eid,
        cargo_id="C1",
        event_type=EventType.handover,
        occurred_at=T0 + timedelta(minutes=minute),
        giver=giver,
        receiver=receiver,
    )


def test_simple_excursion_split_across_handover():
    # 10:10 超限开启 -> 10:30 A 交 B -> 10:50 不超限关闭
    events = [meas("m1", 10, "8"), handover("h1", 30, "A", "B"), meas("m2", 50, "4")]
    r = rebuild_chain("A", Decimal("5"), events)
    assert r.current_holder == "B"
    assert len(r.excursions) == 1
    ex = r.excursions[0]
    assert (ex.start, ex.end) == (
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=50),
    )
    assert ex.is_open is False
    assert [(s.start.minute, s.end.minute, s.party) for s in ex.segments] == [
        (10, 30, "A"),
        (30, 50, "B"),
    ]


def test_open_excursion_marked_open():
    r = rebuild_chain("A", Decimal("5"), [meas("m1", 10, "8")])
    ex = r.excursions[0]
    assert ex.is_open is True
    assert ex.end is None
    assert ex.segments[0].end is None
    assert ex.segments[0].party == "A"


def test_temp_equal_limit_does_not_open():
    r = rebuild_chain("A", Decimal("5"), [meas("m1", 10, "5"), meas("m2", 20, "4")])
    assert r.excursions == ()


def test_repeated_over_measurements_keep_one_interval():
    events = [meas("m1", 10, "8"), meas("m2", 20, "9"), meas("m3", 30, "4")]
    r = rebuild_chain("A", Decimal("5"), events)
    assert len(r.excursions) == 1
    assert r.excursions[0].start == T0 + timedelta(minutes=10)
    assert r.excursions[0].end == T0 + timedelta(minutes=30)


def test_multiple_handovers_split_into_more_segments():
    events = [
        meas("m1", 10, "8"),
        handover("h1", 20, "A", "B"),
        handover("h2", 30, "B", "C"),
        meas("m2", 40, "4"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    seg = r.excursions[0].segments
    assert [(s.start.minute, s.end and s.end.minute, s.party) for s in seg] == [
        (10, 20, "A"),
        (20, 30, "B"),
        (30, 40, "C"),
    ]


def test_handover_same_instant_as_open_measurement_receiver_liable():
    events = [
        meas("m1", 30, "8"),
        handover("h1", 30, "A", "B"),
        meas("m2", 40, "4"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    seg = r.excursions[0].segments
    assert [(s.start.minute, s.end.minute, s.party) for s in seg] == [(30, 40, "B")]


def test_close_same_instant_as_handover_no_zero_segment_for_giver():
    # 10:10 开启；10:30 交接 A->B，且同刻测温不超限关闭：交接时刻起归 B，
    # B 的片段为零长 -> 仅保留 A 的 [10:10,10:30)
    events = [
        meas("m1", 10, "8"),
        meas("m2", 30, "4"),
        handover("h1", 30, "A", "B"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    seg = r.excursions[0].segments
    assert [(s.start.minute, s.end.minute, s.party) for s in seg] == [(10, 30, "A")]
    assert r.current_holder == "B"


def test_two_separate_excursions():
    events = [
        meas("m1", 10, "8"),
        meas("m2", 20, "4"),
        handover("h1", 25, "A", "B"),
        meas("m3", 30, "9"),
        meas("m4", 40, "4"),
    ]
    r = rebuild_chain("A", Decimal("5"), events)
    assert len(r.excursions) == 2
    assert [s.party for s in r.excursions[1].segments] == ["B"]


def test_holder_at_with_handovers():
    events = [handover("h1", 30, "A", "B"), handover("h2", 60, "B", "C")]
    assert holder_at("A", events, T0) == "A"
    assert holder_at("A", events, T0 + timedelta(minutes=30)) == "B"
    assert holder_at("A", events, T0 + timedelta(minutes=45)) == "B"
    assert holder_at("A", events, T0 + timedelta(minutes=60)) == "C"


# ------------------------------------------------------- 车载读数合并


def window(loaded_min, unloaded_min=None, trip="V1", cargo="C1"):
    return TripCargo(
        trip_id=trip,
        cargo_id=cargo,
        loaded_at=T0 + timedelta(minutes=loaded_min),
        unloaded_at=(
            T0 + timedelta(minutes=unloaded_min) if unloaded_min is not None else None
        ),
    )


def vreading(rid, minute, temp, trip="V1"):
    return VehicleReading(
        reading_id=rid,
        trip_id=trip,
        occurred_at=T0 + timedelta(minutes=minute),
        temperature=Decimal(temp),
    )


def vpoints(windows, readings):
    return chain.applicable_vehicle_readings(windows, readings)


def test_vehicle_reading_applicable_only_while_on_board():
    wins = [window(0, 30)]
    pts = vpoints(wins, [vreading("r0", 0, 8), vreading("r1", 25, 9), vreading("r2", 30, 9)])
    # 左闭（00 含）右开（30 不含）
    assert [p.source_id for p in pts] == ["r0", "r1"]


def test_vehicle_reading_opens_and_closes_interval_with_stable_key():
    events = [handover("h1", 30, "A", "B")]
    pts = vpoints([window(0)], [vreading("r1", 10, 8), vreading("r2", 50, 2)])
    r = rebuild_chain("A", Decimal("5"), events, pts)
    ex = r.excursions[0]
    assert ex.opener_source == "vehicle"
    assert ex.start_reading_id == "r1"
    assert ex.start_event_id is None
    assert ex.closer_source == "vehicle"
    assert ex.end_reading_id == "r2"
    assert ex.interval_key == "vehicle:r1"
    assert [(s.party) for s in ex.segments] == ["A", "B"]


def test_own_measurement_shadows_vehicle_reading_at_same_instant():
    # 同刻：车载 9（越温）vs 自有 3（不越温）-> 采用自有，无区间
    pts = vpoints([window(0)], [vreading("r1", 10, 9)])
    r = rebuild_chain("A", Decimal("5"), [meas("m1", 10, 3)], pts)
    assert r.excursions == ()
    assert [p.source for p in r.timeline] == ["own"]
    assert [p.source_id for p in r.timeline] == ["m1"]


def test_vehicle_reading_per_cargo_limit_and_handover_ownership_only_own():
    # 同一车载读数对不同上限的货物结论不同（本测只建一单货物，用例覆盖上限行为）
    pts = vpoints([window(0)], [vreading("r1", 10, "4.5")])
    assert rebuild_chain("A", Decimal("4"), [], pts).excursions
    assert rebuild_chain("A", Decimal("6"), [], pts).excursions == ()
