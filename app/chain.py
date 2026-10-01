"""保管链重建与越温责任拆分（纯函数，便于单测）。

规则
----
* 货物的测温时间线由两类读数合并而成：
  - **货物自有测温事件**；
  - **车载读数**：读数时刻货物仍在车上
    （装载时段 ``[loaded_at, unloaded_at)``，未卸车时只要求 ``loaded_at <= t``）
    且按**该货物自身的温度上限**判断越温。
* 同一时刻两类读数都存在时，**货物自有测温优先采用**；被遮蔽的车载读数
  不参与该货物的时间线（车载原始读数仍可经车次接口查询）。
* 时间线按 occurred_at 排序；测温点先于交接处理，同一时刻测温先于车载
  （“交接时刻起归接收方”——交接在该时刻已生效）。
* 高于上限的测温开启越温区间；越温期间首次不超限（<= 上限）的测温关闭区间。
* 区间跨交接时按交接时刻拆分：[区间起, 交接时刻) 归交出方，
  [交接时刻, ...) 归接收方；交接与区间起点同刻时直接由接收方担责。
* 区间内重复超限测温不影响区间；区间外不超限测温无影响。
* **交接只由货物自有事件改变保管方**，车载读数不产生交接。
* 不产生零长度片段。
"""
from dataclasses import dataclass
from decimal import Decimal

from app.models import Event, EventType, TripCargo, VehicleReading

VEHICLE_INTERVAL_PREFIX = "vehicle:"


@dataclass(frozen=True)
class Segment:
    start: object          # datetime，含
    end: object | None     # datetime，不含；None 表示未闭合
    party: str


@dataclass(frozen=True)
class TempPoint:
    """合并后测温时间线上的一个读数点。"""

    occurred_at: object
    temperature: Decimal
    # own = 货物自有测温；vehicle = 车载读数
    source: str
    # source == "own" 时为事件ID；"vehicle" 时为车载读数ID
    source_id: str
    trip_id: str | None = None

    @property
    def event_id(self) -> str | None:
        return self.source_id if self.source == "own" else None

    @property
    def reading_id(self) -> str | None:
        return self.source_id if self.source == "vehicle" else None


@dataclass(frozen=True)
class ExcursionResult:
    start: object
    end: object | None
    segments: tuple[Segment, ...]
    # 开启 / 闭合区间的读数来源（自有事件ID 与 车载读数ID 互斥，至多各一个）
    start_event_id: str | None = None
    start_reading_id: str | None = None
    end_event_id: str | None = None
    end_reading_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.end is None

    @property
    def interval_key(self) -> str:
        """区间的稳定标识：自有测温开启为事件ID，车载读数开启为 vehicle:<读数ID>。"""
        if self.start_reading_id is not None:
            return f"{VEHICLE_INTERVAL_PREFIX}{self.start_reading_id}"
        return self.start_event_id

    @property
    def opener_source(self) -> str:
        return "vehicle" if self.start_reading_id is not None else "own"

    @property
    def closer_source(self) -> str | None:
        if self.end is None:
            return None
        return "vehicle" if self.end_reading_id is not None else "own"


@dataclass(frozen=True)
class ChainResult:
    current_holder: str
    excursions: tuple[ExcursionResult, ...]
    timeline: tuple[TempPoint, ...] = ()


def _sorted_events(events: list[Event]) -> list[Event]:
    # 同一时刻：measurement 在 handover 之前（交接在测温“之后”生效）
    type_order = {EventType.measurement: 0, EventType.handover: 1}
    return sorted(
        events,
        key=lambda e: (e.occurred_at, type_order[e.event_type], e.event_id),
    )


def cargo_on_board(window: TripCargo, at) -> bool:
    """读数时刻 at 货物是否仍在车上：装载时段左闭右开 [loaded, unloaded)。"""
    if at < window.loaded_at:
        return False
    if window.unloaded_at is not None and at >= window.unloaded_at:
        return False
    return True


def applicable_vehicle_readings(
    windows: list[TripCargo],
    readings: list[VehicleReading],
) -> list[TempPoint]:
    """挑出对某票货物生效的车载读数（读数时刻仍在车上），返回测温点。

    同货物的装载时段互不重叠，故同一车次同一时刻不会有两个窗口同时命中。
    """
    points: list[TempPoint] = []
    for r in readings:
        for w in windows:
            if w.trip_id == r.trip_id and cargo_on_board(w, r.occurred_at):
                points.append(
                    TempPoint(
                        occurred_at=r.occurred_at,
                        temperature=Decimal(r.temperature),
                        source="vehicle",
                        source_id=r.reading_id,
                        trip_id=r.trip_id,
                    )
                )
                break
    return points


def merge_timeline(
    events: list[Event],
    vehicle_points: list[TempPoint] | None = None,
) -> tuple[TempPoint, ...]:
    """合并货物自有测温与生效车载读数。

    同刻有货物自有测温时自有优先，该时刻的车载点被遮蔽（不进时间线）。
    结果按 (时刻, 来源顺序[自有先], 来源ID) 升序。
    """
    own: dict[object, TempPoint] = {}
    for e in _sorted_events(events):
        if e.event_type is EventType.measurement:
            own[e.occurred_at] = TempPoint(
                occurred_at=e.occurred_at,
                temperature=Decimal(e.temperature),
                source="own",
                source_id=e.event_id,
            )

    points = list(own.values())
    for p in vehicle_points or ():
        if p.occurred_at not in own:  # 同刻自有测温优先
            points.append(p)

    source_order = {"own": 0, "vehicle": 1}
    points.sort(key=lambda p: (p.occurred_at, source_order[p.source], p.source_id))
    return tuple(points)


def rebuild_chain(
    initial_holder: str,
    temp_upper_limit: Decimal,
    events: list[Event],
    vehicle_points: list[TempPoint] | None = None,
) -> ChainResult:
    """按事件流 + 生效车载读数重建当前保管方与全部越温区间（含责任片段）。"""
    limit = Decimal(temp_upper_limit)
    holder = initial_holder
    timeline = merge_timeline(events, vehicle_points)

    # 交接只来自货物自有事件，按时间升序（同刻测温先于交接已由排序保证）
    handovers = [
        e for e in _sorted_events(events) if e.event_type is EventType.handover
    ]

    open_start = None                  # 当前越温区间起点
    opener = None                      # 开启区间的测温点
    segments: list[Segment] = []       # 当前区间已切分出的片段（最后一段开放）
    excursions: list[ExcursionResult] = []

    def close_interval(at, closer: TempPoint):
        nonlocal open_start, opener, segments
        last = segments[-1]
        if last.start < at:
            segments[-1] = Segment(last.start, at, last.party)
        else:
            segments.pop()  # 理论不会发生（同刻读数自有优先已去重）
        excursions.append(
            ExcursionResult(
                open_start,
                at,
                tuple(segments),
                start_event_id=opener.event_id if opener is not None else None,
                start_reading_id=opener.reading_id if opener is not None else None,
                end_event_id=closer.event_id,
                end_reading_id=closer.reading_id,
            )
        )
        open_start = None
        opener = None
        segments = []

    hi = 0
    for point in timeline:
        # 先应用严格早于该测温时刻的交接
        while hi < len(handovers) and handovers[hi].occurred_at < point.occurred_at:
            e = handovers[hi]
            if open_start is not None:
                prev = segments[-1]
                # 交接时刻严格晚于区间起点（同刻交接在测温点之后单独处理）
                segments[-1] = Segment(prev.start, e.occurred_at, prev.party)
                segments.append(Segment(e.occurred_at, None, e.receiver))
            holder = e.receiver
            hi += 1

        over = point.temperature > limit
        if open_start is None and over:
            # 开启越温：当前保管方自读数时刻起担责
            open_start = point.occurred_at
            opener = point
            segments = [Segment(point.occurred_at, None, holder)]
        elif open_start is not None and not over:
            # 首次不超限读数 -> 在该时刻闭合
            close_interval(point.occurred_at, point)

        # 与测温同刻的交接：测温先处理，交接在该时刻生效
        while hi < len(handovers) and handovers[hi].occurred_at == point.occurred_at:
            e = handovers[hi]
            if open_start is not None:
                prev = segments[-1]
                if e.occurred_at > prev.start:
                    # 交出方承担 [prev.start, 交接时刻)，接收方自交接时刻起
                    segments[-1] = Segment(prev.start, e.occurred_at, prev.party)
                    segments.append(Segment(e.occurred_at, None, e.receiver))
                else:
                    # 交接与区间起点（同刻的开启测温）同一时刻：
                    # 交出方片段为零长，直接由接收方自该时刻起担责
                    segments[-1] = Segment(e.occurred_at, None, e.receiver)
            holder = e.receiver
            hi += 1

    # 测温时间线之后的交接
    while hi < len(handovers):
        e = handovers[hi]
        if open_start is not None:
            prev = segments[-1]
            segments[-1] = Segment(prev.start, e.occurred_at, prev.party)
            segments.append(Segment(e.occurred_at, None, e.receiver))
        holder = e.receiver
        hi += 1

    if open_start is not None:
        # 未闭合区间：最后片段保持开放终点，标记“未结束”
        excursions.append(
            ExcursionResult(
                open_start,
                None,
                tuple(segments),
                start_event_id=opener.event_id if opener is not None else None,
                start_reading_id=opener.reading_id if opener is not None else None,
            )
        )

    return ChainResult(
        current_holder=holder, excursions=tuple(excursions), timeline=timeline
    )


def holder_at(initial_holder: str, events: list[Event], at) -> str:
    """返回 at 时刻（该时刻的交接已生效后）的保管方。

    用于写入校验：交接的交出方必须等于“交接时刻的保管方”。
    """
    holder = initial_holder
    for e in _sorted_events(events):
        if e.occurred_at <= at and e.event_type is EventType.handover:
            holder = e.receiver
    return holder
