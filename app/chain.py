"""保管链重建与越温责任拆分（纯函数，便于单测）。

规则
----
* 事件按 occurred_at 排序；同一时刻测温先于交接处理
  （“交接时刻起归接收方”——交接在该时刻已生效）。
* 高于上限的测温开启越温区间；越温期间首次不超限（<= 上限）的测温关闭区间。
* 区间跨交接时按交接时刻拆分：[区间起, 交接时刻) 归交出方，
  [交接时刻, ...) 归接收方；交接与区间起点同刻时直接由接收方担责。
* 区间内重复超限测温不影响区间；区间外不超限测温无影响。
* 不产生零长度片段。
"""
from dataclasses import dataclass
from decimal import Decimal

from app.models import Event, EventType


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
    start_event_id: str | None = None   # 开启区间的测温事件ID（区间标识）
    end_event_id: str | None = None     # 闭合区间的测温事件ID（开放区间为 None）

    @property
    def is_open(self) -> bool:
        return self.end is None


@dataclass(frozen=True)
class ChainResult:
    current_holder: str
    excursions: tuple[ExcursionResult, ...]


def _sorted_events(events: list[Event]) -> list[Event]:
    # 同一时刻：measurement 在 handover 之前（交接在测温“之后”生效）
    type_order = {EventType.measurement: 0, EventType.handover: 1}
    return sorted(
        events,
        key=lambda e: (e.occurred_at, type_order[e.event_type], e.event_id),
    )


def rebuild_chain(
    initial_holder: str,
    temp_upper_limit: Decimal,
    events: list[Event],
) -> ChainResult:
    """按事件流重建当前保管方与全部越温区间（含责任片段）。"""
    limit = Decimal(temp_upper_limit)
    holder = initial_holder

    open_start = None                  # 当前越温区间起点
    opener_id = None                   # 开启区间的测温事件ID
    segments: list[Segment] = []       # 当前区间已切分出的片段（最后一段开放）
    excursions: list[ExcursionResult] = []

    for e in _sorted_events(events):
        if e.event_type is EventType.measurement:
            over = Decimal(e.temperature) > limit
            if open_start is None and over:
                # 开启越温：当前保管方自测温时刻起担责
                open_start = e.occurred_at
                opener_id = e.event_id
                segments = [Segment(e.occurred_at, None, holder)]
            elif open_start is not None and not over:
                # 首次不超限测温 -> 在该时刻闭合
                last = segments[-1]
                if last.start < e.occurred_at:
                    segments[-1] = Segment(last.start, e.occurred_at, last.party)
                else:
                    segments.pop()  # 理论不会发生（同刻两笔测温已拒绝）
                excursions.append(
                    ExcursionResult(
                        open_start,
                        e.occurred_at,
                        tuple(segments),
                        start_event_id=opener_id,
                        end_event_id=e.event_id,
                    )
                )
                open_start = None
                opener_id = None
                segments = []
        else:  # handover
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
        excursions.append(
            ExcursionResult(
                open_start, None, tuple(segments), start_event_id=opener_id
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
