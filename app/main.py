"""冷链货物追责服务 —— FastAPI 入口。

启动（开发）: uvicorn app.main:app --reload
一键启动    : docker compose up --build  （见 README）
"""
from contextlib import asynccontextmanager
from decimal import Decimal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chain, crud, responsibility, stance_crud, trip_crud
from app.database import Base, engine, get_db
from app.models import Cargo, Event, Stance, StanceKind, Trip, TripCargo
from app.schemas import (
    CargoCreate,
    CargoOut,
    CargoTimelineOut,
    CargoTemperaturePoint,
    ChainReport,
    EventCreate,
    EventOut,
    Excursion,
    IdempotentReplay,
    LatestStance,
    ReadingAppliedCargo,
    ReadingReplay,
    ResponsibilityInterval,
    ResponsibilityReport,
    ResponsibilitySegment,
    StanceCreate,
    StanceHistory,
    StanceHistoryItem,
    StanceOut,
    StanceReplay,
    StanceSegment,
    TripCargoCreate,
    TripCreate,
    TripOut,
    TripReadingItem,
    TripReadingsReport,
    TripWindowOut,
    UnloadCreate,
    VehicleReadingCreate,
    VehicleReadingOut,
)
from app.timeutil import ensure_utc


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产中由 db/init.sql 建表；此处确保直接 uvicorn / sqlite 测试也可用
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="冷链货物追责服务",
    description=(
        "记录冷链货物的自有测温与交接事件、冷链车次/装卸时段与车载读数，"
        "按发生时间重建每票货物实际采用的测温时间线与保管链，"
        "计算越温区间并拆分各保管方责任。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.ApiError)
async def api_error_handler(request: Request, exc: crud.ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


# ---------------------------------------------------------------- 货物


@app.post("/cargo", response_model=CargoOut, status_code=201, tags=["货物"])
def create_cargo(payload: CargoCreate, db: Session = Depends(get_db)) -> Cargo:
    payload.started_at = ensure_utc(payload.started_at)
    return crud.create_cargo(db, payload)


@app.get("/cargo", response_model=list[CargoOut], tags=["货物"])
def list_cargo(db: Session = Depends(get_db)) -> list[Cargo]:
    return list(db.scalars(select(Cargo).order_by(Cargo.cargo_id)).all())


@app.get("/cargo/{cargo_id}", response_model=CargoOut, tags=["货物"])
def get_cargo(cargo_id: str, db: Session = Depends(get_db)) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    return cargo


# ---------------------------------------------------------------- 事件


@app.post(
    "/events",
    response_model=EventOut,
    status_code=201,
    tags=["事件"],
    responses={
        200: {
            "model": IdempotentReplay,
            "description": "同 ID 同内容重传：不重复写入",
        }
    },
)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    payload.occurred_at = ensure_utc(payload.occurred_at)
    result = crud.create_event(db, payload)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=IdempotentReplay(
                event_id=payload.event_id,
                message="相同内容的事件已存在，重传被忽略（幂等）",
            ).model_dump(),
        )
    return result.event


@app.get(
    "/cargo/{cargo_id}/events",
    response_model=list[EventOut],
    tags=["事件"],
)
def list_events(cargo_id: str, db: Session = Depends(get_db)) -> list[Event]:
    if db.get(Cargo, cargo_id) is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    return events


# ---------------------------------------------------------------- 追责查询


@app.get(
    "/cargo/{cargo_id}/report",
    response_model=ChainReport,
    tags=["追责查询"],
)
def cargo_report(cargo_id: str, db: Session = Depends(get_db)) -> ChainReport:
    """按合并后的测温时间线返回越温区间（起止）与各责任方片段；未闭合区间标记未结束。"""
    cargo, events, windows, readings = trip_crud.load_cargo_context(db, cargo_id)

    result = chain.rebuild_chain(
        cargo.initial_holder,
        Decimal(cargo.temp_upper_limit),
        events,
        chain.applicable_vehicle_readings(windows, readings),
    )

    excursions = [_excursion_schema(ex, result) for ex in result.excursions]
    return ChainReport(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        current_holder=result.current_holder,
        excursions=excursions,
    )


def _excursion_schema(ex, result=None) -> Excursion:
    start_trip_id = None
    if result is not None and ex.start_reading_id is not None:
        for p in result.timeline:
            if p.source == "vehicle" and p.source_id == ex.start_reading_id:
                start_trip_id = p.trip_id
                break
    return Excursion(
        start=ex.start,
        end=ex.end,
        is_open=ex.is_open,
        opener_source=ex.opener_source,
        closer_source=ex.closer_source,
        start_event_id=ex.start_event_id,
        start_reading_id=ex.start_reading_id,
        start_trip_id=start_trip_id,
        end_event_id=ex.end_event_id,
        end_reading_id=ex.end_reading_id,
        interval_id=ex.interval_key,
        segments=[
            ResponsibilitySegment(
                start=seg.start, end=seg.end, responsible_party=seg.party
            )
            for seg in ex.segments
        ],
    )


# ---------------------------------------------------------------- 责任确认


def _load_cargo_for_resp(db, cargo_id: str) -> Cargo:
    cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    return cargo


def _interval_schema(view) -> ResponsibilityInterval:
    """责任确认视图 -> 响应模型。"""
    return ResponsibilityInterval(
        cargo_id=view.cargo_id,
        interval_id=view.interval_key,
        opener_source=view.opener_source,
        closer_source=view.closer_source,
        start_event_id=view.start_event_id,
        start_reading_id=view.start_reading_id,
        start_trip_id=view.start_trip_id,
        end_event_id=view.end_event_id,
        end_reading_id=view.end_reading_id,
        start=view.start,
        end=view.end,
        snapshot_id=view.snapshot_id,
        segments=[
            StanceSegment(
                start=seg.start, end=seg.end, responsible_party=seg.responsible_party
            )
            for seg in view.segments
        ],
        latest_stances=[
            LatestStance(
                stance_id=sv.stance_id,
                responsible_party=sv.responsible_party,
                stance=sv.kind,
                reason=sv.reason,
                created_at=sv.created_at,
                segments=[
                    StanceSegment(
                        start=s.start,
                        end=s.end,
                        responsible_party=s.responsible_party,
                    )
                    for s in sv.segments
                ],
            )
            for sv in view.stances
        ],
        summary=view.summary,
    )


def _closed_interval_views(db, cargo: Cargo):
    """按合并时间线重建，并为每个已闭合区间装配责任确认视图（按开始时间升序）。"""
    _, events, windows, readings = trip_crud.load_cargo_context(db, cargo.cargo_id)
    result, closed = responsibility.rebuild(cargo, events, windows, readings)
    views = []
    for ex in sorted(closed.values(), key=lambda e: e.start):
        rows = list(
            db.scalars(
                select(Stance).where(
                    Stance.cargo_id == cargo.cargo_id,
                    Stance.interval_key == ex.interval_key,
                )
            ).all()
        )
        views.append(
            responsibility.build_interval_view(
                cargo.cargo_id, ex, rows, chain_result=result
            )
        )
    return result, views


@app.get(
    "/cargo/{cargo_id}/responsibilities",
    response_model=ResponsibilityReport,
    tags=["责任确认"],
)
def list_responsibilities(cargo_id: str, db: Session = Depends(get_db)) -> ResponsibilityReport:
    """列出货物下所有**已闭合**越温区间的责任确认状态（开放区间不参与）。"""
    cargo = _load_cargo_for_resp(db, cargo_id)
    _, views = _closed_interval_views(db, cargo)
    return ResponsibilityReport(
        cargo_id=cargo_id, intervals=[_interval_schema(v) for v in views]
    )


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}",
    response_model=ResponsibilityInterval,
    tags=["责任确认"],
    responses={404: {"description": "货物 / 区间不存在"}, 422: {"description": "区间开放"}},
)
def get_responsibility(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> ResponsibilityInterval:
    """按区间稳定标识（开启事件ID，或 vehicle:<读数ID>）查询单个已闭合区间视图。"""
    cargo = _load_cargo_for_resp(db, cargo_id)

    result, ex = stance_crud._resolve_interval(db, cargo, interval_id)
    rows = list(
        db.scalars(
            select(Stance).where(
                Stance.cargo_id == cargo_id,
                Stance.interval_key == interval_id,
            )
        ).all()
    )
    return _interval_schema(
        responsibility.build_interval_view(cargo_id, ex, rows, chain_result=result)
    )


@app.post(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceOut,
    status_code=201,
    tags=["责任确认"],
    responses={
        200: {"model": StanceReplay, "description": "同编号同内容重传：返回原记录"},
    },
)
def submit_stance(
    interval_id: str,
    payload: StanceCreate,
    cargo_id: str,
    db: Session = Depends(get_db),
):
    """提交责任方表态（确认 / 异议）。

    与同货物的事件写入共用货物行锁串行化；以当前快照校验，
    过期快照、区间不存在或开放区间一律拒绝且不落库。
    """
    result = stance_crud.submit_stance(db, cargo_id, interval_id, payload)
    stance = result.stance
    body = StanceOut(
        stance_id=stance.stance_id,
        cargo_id=stance.cargo_id,
        interval_id=stance.interval_key,
        responsible_party=stance.responsible_party,
        stance=StanceKind(stance.kind),
        reason=stance.reason,
        snapshot_id=stance.snapshot_id,
        created_at=ensure_utc(stance.created_at),
    )
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=StanceReplay(
                replayed=True,
                stance=body,
                message="相同内容的表态已存在，重传被忽略（幂等；即使该表态快照已失效也返回原记录）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/cargo/{cargo_id}/responsibilities/{interval_id}/stances",
    response_model=StanceHistory,
    tags=["责任确认"],
)
def list_stance_history(
    interval_id: str, cargo_id: str, db: Session = Depends(get_db)
) -> StanceHistory:
    """区间表态历史：历次表态全部保留（含已改口、已失效），按提交时间升序。

    区间当前不存在 / 仍开放时，历史仍可查，头部字段给出当前状态。
    """
    cargo = _load_cargo_for_resp(db, cargo_id)
    rows = list(
        db.scalars(
            select(Stance)
            .where(
                Stance.cargo_id == cargo_id,
                Stance.interval_key == interval_id,
            )
            .order_by(Stance.created_at, Stance.stance_id)
        ).all()
    )

    # 当前区间状态（不因此报 404/422：历史要始终可查）
    _, events, windows, readings = trip_crud.load_cargo_context(db, cargo_id)
    chain_result, closed = responsibility.rebuild(cargo, events, windows, readings)
    ex = closed.get(interval_id)
    open_ex = next(
        (e for e in chain_result.excursions if e.interval_key == interval_id), None
    )
    interval_exists = ex is not None or open_ex is not None
    is_open = ex is None and open_ex is not None
    current_snapshot = responsibility.snapshot_id(ex) if ex is not None else None

    # 各责任方“最新有效表态”（行已按 (created_at, stance_id) 升序），
    # 用于把更早的有效表态标记为已改口
    latest_current: dict[str, Stance] = {}
    if ex is not None:
        for s in rows:
            if responsibility.is_stance_current(s, ex):
                latest_current[s.responsible_party] = s

    items = [
        StanceHistoryItem(
            stance_id=s.stance_id,
            cargo_id=s.cargo_id,
            interval_id=s.interval_key,
            responsible_party=s.responsible_party,
            stance=StanceKind(s.kind),
            reason=s.reason,
            snapshot_id=s.snapshot_id,
            created_at=ensure_utc(s.created_at),
            is_current=(ex is not None and responsibility.is_stance_current(s, ex)),
            # 改口只在“当前仍有效”的行之间比较：已因快照变化失效的行不算被改口
            superseded=(
                ex is not None
                and responsibility.is_stance_current(s, ex)
                and (
                    (latest := latest_current.get(s.responsible_party)) is not None
                    and latest.stance_id != s.stance_id
                )
            ),
        )
        for s in rows
    ]
    return StanceHistory(
        cargo_id=cargo_id,
        interval_id=interval_id,
        interval_exists=interval_exists,
        is_open=is_open,
        current_snapshot_id=current_snapshot,
        history=items,
    )


# ---------------------------------------------------------------- 车次 / 车载读数


def _trip_out(trip: Trip, windows: list[TripCargo]) -> TripOut:
    for w in windows:
        w.loaded_at = ensure_utc(w.loaded_at)
        if w.unloaded_at is not None:
            w.unloaded_at = ensure_utc(w.unloaded_at)
    return TripOut(
        trip_id=trip.trip_id,
        truck_no=trip.truck_no,
        created_at=ensure_utc(trip.created_at),
        cargos=[
            TripWindowOut(
                cargo_id=w.cargo_id,
                loaded_at=w.loaded_at,
                unloaded_at=w.unloaded_at,
            )
            for w in sorted(windows, key=lambda w: (w.loaded_at, w.cargo_id))
        ],
    )


@app.post(
    "/trips",
    response_model=TripOut,
    status_code=201,
    tags=["车次"],
    responses={200: {"model": TripOut, "description": "同车次同内容重传"}},
)
def register_trip(payload: TripCreate, db: Session = Depends(get_db)):
    """登记车次及每票货物的装卸时段（整体事务：失败不留任何部分记录）。"""
    trip, windows, replayed = trip_crud.register_trip(db, payload)
    out = _trip_out(trip, windows)
    if replayed:
        return JSONResponse(status_code=200, content=out.model_dump(mode="json"))
    return out


@app.get("/trips", response_model=list[TripOut], tags=["车次"])
def list_trips(db: Session = Depends(get_db)) -> list[TripOut]:
    trips = list(db.scalars(select(Trip).order_by(Trip.trip_id)).all())
    return [
        _trip_out(t, trip_crud.load_trip_windows(db, t.trip_id)) for t in trips
    ]


@app.get("/trips/{trip_id}", response_model=TripOut, tags=["车次"])
def get_trip(trip_id: str, db: Session = Depends(get_db)) -> TripOut:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    return _trip_out(trip, trip_crud.load_trip_windows(db, trip_id))


@app.post(
    "/trips/{trip_id}/cargos",
    response_model=TripWindowOut,
    status_code=201,
    tags=["车次"],
    responses={200: {"model": TripWindowOut, "description": "同内容重传"}},
)
def add_trip_cargo(
    trip_id: str, payload: TripCargoCreate, db: Session = Depends(get_db)
):
    """向既有车次补录一票货物的装卸时段（可只装不卸；与既有在车时段不得重叠）。"""
    window, replayed = trip_crud.add_cargo_to_trip(db, trip_id, payload)
    window.loaded_at = ensure_utc(window.loaded_at)
    if window.unloaded_at is not None:
        window.unloaded_at = ensure_utc(window.unloaded_at)
    out = TripWindowOut.model_validate(window)
    if replayed:
        return JSONResponse(status_code=200, content=out.model_dump(mode="json"))
    return out


@app.post(
    "/trips/{trip_id}/cargos/{cargo_id}/unload",
    response_model=TripWindowOut,
    tags=["车次"],
)
def unload_trip_cargo(
    trip_id: str,
    cargo_id: str,
    payload: UnloadCreate,
    db: Session = Depends(get_db),
) -> TripWindowOut:
    """登记卸车时刻（左闭右开：卸车时刻该货物已不在车上）。"""
    window = trip_crud.unload_cargo(db, trip_id, cargo_id, payload)
    window.loaded_at = ensure_utc(window.loaded_at)
    window.unloaded_at = ensure_utc(window.unloaded_at)
    return TripWindowOut.model_validate(window)


@app.post(
    "/vehicle-readings",
    response_model=VehicleReadingOut,
    status_code=201,
    tags=["车载读数"],
    responses={200: {"model": ReadingReplay, "description": "同编号同内容重传"}},
)
def submit_vehicle_reading(
    payload: VehicleReadingCreate, db: Session = Depends(get_db)
):
    """登记一笔车载测温原始读数（允许晚到）。

    读数是否作用于某票货物，取决于读数时刻该货物是否仍在车上、
    以及同刻是否有货物自有测温（自有优先）。
    """
    result = trip_crud.submit_reading(db, payload)
    r = result.reading
    body = VehicleReadingOut(
        reading_id=r.reading_id,
        trip_id=r.trip_id,
        occurred_at=ensure_utc(r.occurred_at),
        temperature=r.temperature,
        created_at=ensure_utc(r.created_at),
    )
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=ReadingReplay(
                replayed=True,
                reading=body,
                message="相同内容的车载读数已存在，重传被忽略（幂等）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/trips/{trip_id}/readings",
    response_model=TripReadingsReport,
    tags=["车载读数"],
)
def trip_readings(trip_id: str, db: Session = Depends(get_db)) -> TripReadingsReport:
    """车次的全部原始车载读数，以及各读数对车上货物的采用 / 越温情况。

    每票货物按各自温度上限独立判断越温；同刻有货物自有测温的，
    该货物上本读数 adopted=false（原始读数仍列出）。
    """
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")

    windows = trip_crud.load_trip_windows(db, trip_id)
    readings = trip_crud.load_readings(db, trip_id=trip_id)

    # 货物自有测温时刻表（仅取车上相关货物），用于“自有优先遮蔽”
    cargo_ids = sorted({w.cargo_id for w in windows})
    cargos = {c.cargo_id: c for c in db.scalars(
        select(Cargo).where(Cargo.cargo_id.in_(cargo_ids))
    ).all()} if cargo_ids else {}
    own_at: dict[tuple[str, object], str] = {}
    if cargo_ids:
        own_events = list(
            db.scalars(
                select(Event).where(
                    Event.cargo_id.in_(cargo_ids),
                    Event.event_type == "measurement",
                )
            ).all()
        )
        for e in own_events:
            e.occurred_at = ensure_utc(e.occurred_at)
            own_at[(e.cargo_id, e.occurred_at)] = e.event_id

    items: list[TripReadingItem] = []
    for r in readings:
        applied: list[ReadingAppliedCargo] = []
        for w in windows:
            if not chain.cargo_on_board(w, r.occurred_at):
                continue
            cargo = cargos.get(w.cargo_id)
            if cargo is None:
                continue
            limit = Decimal(cargo.temp_upper_limit)
            own_eid = own_at.get((w.cargo_id, r.occurred_at))
            applied.append(
                ReadingAppliedCargo(
                    cargo_id=w.cargo_id,
                    temp_upper_limit=limit,
                    is_over_limit=Decimal(r.temperature) > limit,
                    adopted=own_eid is None,
                    own_event_id=own_eid,
                )
            )
        applied.sort(key=lambda a: a.cargo_id)
        items.append(
            TripReadingItem(
                reading_id=r.reading_id,
                trip_id=r.trip_id,
                occurred_at=r.occurred_at,
                temperature=r.temperature,
                created_at=ensure_utc(r.created_at),
                applies_to=applied,
            )
        )

    return TripReadingsReport(trip_id=trip_id, readings=items)


@app.get(
    "/cargo/{cargo_id}/temperatures",
    response_model=CargoTimelineOut,
    tags=["追责查询"],
)
def cargo_temperatures(cargo_id: str, db: Session = Depends(get_db)) -> CargoTimelineOut:
    """该货物实际采用的测温时间线：货物自有测温 + 生效车载读数（自有优先）。"""
    cargo, events, windows, readings = trip_crud.load_cargo_context(db, cargo_id)
    limit = Decimal(cargo.temp_upper_limit)
    result = chain.rebuild_chain(
        cargo.initial_holder,
        limit,
        events,
        chain.applicable_vehicle_readings(windows, readings),
    )
    points = [
        CargoTemperaturePoint(
            occurred_at=p.occurred_at,
            temperature=p.temperature,
            source=p.source,
            event_id=p.event_id,
            reading_id=p.reading_id,
            trip_id=p.trip_id,
            temp_upper_limit=limit,
            is_over_limit=p.temperature > limit,
        )
        for p in result.timeline
    ]
    return CargoTimelineOut(
        cargo_id=cargo_id, temp_upper_limit=limit, readings=points
    )


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}
