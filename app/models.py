"""SQLAlchemy ORM 模型：货物与冷链事件（测温 / 交接）。"""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventType(str, enum.Enum):
    """事件类型。成员名与 PG enum 字面量保持一致。"""

    measurement = "measurement"  # 测温
    handover = "handover"        # 交接


class StanceKind(str, enum.Enum):
    """责任方表态类型。"""

    confirm = "confirm"  # 确认担责
    dispute = "dispute"  # 异议（须附理由）


class Cargo(Base):
    __tablename__ = "cargo"

    cargo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temp_upper_limit: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False
    )
    initial_holder: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Event(Base):
    """一条测温或交接事件。

    测温：temperature 有值，giver/receiver 为空。
    交接：giver/receiver 有值，temperature 为空。
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "(event_type = 'measurement' AND temperature IS NOT NULL "
            "AND giver IS NULL AND receiver IS NULL) "
            "OR (event_type = 'handover' AND temperature IS NULL "
            "AND giver IS NOT NULL AND receiver IS NOT NULL AND giver <> receiver)",
            name="chk_event_shape",
        ),
        # 同一货物同一时刻只能有一笔测温（跨类型不互斥：测温与交接可同刻发生）
        Index(
            "ux_event_cargo_time_measurement",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'measurement'::event_type"),
            sqlite_where=text("event_type = 'measurement'"),
        ),
        # 同一货物同一时刻只能有一笔交接
        Index(
            "ux_event_cargo_time_handover",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'handover'::event_type"),
            sqlite_where=text("event_type = 'handover'"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type", create_constraint=True),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    giver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Trip(Base):
    """一次冷链车次。同一车次可承载多票货物（见 TripCargo）。"""

    __tablename__ = "trip"

    trip_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    truck_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class TripCargo(Base):
    """货物在某车次上的装载时段：[loaded_at, unloaded_at)，左闭右开。

    unloaded_at 为 NULL 表示仍在车上。同一货物的任意两个装载时段
    （可跨车次）不得重叠——由写入事务在货物行锁下串行保证。
    """

    __tablename__ = "trip_cargo"
    __table_args__ = (
        CheckConstraint(
            "unloaded_at IS NULL OR loaded_at < unloaded_at",
            name="chk_loading_window",
        ),
        Index("ix_trip_cargo_cargo", "cargo_id", "loaded_at"),
    )

    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    unloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class VehicleReading(Base):
    """车载测温设备的一笔原始读数（属于某车次）。

    允许晚到（乱序写入）。原始读数永久保留；是否被某票货物“采用”
    取决于读数时刻该货物是否仍在车上、以及同刻是否有货物自有测温
    （自有优先）——在查询/重建时计算，不落在本表。
    """

    __tablename__ = "vehicle_reading"
    __table_args__ = (
        # 同一车次同一时刻至多一笔车载读数
        Index(
            "ux_reading_trip_time",
            "trip_id",
            "occurred_at",
            unique=True,
        ),
        Index("ix_reading_trip", "trip_id", "occurred_at"),
    )

    reading_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trip_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("trip.trip_id", ondelete="RESTRICT"),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Stance(Base):
    """责任方对一个已闭合越温区间的一次表态（确认 / 异议）。

    表态是只追加（append-only）的：改口产生新编号的新记录，历次表态永久保留；
    晚到事件 / 晚到车载读数重建后若区间边界或责任片段变化，旧行不删除、不修改，
    其是否仍“有效”由查询时与当前快照比对得出（见 app/responsibility.py）。

    区间标识 ``interval_key`` 稳定可查：
    * 货物自有测温开启：``interval_key = 开启测温事件ID``；
    * 车载读数开启：    ``interval_key = "vehicle:" + 车载读数ID``。
    因此不对外键做硬约束（开启来源可能是事件或车载读数）。
    """

    __tablename__ = "responsibility_stance"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'confirm' AND reason IS NULL) "
            "OR (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)",
            name="chk_stance_shape",
        ),
        Index("ix_stance_cargo_interval", "cargo_id", "interval_key"),
    )

    stance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 区间标识：开启测温事件ID，或 "vehicle:<车载读数ID>"
    interval_key: Mapped[str] = mapped_column(String(78), nullable=False)
    responsible_party: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[StanceKind] = mapped_column(
        Enum(StanceKind, name="stance_kind", create_constraint=True),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 提交时校验通过的快照标识；区间/片段变化后该标识与当前快照不再一致
    snapshot_id: Mapped[str] = mapped_column(String(71), nullable=False)
    # 表态时该责任方承担的片段坐标（规范化 JSON：[[start,end,party],...]）。
    # 晚到事件 / 车载读数重建后据此逐片段判定表态是否仍有效（未变化片段继续有效）。
    attested_segments: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
