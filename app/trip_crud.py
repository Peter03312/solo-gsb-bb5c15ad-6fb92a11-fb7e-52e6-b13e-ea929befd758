"""车次、装载时段与车载读数的写入逻辑、一致性校验和查询辅助。

写入都在单事务内完成，并与货物自有事件写入 / 责任表态共用同一套
“货物行锁”（``SELECT ... FOR UPDATE``）以保证串行一致：

* 登记车次（含一批货物的装载时段）：先锁车次键（插入），再按货物编号
  排序锁定全部相关货物行，随后校验时段合法、同货物时段不重叠
  （含与既有窗口的重叠、批内自重叠）；
* 补录装载 / 卸车：锁车次行 + 货物行，做同样的时段 / 重叠校验；
* 车载读数：锁车次行与读数时刻在车上的货物行（按编号排序），
  使读数写入与这些货物的事件写入、表态提交互斥。

任一校验失败即整体回滚——不留下车次、窗口或读数的部分记录。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.crud import ApiError, _conflict, _unprocessable
from app.models import Cargo, Trip, TripCargo, VehicleReading
from app.timeutil import ensure_utc


# ----------------------------------------------------------- 锁 / 读辅助


def _lock_cargo_rows(db, cargo_ids: list[str], *, must_exist: bool = True) -> dict[str, Cargo]:
    """按货物编号排序锁定货物行（固定加锁顺序，避免死锁）。"""
    out: dict[str, Cargo] = {}
    for cid in sorted(set(cargo_ids)):
        stmt = select(Cargo).where(Cargo.cargo_id == cid).with_for_update()
        cargo = db.scalars(stmt).first()
        if cargo is None:
            if must_exist:
                raise ApiError(404, "cargo_not_found", f"货物 {cid} 不存在")
            continue
        cargo.started_at = ensure_utc(cargo.started_at)
        out[cid] = cargo
    return out


def _lock_trip(db, trip_id: str) -> Trip:
    stmt = select(Trip).where(Trip.trip_id == trip_id).with_for_update()
    trip = db.scalars(stmt).first()
    if trip is None:
        raise ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return trip


def load_windows(db, cargo_id: str, *, lock: bool = False) -> list[TripCargo]:
    stmt = select(TripCargo).where(TripCargo.cargo_id == cargo_id)
    if lock:
        stmt = stmt.with_for_update()
    rows = list(db.scalars(stmt).all())
    for w in rows:
        w.loaded_at = ensure_utc(w.loaded_at)
        if w.unloaded_at is not None:
            w.unloaded_at = ensure_utc(w.unloaded_at)
    return rows


def load_trip_windows(db, trip_id: str) -> list[TripCargo]:
    stmt = select(TripCargo).where(TripCargo.trip_id == trip_id).order_by(
        TripCargo.cargo_id, TripCargo.loaded_at
    )
    rows = list(db.scalars(stmt).all())
    for w in rows:
        w.loaded_at = ensure_utc(w.loaded_at)
        if w.unloaded_at is not None:
            w.unloaded_at = ensure_utc(w.unloaded_at)
    return rows


def load_readings(
    db,
    trip_id: str | None = None,
    cargo_id: str | None = None,
) -> list[VehicleReading]:
    """读取车载读数（可按车次；给货物时间线用时配合窗口在内存中过滤）。"""
    stmt = select(VehicleReading)
    if trip_id is not None:
        stmt = stmt.where(VehicleReading.trip_id == trip_id)
    if cargo_id is not None:
        stmt = stmt.join(
            TripCargo, TripCargo.trip_id == VehicleReading.trip_id
        ).where(TripCargo.cargo_id == cargo_id)
    stmt = stmt.order_by(VehicleReading.occurred_at, VehicleReading.reading_id)
    rows = list(db.scalars(stmt).all())
    for r in rows:
        r.occurred_at = ensure_utc(r.occurred_at)
    return rows


def load_cargo_context(db, cargo_id: str):
    """读货物 + 自有事件 + 装载窗口 + 相关车次的全部读数（供重建）。"""
    from app.crud import _load_events

    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    events = _load_events(db, cargo_id)
    windows = load_windows(db, cargo_id)
    trip_ids = {w.trip_id for w in windows}
    readings: list[VehicleReading] = []
    if trip_ids:
        stmt = (
            select(VehicleReading)
            .where(VehicleReading.trip_id.in_(trip_ids))
            .order_by(VehicleReading.occurred_at, VehicleReading.reading_id)
        )
        readings = list(db.scalars(stmt).all())
        for r in readings:
            r.occurred_at = ensure_utc(r.occurred_at)
    return cargo, events, windows, readings


# ----------------------------------------------------------- 时段校验


def _validate_window(cargo: Cargo, loaded_at, unloaded_at) -> None:
    """单个装载时段合法性：左闭右开且起点早于终点；不早于货物起始。"""
    if unloaded_at is not None and not (loaded_at < unloaded_at):
        raise _unprocessable(
            f"货物 {cargo.cargo_id} 的装载时段非法：装载 {loaded_at.isoformat()} "
            f"必须早于卸车 {unloaded_at.isoformat()}（时段左闭右开，不允许空时段）",
            code="invalid_loading_window",
        )
    if loaded_at < cargo.started_at:
        raise _unprocessable(
            f"货物 {cargo.cargo_id} 的装载时刻 {loaded_at.isoformat()} "
            f"早于货物起始时间 {cargo.started_at.isoformat()}",
            code="loading_before_cargo_start",
        )


def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    """两个左闭右开 / 右端可开放（None）的时段是否重叠（相邻不算重叠）。"""
    a_end = a_end if a_end is not None else None
    b_end = b_end if b_end is not None else None
    if a_end is not None and a_end <= b_start:
        return False
    if b_end is not None and b_end <= a_start:
        return False
    return True


def _check_no_overlap(cargo_id, new_start, new_end, existing: list[TripCargo]) -> None:
    for w in existing:
        if _overlaps(new_start, new_end, w.loaded_at, w.unloaded_at):
            end_txt = w.unloaded_at.isoformat() if w.unloaded_at is not None else "仍在车"
            raise _conflict(
                f"货物 {cargo_id} 在 {new_start.isoformat()} 起的装载时段与既有车次 "
                f"{w.trip_id} 的在车时段 [{w.loaded_at.isoformat()}, {end_txt}) 重叠；"
                "同一货物不得同时属于两个车次",
                code="cargo_already_on_trip",
            )


# ----------------------------------------------------------- 登记车次


@dataclass
class WindowItem:
    cargo_id: str
    loaded_at: object
    unloaded_at: object | None


def register_trip(db, payload) -> tuple[Trip, list[TripCargo], bool]:
    """登记车次与一批货物的装卸时段（整体事务）。

    加锁顺序固定为「货物行（编号排序）-> 车次行」，与读数 / 装卸 /
    事件 / 表态路径一致，避免跨实体死锁。

    幂等：trip_id 已存在且整批内容一致 -> (trip, windows, True)。
    """
    items: list[WindowItem] = []
    seen: set[str] = set()
    for it in payload.cargos:
        loaded_at = ensure_utc(it.loaded_at)
        unloaded_at = ensure_utc(it.unloaded_at) if it.unloaded_at is not None else None
        if it.cargo_id in seen:
            raise _unprocessable(
                f"货物 {it.cargo_id} 在同一车次登记中出现多次",
                code="duplicate_cargo_in_batch",
            )
        seen.add(it.cargo_id)
        items.append(WindowItem(it.cargo_id, loaded_at, unloaded_at))

    try:
        # 先锁全部相关货物行（固定顺序），再处理车次
        cargos = _lock_cargo_rows(db, [it.cargo_id for it in items])
        # 校验窗口本身（不依赖车次是否存在），非法时连车次行都不创建
        for it in items:
            _validate_window(cargos[it.cargo_id], it.loaded_at, it.unloaded_at)

        existing_trip = db.get(Trip, payload.trip_id, with_for_update=True)
        if existing_trip is not None:
            # 车次幂等 / 冲突：整批窗口逐字段比对
            existing_windows = load_trip_windows(db, payload.trip_id)
            if _same_batch(existing_trip, existing_windows, payload, items):
                db.rollback()
                return existing_trip, existing_windows, True
            raise _conflict(
                f"车次 {payload.trip_id} 已存在且登记内容不同，拒绝覆盖",
                code="trip_exists",
            )

        trip = Trip(trip_id=payload.trip_id, truck_no=payload.truck_no)
        db.add(trip)
        db.flush()

        new_windows: list[TripCargo] = []
        batch_by_cargo: dict[str, list[TripCargo]] = {}
        for it in items:
            prior = [
                w
                for w in load_windows(db, it.cargo_id)
            ]
            _check_no_overlap(it.cargo_id, it.loaded_at, it.unloaded_at, prior)
            for bw in batch_by_cargo.get(it.cargo_id, []):
                if _overlaps(it.loaded_at, it.unloaded_at, bw.loaded_at, bw.unloaded_at):
                    raise _conflict(
                        f"货物 {it.cargo_id} 在本批登记中存在重叠装载时段",
                        code="cargo_already_on_trip",
                    )
            w = TripCargo(
                trip_id=payload.trip_id,
                cargo_id=it.cargo_id,
                loaded_at=it.loaded_at,
                unloaded_at=it.unloaded_at,
            )
            db.add(w)
            new_windows.append(w)
            batch_by_cargo.setdefault(it.cargo_id, []).append(w)

        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"车次 {payload.trip_id} 登记与既有数据冲突（车次编号/唯一约束）",
            code="integrity_conflict",
        )

    return trip, new_windows, False


def _same_batch(
    trip: Trip,
    existing_windows: list[TripCargo],
    payload,
    items: list[WindowItem],
) -> bool:
    if (trip.truck_no or None) != (payload.truck_no or None):
        return False
    have = {
        (w.cargo_id, w.loaded_at, w.unloaded_at)
        for w in existing_windows
    }
    want = {(it.cargo_id, it.loaded_at, it.unloaded_at) for it in items}
    return have == want


# ----------------------------------------------------------- 补录装卸


def add_cargo_to_trip(db, trip_id: str, payload) -> tuple[TripCargo, bool]:
    """向既有车次补录一票货物的装载时段（可只装不卸）。

    加锁顺序：货物行 -> 车次行（全局固定，避免与读数路径死锁）。
    """
    loaded_at = ensure_utc(payload.loaded_at)
    unloaded_at = ensure_utc(payload.unloaded_at) if payload.unloaded_at is not None else None
    try:
        cargo_row = _lock_cargo_rows(db, [payload.cargo_id]).get(payload.cargo_id)
        _validate_window(cargo_row, loaded_at, unloaded_at)

        trip = _lock_trip(db, trip_id)
        existing = db.get(TripCargo, (trip_id, payload.cargo_id))
        if existing is not None:
            existing.loaded_at = ensure_utc(existing.loaded_at)
            existing.unloaded_at = (
                ensure_utc(existing.unloaded_at)
                if existing.unloaded_at is not None
                else None
            )
            if (
                existing.loaded_at == loaded_at
                and existing.unloaded_at == unloaded_at
            ):
                db.rollback()
                return existing, True
            raise _conflict(
                f"货物 {payload.cargo_id} 已登记在车次 {trip_id} 上且时段不同，"
                "拒绝覆盖",
                code="cargo_already_on_trip",
            )

        prior = load_windows(db, payload.cargo_id)
        _check_no_overlap(payload.cargo_id, loaded_at, unloaded_at, prior)

        window = TripCargo(
            trip_id=trip.trip_id,
            cargo_id=payload.cargo_id,
            loaded_at=loaded_at,
            unloaded_at=unloaded_at,
        )
        db.add(window)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict(
            f"车次 {trip_id} 装载登记与既有数据冲突", code="integrity_conflict"
        )
    return window, False


def unload_cargo(db, trip_id: str, cargo_id: str, payload) -> TripCargo:
    """登记卸车时刻，闭合货物在该车次上的装载时段（货物行 -> 车次行加锁）。"""
    unloaded_at = ensure_utc(payload.unloaded_at)
    try:
        cargo = _lock_cargo_rows(db, [cargo_id], must_exist=True)[cargo_id]
        _lock_trip(db, trip_id)
        window = db.get(TripCargo, (trip_id, cargo_id))
        if window is None:
            raise ApiError(
                404,
                "cargo_not_on_trip",
                f"货物 {cargo_id} 未登记在车次 {trip_id} 上，无法卸车",
            )
        window.loaded_at = ensure_utc(window.loaded_at)
        if window.unloaded_at is not None:
            raise _conflict(
                f"货物 {cargo_id} 在车次 {trip_id} 上已登记卸车时刻 "
                f"{ensure_utc(window.unloaded_at).isoformat()}",
                code="already_unloaded",
            )
        if not (window.loaded_at < unloaded_at):
            raise _unprocessable(
                f"卸车时刻 {unloaded_at.isoformat()} 必须晚于装载时刻 "
                f"{window.loaded_at.isoformat()}（时段左闭右开）",
                code="invalid_loading_window",
            )
        if unloaded_at < cargo.started_at:
            raise _unprocessable(
                "卸车时刻早于货物起始时间",
                code="loading_before_cargo_start",
            )
        # 闭合后不得与其他窗口重叠（新右端不允许切进别的在车时段）
        others = [
            w
            for w in load_windows(db, cargo_id)
            if not (w.trip_id == trip_id)
        ]
        _check_no_overlap(cargo_id, window.loaded_at, unloaded_at, others)

        window.unloaded_at = unloaded_at
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise _conflict("卸车登记与既有数据冲突", code="integrity_conflict")
    return window


# ----------------------------------------------------------- 车载读数


@dataclass
class ReadingResult:
    reading: VehicleReading
    replayed: bool


def _same_reading(existing: VehicleReading, trip_id: str, occurred_at, temperature) -> bool:
    return (
        existing.trip_id == trip_id
        and ensure_utc(existing.occurred_at) == occurred_at
        and existing.temperature == temperature
    )


def submit_reading(db, payload) -> ReadingResult:
    """写入一笔车载读数（允许晚到）。

    同编号同内容重传幂等；同编号异内容冲突；同车次同刻读数冲突。
    读数始终按原始事实保存；是否被货物采用由查询 / 重建时的装载时段决定。
    """
    from app import chain

    occurred_at = ensure_utc(payload.occurred_at)

    # 幂等优先（编号为全局主键，不持锁即可判定；同内容直接返回）
    existing = db.get(VehicleReading, payload.reading_id)
    if existing is not None:
        existing.occurred_at = ensure_utc(existing.occurred_at)
        if _same_reading(existing, payload.trip_id, occurred_at, payload.temperature):
            db.rollback()
            return ReadingResult(existing, True)
        raise _conflict(
            f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
            code="reading_id_conflict",
        )

    try:
        # 先依据（未加锁的）装载快照确定受影响货物，按货物编号加行锁；
        # 随后再锁车次行。固定的「货物 -> 车次」加锁顺序与
        # 车次登记 / 装卸 / 自有事件 / 表态路径一致，避免跨实体死锁。
        probe_windows = [
            w
            for w in db.scalars(
                select(TripCargo).where(TripCargo.trip_id == payload.trip_id)
            ).all()
        ]
        for w in probe_windows:
            w.loaded_at = ensure_utc(w.loaded_at)
            if w.unloaded_at is not None:
                w.unloaded_at = ensure_utc(w.unloaded_at)
        affected = sorted({
            w.cargo_id
            for w in probe_windows
            if not (
                occurred_at < w.loaded_at
                or (w.unloaded_at is not None and occurred_at >= w.unloaded_at)
            )
        })
        _lock_cargo_rows(db, affected)

        trip = _lock_trip(db, payload.trip_id)

        # 同车次同刻冲突
        same_time = db.scalars(
            select(VehicleReading).where(
                VehicleReading.trip_id == trip.trip_id,
                VehicleReading.occurred_at == occurred_at,
            )
        ).first()
        if same_time is not None:
            raise _conflict(
                f"车次 {trip.trip_id} 在 {occurred_at.isoformat()} 已存在车载读数 "
                f"{same_time.reading_id}，同一时刻不允许重复",
                code="same_timestamp_conflict",
            )

        # 持车次锁后重新读取窗口（可能刚被装卸 / 登记事务更新）
        windows = load_trip_windows(db, trip.trip_id)
        affected_now = [
            w.cargo_id for w in windows if chain.cargo_on_board(w, occurred_at)
        ]
        if sorted(set(affected_now)) != affected:
            # 窗口在两次读取间变化：补锁新增货物后统一在持锁状态下继续
            _lock_cargo_rows(db, sorted(set(affected_now) - set(affected)))

        reading = VehicleReading(
            reading_id=payload.reading_id,
            trip_id=trip.trip_id,
            occurred_at=occurred_at,
            temperature=payload.temperature,
        )
        db.add(reading)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner = db.get(VehicleReading, payload.reading_id)
        if winner is not None:
            winner.occurred_at = ensure_utc(winner.occurred_at)
            if _same_reading(winner, payload.trip_id, occurred_at, payload.temperature):
                return ReadingResult(winner, True)
            raise _conflict(
                f"车载读数编号 {payload.reading_id} 已存在且内容不同，拒绝覆盖",
                code="reading_id_conflict",
            )
        raise _conflict(
            f"车载读数 {payload.reading_id} 与既有数据冲突"
            "（同车次同刻重复 / 唯一约束）",
            code="integrity_conflict",
        )

    return ReadingResult(reading, False)
