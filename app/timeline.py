"""货物追责时间线合并：货物自有事件 + 车载读数（按装载时段归属）。

合并规则
--------
* 货物的交接事件始终进入追责时间线，且**只有自有交接能改变保管方**。
* 货物的自有测温始终进入；同一时刻若存在自有测温，该时刻的车载读数
  **不进入追责时间线**（自有值优先），但车载原始读数仍可经车次接口查询。
* 车载读数仅对“读数时刻仍在车上”的货物生效：装载时段左闭右开，
  即 ``loaded_at <= read_at < unloaded_at``；读数时刻早于货物起始时间的不并入。
* 多票货物同车：每票货物按**各自的温度上限**独立判断越温，
  同一条车载原始读数可能同时被多票货物采用。

数据全部来自数据库当前状态，因此车载读数、装载时段、自有事件都允许晚到，
追责时间线与责任快照在每次查询 / 表态时整体重建。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.chain import (
    MeasurementPoint,
    SOURCE_ONBOARD,
    SOURCE_OWN,
    rebuild_chain,
)
from app.models import Cargo, CargoLoading, Event, EventType, OnboardReading
from app.timeutil import ensure_utc

# 车载读数未被某货物采用的原因（仅用于查询展示，不参与重建）
SKIP_OWN_PRIORITY = "own_measurement_priority"  # 同刻有货物自有测温
SKIP_NOT_ON_BOARD = "not_on_board"              # 读数时刻货物不在该车次上
SKIP_BEFORE_START = "before_cargo_start"        # 读数时刻早于货物起始时间


@dataclass(frozen=True)
class ReadingView:
    """货物合并测温时间线中的一条读数（含来源与是否采用）。"""

    occurred_at: object
    temperature: Decimal
    source: str                 # own / onboard
    adopted: bool               # 是否进入该货物的追责时间线
    event_id: str | None = None
    reading_id: str | None = None
    trip_id: str | None = None
    not_adopted_reason: str | None = None


@dataclass(frozen=True)
class CargoTimeline:
    """某货物合并后的完整视图：追责时间线 + 全部候选读数。"""

    timeline: tuple                     # Event（交接/自有测温）+ MeasurementPoint
    readings: tuple[ReadingView, ...]   # 全部测温候选（含未采用的车载读数）
    loadings: tuple[CargoLoading, ...]


def _utc(dt):
    return ensure_utc(dt)


def load_own_events(db, cargo_id: str) -> list[Event]:
    """读取货物的全部自有事件（测温 + 交接），时间归一化为 UTC。"""
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = _utc(e.occurred_at)
    return events


def load_loadings(db, cargo_id: str) -> list[CargoLoading]:
    """读取货物的全部装载时段（可能跨多个车次）。"""
    stmt = (
        select(CargoLoading)
        .where(CargoLoading.cargo_id == cargo_id)
        .order_by(CargoLoading.loaded_at, CargoLoading.trip_id)
    )
    rows = list(db.scalars(stmt).all())
    for r in rows:
        r.loaded_at = _utc(r.loaded_at)
        r.unloaded_at = _utc(r.unloaded_at)
    return rows


def load_onboard_for_loadings(db, loadings) -> dict[str, list[OnboardReading]]:
    """按装载时段涉及的车次，读取相关车次的全部车载原始读数。

    返回 {trip_id: [OnboardReading,...]}（按读数时刻升序）。
    """
    trip_ids = {row.trip_id for row in loadings}
    if not trip_ids:
        return {}
    stmt = (
        select(OnboardReading)
        .where(OnboardReading.trip_id.in_(trip_ids))
        .order_by(OnboardReading.read_at, OnboardReading.reading_id)
    )
    by_trip: dict[str, list[OnboardReading]] = {tid: [] for tid in trip_ids}
    for r in db.scalars(stmt).all():
        r.read_at = _utc(r.read_at)
        by_trip[r.trip_id].append(r)
    return by_trip


def _on_board(read_at, trip_id: str, windows) -> bool:
    """读数时刻货物是否在指定车次上（左闭右开，且装载时段须属于该车次）。"""
    return any(
        w.trip_id == trip_id and w.loaded_at <= read_at < w.unloaded_at
        for w in windows
    )


def build_cargo_timeline(db, cargo: Cargo) -> CargoTimeline:
    """合并货物自有事件与相关车次的车载读数，生成追责时间线与读数视图。"""
    events = load_own_events(db, cargo.cargo_id)
    loadings = load_loadings(db, cargo.cargo_id)
    onboard_by_trip = load_onboard_for_loadings(db, loadings)

    own_times = {
        e.occurred_at
        for e in events
        if e.event_type is EventType.measurement
    }
    started_at = _utc(cargo.started_at)

    timeline = list(events)
    readings: list[ReadingView] = [
        ReadingView(
            occurred_at=e.occurred_at,
            temperature=Decimal(e.temperature),
            source=SOURCE_OWN,
            adopted=True,
            event_id=e.event_id,
        )
        for e in events
        if e.event_type is EventType.measurement
    ]

    # 涉及车次的每条车载读数至多处理一次（同车多装载时段按覆盖判断即可）
    processed: set[str] = set()
    for w in loadings:
        for r in onboard_by_trip.get(w.trip_id, ()):
            if r.reading_id in processed:
                continue
            processed.add(r.reading_id)

            if not _on_board(r.read_at, w.trip_id, loadings):
                adopted = False
                reason = SKIP_NOT_ON_BOARD
                trip_for_view = w.trip_id
            elif r.read_at in own_times:
                # 同刻有货物自有测温：自有值优先，车载读数不并入但仍可查
                adopted = False
                reason = SKIP_OWN_PRIORITY
                trip_for_view = w.trip_id
            elif r.read_at < started_at:
                adopted = False
                reason = SKIP_BEFORE_START
                trip_for_view = w.trip_id
            else:
                adopted = True
                reason = None
                trip_for_view = w.trip_id
                timeline.append(
                    MeasurementPoint(
                        point_id=r.reading_id,
                        occurred_at=r.read_at,
                        temperature=Decimal(r.temperature),
                        source=SOURCE_ONBOARD,
                        trip_id=trip_for_view,
                    )
                )
            readings.append(
                ReadingView(
                    occurred_at=r.read_at,
                    temperature=Decimal(r.temperature),
                    source=SOURCE_ONBOARD,
                    adopted=adopted,
                    reading_id=r.reading_id,
                    trip_id=trip_for_view,
                    not_adopted_reason=reason,
                )
            )

    readings.sort(
        key=lambda r: (
            r.occurred_at,
            0 if r.source == SOURCE_OWN else 1,
            r.event_id or r.reading_id or "",
        )
    )
    return CargoTimeline(
        timeline=tuple(timeline),
        readings=tuple(readings),
        loadings=tuple(loadings),
    )


def rebuild_cargo_chain(db, cargo: Cargo):
    """便捷封装：合并时间线后重建保管链，返回 (ChainResult, CargoTimeline)。"""
    merged = build_cargo_timeline(db, cargo)
    result = rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), list(merged.timeline)
    )
    return result, merged
