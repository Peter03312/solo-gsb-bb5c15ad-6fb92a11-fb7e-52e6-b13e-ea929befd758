"""保管链重建与越温责任拆分（纯函数，便于单测）。

规则
----
* 事件按 occurred_at 排序；同一时刻测温先于交接处理
  （“交接时刻起归接收方”——交接在该时刻已生效）。
* 测温点有两种来源（``source``）：
  - ``own``：货物自有测温事件（event_id 标识）；
  - ``onboard``：车载原始读数（reading_id + trip_id 标识），仅在读数时刻
    货物仍在该车次上时才并入（见 app/timeline.py 的装载时段合并）。
  同一时刻存在自有测温时，车载读数不并入（自有值优先）。
* 高于上限的测温开启越温区间；越温期间首次不超限（<= 上限）的测温关闭区间。
* 区间跨交接时按交接时刻拆分：[区间起, 交接时刻) 归交出方，
  [交接时刻, ...) 归接收方；交接与区间起点同刻时直接由接收方担责。
* 区间内重复超限测温不影响区间；区间外不超限测温无影响。
* 不产生零长度片段。
* 交接只由货物自有交接事件改变保管方：车载读数永不改变保管方。
"""
from dataclasses import dataclass
from decimal import Decimal

from app.models import Event, EventType

# 测温来源
SOURCE_OWN = "own"            # 货物自有测温
SOURCE_ONBOARD = "onboard"    # 车载测温


@dataclass(frozen=True)
class MeasurementPoint:
    """时间线上的一笔测温（自有事件或车载读数）。

    ``point_id`` 为该测温的稳定标识：自有=事件ID，车载=读数ID。
    以车载读数开启的越温区间，其区间标识为 ``onboard:<reading_id>``，
    与自有事件ID空间彻底区分，便于查询与责任表态。
    """

    point_id: str
    occurred_at: object
    temperature: Decimal
    source: str = SOURCE_OWN
    trip_id: str | None = None

    @property
    def event_type(self):
        # 与 Event 同构，使测温/交接可共用同一排序与处理逻辑
        return EventType.measurement

    @property
    def event_id(self) -> str:
        return self.point_id

    @property
    def opener_id(self) -> str:
        """以该测温开启越温区间时的区间稳定标识。"""
        return interval_opener_id(self.source, self.point_id)


def interval_opener_id(source: str, point_id: str) -> str:
    """车载读数开启的区间加 ``onboard:`` 前缀；自有测温直接用事件ID。"""
    return f"onboard:{point_id}" if source == SOURCE_ONBOARD else point_id


@dataclass(frozen=True)
class Segment:
    start: object          # datetime，含
    end: object | None     # datetime，不含；None 表示未闭合
    party: str


@dataclass(frozen=True)
class ExcursionResult:
    start: object
    end: object | None
    segments: tuple[Segment, ...]
    start_event_id: str | None = None   # 区间标识（自有事件ID 或 onboard:读数ID）
    end_event_id: str | None = None     # 闭合测温标识（开放区间为 None）
    # 开启 / 闭合测温的来源元数据
    start_source: str = SOURCE_OWN
    end_source: str | None = None
    start_point_id: str | None = None   # 去掉 onboard: 前缀的原始测温标识
    end_point_id: str | None = None
    start_trip_id: str | None = None
    end_trip_id: str | None = None

    @property
    def is_open(self) -> bool:
        return self.end is None


@dataclass(frozen=True)
class ChainResult:
    current_holder: str
    excursions: tuple[ExcursionResult, ...]


def _is_measurement(item) -> bool:
    return item.event_type is EventType.measurement


def _sorted(items) -> list:
    """测温/交接混合时间线排序：同刻测温先于交接，再按稳定标识去歧义。"""
    type_order = {EventType.measurement: 0, EventType.handover: 1}
    return sorted(
        items,
        key=lambda e: (
            e.occurred_at,
            type_order[e.event_type],
            getattr(e, "event_id", None) or getattr(e, "point_id", ""),
        ),
    )


def _sorted_events(events: list[Event]) -> list[Event]:
    """向后兼容：仅自有事件时的排序（测试 / 校验逻辑使用）。"""
    return _sorted(events)


def _point_meta(item):
    """返回 (source, raw_point_id, trip_id)。Event 为自有测温。"""
    if isinstance(item, MeasurementPoint):
        return item.source, item.point_id, item.trip_id
    return SOURCE_OWN, item.event_id, None


def rebuild_chain(
    initial_holder: str,
    temp_upper_limit: Decimal,
    events,
) -> ChainResult:
    """按测温/交接时间线重建当前保管方与全部越温区间（含责任片段）。

    ``events`` 可为：
    * 仅 ``Event`` 列表（历史调用，全部视为货物自有事件）；
    * ``Event``（交接/自有测温）与 ``MeasurementPoint``（车载读数）的混合列表
      （由 app/timeline.py 合并后的追责时间线）。
    """
    limit = Decimal(temp_upper_limit)
    holder = initial_holder

    open_start = None                  # 当前越温区间起点
    opener = None                      # 开启测温点
    segments: list[Segment] = []       # 当前区间已切分出的片段（最后一段开放）
    excursions: list[ExcursionResult] = []

    for e in _sorted(events):
        if _is_measurement(e):
            temp = Decimal(e.temperature)
            over = temp > limit
            if open_start is None and over:
                # 开启越温：当前保管方自测温时刻起担责
                open_start = e.occurred_at
                opener = e
                segments = [Segment(e.occurred_at, None, holder)]
            elif open_start is not None and not over:
                # 首次不超限测温 -> 在该时刻闭合
                last = segments[-1]
                if last.start < e.occurred_at:
                    segments[-1] = Segment(last.start, e.occurred_at, last.party)
                else:
                    segments.pop()  # 理论不会发生（同刻两笔测温已拒绝）
                o_src, o_pid, o_trip = _point_meta(opener)
                c_src, c_pid, c_trip = _point_meta(e)
                excursions.append(
                    ExcursionResult(
                        open_start,
                        e.occurred_at,
                        tuple(segments),
                        start_event_id=interval_opener_id(o_src, o_pid),
                        end_event_id=interval_opener_id(c_src, c_pid),
                        start_source=o_src,
                        end_source=c_src,
                        start_point_id=o_pid,
                        end_point_id=c_pid,
                        start_trip_id=o_trip,
                        end_trip_id=c_trip,
                    )
                )
                open_start = None
                opener = None
                segments = []
        else:  # handover（仅货物自有交接事件）
            if open_start is not None:
                # 跨交接拆分责任片段
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

    if open_start is not None:
        # 未闭合区间：最后片段保持开放终点，标记“未结束”
        o_src, o_pid, o_trip = _point_meta(opener)
        excursions.append(
            ExcursionResult(
                open_start,
                None,
                tuple(segments),
                start_event_id=interval_opener_id(o_src, o_pid),
                start_source=o_src,
                start_point_id=o_pid,
                start_trip_id=o_trip,
            )
        )

    return ChainResult(current_holder=holder, excursions=tuple(excursions))


def holder_at(initial_holder: str, events: list[Event], at) -> str:
    """返回 at 时刻（该时刻的交接已生效后）的保管方。

    用于写入校验：交接的交出方必须等于“交接时刻的保管方”。
    """
    holder = initial_holder
    for e in _sorted_events(events):
        if e.occurred_at <= at and e.event_type is EventType.handover:
            holder = e.receiver
    return holder
