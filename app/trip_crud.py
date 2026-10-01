"""车次 / 装载时段 / 车载读数的写入逻辑与一致性校验。

事务与串行化
------------
* **登记车次**：独立插入，编号冲突返回 409（不涉及货物行锁）。
* **登记装载时段**（可批量多票货物）：
  1. 按货物编号排序依次 ``SELECT ... FOR UPDATE`` 锁定全部涉及货物行
     （固定加锁顺序，杜绝死锁）——与该货物的事件写入、责任表态、
     车载读数写入互斥；
  2. 车次与货物必须存在；时段必须左闭右开（loaded_at < unloaded_at）；
  3. 同货物的新旧时段两两不得相交（首尾相接允许）；批量内部也不得自相交；
  4. 任一校验失败整体回滚，不留部分记录。
* **写入车载读数**：
  1. 先锁车次行，再按货物编号排序锁定“读数时刻仍在车上”的货物行
     ——车载读数写入与这些货物的事件写入、责任表态、装载时段写入严格串行；
  2. 编号同内容重传幂等、异内容冲突；同车次同刻读数冲突；
  3. 允许晚到：读数是否被各货物采用、对越温区间的影响一律在查询 / 表态时
     按合并后的追责时间线重建（见 app/timeline.py）。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.crud import ApiError, _conflict
from app.models import Cargo, CargoLoading, OnboardReading, Trip
from app.schemas import LoadingEntry, OnboardReadingCreate, TripCreate
from app.timeutil import ensure_utc


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


# ----------------------------------------------------------- 车次


def create_trip(db, payload: TripCreate) -> Trip:
    existing = db.get(Trip, payload.trip_id)
    if existing is not None:
        raise _conflict(f"车次编号 {payload.trip_id} 已存在", code="trip_exists")
    trip = Trip(
        trip_id=payload.trip_id,
        vehicle_no=payload.vehicle_no,
        note=payload.note,
    )
    db.add(trip)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict(f"车次编号 {payload.trip_id} 已存在", code="trip_exists")
    return trip


# ----------------------------------------------------------- 装载时段


def _load_cargos_locked_ordered(db, cargo_ids: list[str]) -> dict[str, Cargo]:
    """按编号排序锁定货物行（固定加锁顺序，杜绝死锁）。"""
    cargos: dict[str, Cargo] = {}
    for cid in sorted(set(cargo_ids)):
        stmt = select(Cargo).where(Cargo.cargo_id == cid).with_for_update()
        cargo = db.scalars(stmt).first()
        if cargo is None:
            raise ApiError(404, "cargo_not_found", f"货物 {cid} 不存在")
        cargo.started_at = ensure_utc(cargo.started_at)
        cargos[cid] = cargo
    return cargos


def _windows_overlap(a_start, a_end, b_start, b_end) -> bool:
    """半开区间相交判定：首尾相接（a_end == b_start）不算相交。"""
    return a_start < b_end and b_start < a_end


def register_loadings(db, trip_id: str, entries: list[LoadingEntry]) -> list[CargoLoading]:
    try:
        return _register_loadings_tx(db, trip_id, entries)
    except OperationalError:
        # SQLite 文件库无行级锁，busy timeout 耗尽时整体重试一次
        db.rollback()
        return _register_loadings_tx(db, trip_id, entries)


def _register_loadings_tx(db, trip_id: str, entries: list[LoadingEntry]) -> list[CargoLoading]:
    try:
        # 先锁车次行：与同车次的车载读数写入互斥（同时保证车次存在）。
        # 加锁顺序恒为「车次行 -> 按编号排序的货物行」，与读数写入一致，杜绝死锁。
        _load_trip_locked(db, trip_id)

        # 锁全部涉及货物行（固定排序）
        cargo_ids = [e.cargo_id for e in entries]
        cargos = _load_cargos_locked_ordered(db, cargo_ids)

        # 规范化 + 形状校验：左闭右开
        normalized: dict[str, tuple] = {}
        for e in entries:
            start = ensure_utc(e.loaded_at)
            end = ensure_utc(e.unloaded_at)
            if not (start < end):
                raise _unprocessable(
                    f"货物 {e.cargo_id} 的装载时段无效：起点 {start.isoformat()} "
                    f"必须严格早于终点 {end.isoformat()}（时段左闭右开）",
                    code="invalid_loading_window",
                )
            normalized[e.cargo_id] = (start, end)

        if len(normalized) != len(entries):
            raise _conflict(
                "同一批次中同一货物出现多次装载登记，请合并为一条",
                code="loading_overlap",
            )

        # 既有装载时段（同货物，跨全部车次）
        existing = list(
            db.scalars(
                select(CargoLoading).where(
                    CargoLoading.cargo_id.in_(list(normalized))
                )
            ).all()
        )
        for row in existing:
            row.loaded_at = ensure_utc(row.loaded_at)
            row.unloaded_at = ensure_utc(row.unloaded_at)

        # 同车次同货物唯一
        existing_keys = {(row.trip_id, row.cargo_id) for row in existing}
        for cid in normalized:
            if (trip_id, cid) in existing_keys:
                raise _conflict(
                    f"货物 {cid} 在车次 {trip_id} 上已登记装载时段",
                    code="loading_overlap",
                )

        # 与既有任何时段相交 -> 同货物同时属于两个车次（或与同车既有时段重叠）
        for row in existing:
            start, end = normalized[row.cargo_id]
            if _windows_overlap(start, end, row.loaded_at, row.unloaded_at):
                raise _conflict(
                    f"货物 {row.cargo_id} 的装载时段 [{start.isoformat()}, "
                    f"{end.isoformat()}) 与车次 {row.trip_id} 上的既有时段 "
                    f"[{row.loaded_at.isoformat()}, {row.unloaded_at.isoformat()}) "
                    "相交：同一货物不得同时属于两个车次",
                    code="loading_overlap",
                )

        # 批量内同一货物已在上面以 len(normalized) 校验过；不同货物时段互不影响。

        rows = [
            CargoLoading(
                trip_id=trip_id,
                cargo_id=cid,
                loaded_at=start,
                unloaded_at=end,
            )
            for cid, (start, end) in normalized.items()
        ]
        db.add_all(rows)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict(
            "装载登记与既有数据冲突（车次/货物重复或时段重叠）",
            code="integrity_conflict",
        )
    # 按装载起点稳定排序返回
    rows.sort(key=lambda r: (r.loaded_at, r.cargo_id))
    for r in rows:
        r.loaded_at = ensure_utc(r.loaded_at)
        r.unloaded_at = ensure_utc(r.unloaded_at)
    return rows


# ----------------------------------------------------------- 车载读数


@dataclass
class ReadingWriteResult:
    reading: OnboardReading
    replayed: bool


def _same_reading_content(existing: OnboardReading, candidate: OnboardReading) -> bool:
    return (
        existing.trip_id == candidate.trip_id
        and ensure_utc(existing.read_at) == candidate.read_at
        and existing.temperature == candidate.temperature
    )


def _load_trip_locked(db, trip_id: str) -> Trip:
    stmt = select(Trip).where(Trip.trip_id == trip_id).with_for_update()
    trip = db.scalars(stmt).first()
    if trip is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return trip


def _affected_cargo_ids(db, trip_id: str, read_at) -> list[str]:
    """读数时刻仍在该车次上的货物（左闭右开）。"""
    stmt = select(CargoLoading).where(CargoLoading.trip_id == trip_id)
    rows = db.scalars(stmt).all()
    ids = []
    for row in rows:
        if ensure_utc(row.loaded_at) <= read_at < ensure_utc(row.unloaded_at):
            ids.append(row.cargo_id)
    return ids


def create_reading(db, payload: OnboardReadingCreate) -> ReadingWriteResult:
    read_at = ensure_utc(payload.read_at)
    candidate = OnboardReading(
        reading_id=payload.reading_id,
        trip_id=payload.trip_id,
        read_at=read_at,
        temperature=payload.temperature,
    )
    try:
        return _create_reading_tx(db, candidate)
    except OperationalError:
        db.rollback()
        return _create_reading_tx(db, candidate)


def _create_reading_tx(db, candidate: OnboardReading) -> ReadingWriteResult:
    try:
        # 先锁车次行：串行化同车次读数写入（同时保证车次存在）
        _load_trip_locked(db, candidate.trip_id)

        # 幂等优先：读数编号全局唯一
        existing = db.get(OnboardReading, candidate.reading_id)
        if existing is not None:
            if _same_reading_content(existing, candidate):
                db.rollback()  # 仅释放锁，未修改
                existing.read_at = ensure_utc(existing.read_at)
                return ReadingWriteResult(reading=existing, replayed=True)
            raise _conflict(
                f"车载读数编号 {candidate.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )

        # 同车次同刻至多一笔原始读数
        clash = db.scalars(
            select(OnboardReading).where(
                OnboardReading.trip_id == candidate.trip_id,
                OnboardReading.read_at == candidate.read_at,
            )
        ).first()
        if clash is not None:
            raise _conflict(
                f"车次 {candidate.trip_id} 在 {candidate.read_at.isoformat()} "
                f"已存在车载读数 {clash.reading_id}，同一时刻不允许重复",
                code="same_timestamp_conflict",
            )

        # 锁“读数时刻仍在车上”的货物行（固定排序）：
        # 车载读数写入与这些货物的事件 / 表态 / 装载时段写入串行一致。
        # 没有受影响货物（读数时刻空车）时无需货物锁，读数仍作为原始数据保存。
        affected = _affected_cargo_ids(db, candidate.trip_id, candidate.read_at)
        _load_cargos_locked_ordered(db, affected)

        db.add(candidate)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        # 并发兜底：同编号 / 同车次同刻
        winner = db.get(OnboardReading, candidate.reading_id)
        if winner is not None and _same_reading_content(winner, candidate):
            winner.read_at = ensure_utc(winner.read_at)
            return ReadingWriteResult(reading=winner, replayed=True)
        if winner is not None:
            raise _conflict(
                f"车载读数编号 {candidate.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )
        raise _conflict(
            f"车次 {candidate.trip_id} 的车载读数与既有数据冲突"
            "（同刻重复 / 编号冲突 / 字段约束）",
            code="integrity_conflict",
        )

    return ReadingWriteResult(reading=candidate, replayed=False)
