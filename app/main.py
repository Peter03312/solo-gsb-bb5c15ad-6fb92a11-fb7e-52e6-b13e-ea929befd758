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

from app import crud, responsibility, stance_crud, timeline, trip_crud
from app.database import Base, engine, get_db
from app.models import Cargo, CargoLoading, Event, OnboardReading, Stance, StanceKind, Trip
from app.schemas import (
    CargoCreate,
    CargoOut,
    CargoReading,
    CargoReadings,
    ChainReport,
    EventCreate,
    EventOut,
    Excursion,
    IdempotentReplay,
    LatestStance,
    LoadingOut,
    LoadingsCreate,
    LoadingsResult,
    OnboardReadingCreate,
    OnboardReadingOut,
    OnboardReadingReplay,
    ResponsibilityInterval,
    ResponsibilityReport,
    ResponsibilitySegment,
    StanceCreate,
    StanceHistory,
    StanceHistoryItem,
    StanceOut,
    StanceReplay,
    StanceSegment,
    TripCreate,
    TripDetail,
    TripOut,
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
        "记录冷链货物的测温与交接事件，按发生时间重建保管链，"
        "计算越温区间并拆分各保管方责任。"
    ),
    version="1.0.0",
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


# ---------------------------------------------------------------- 车次 / 装载 / 车载读数


@app.post("/trips", response_model=TripOut, status_code=201, tags=["车次与车载读数"])
def create_trip(payload: TripCreate, db: Session = Depends(get_db)) -> Trip:
    return trip_crud.create_trip(db, payload)


@app.get("/trips", response_model=list[TripOut], tags=["车次与车载读数"])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    return list(db.scalars(select(Trip).order_by(Trip.trip_id)).all())


def _loading_schema(row: CargoLoading) -> LoadingOut:
    return LoadingOut(
        trip_id=row.trip_id,
        cargo_id=row.cargo_id,
        loaded_at=ensure_utc(row.loaded_at),
        unloaded_at=ensure_utc(row.unloaded_at),
    )


def _reading_schema(row: OnboardReading) -> OnboardReadingOut:
    return OnboardReadingOut(
        reading_id=row.reading_id,
        trip_id=row.trip_id,
        read_at=ensure_utc(row.read_at),
        temperature=row.temperature,
    )


@app.get(
    "/trips/{trip_id}",
    response_model=TripDetail,
    tags=["车次与车载读数"],
    responses={404: {"description": "车次不存在"}},
)
def get_trip(trip_id: str, db: Session = Depends(get_db)) -> TripDetail:
    """车次详情：车次信息、各票货物装卸时段、全部车载原始读数（均按时间升序）。"""
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    loadings = list(
        db.scalars(
            select(CargoLoading)
            .where(CargoLoading.trip_id == trip_id)
            .order_by(CargoLoading.loaded_at, CargoLoading.cargo_id)
        ).all()
    )
    readings = list(
        db.scalars(
            select(OnboardReading)
            .where(OnboardReading.trip_id == trip_id)
            .order_by(OnboardReading.read_at, OnboardReading.reading_id)
        ).all()
    )
    return TripDetail(
        trip_id=trip.trip_id,
        vehicle_no=trip.vehicle_no,
        note=trip.note,
        created_at=ensure_utc(trip.created_at),
        loadings=[_loading_schema(r) for r in loadings],
        readings=[_reading_schema(r) for r in readings],
    )


@app.post(
    "/trips/{trip_id}/loadings",
    response_model=LoadingsResult,
    status_code=201,
    tags=["车次与车载读数"],
    responses={404: {"description": "车次或货物不存在"},
               409: {"description": "时段冲突 / 同货物同时属于两个车次"},
               422: {"description": "装载时段无效（起点不早于终点）/ 空批次"}},
)
def register_loadings(
    trip_id: str, payload: LoadingsCreate, db: Session = Depends(get_db)
) -> LoadingsResult:
    """登记车次承载的一票或多票货物的装卸时段（左闭右开）。

    整体校验、整体写入：车次或任一货物不存在、任一时段无效、同货物与既有时段
    （跨车次）相交时全部拒绝，不留部分记录。
    """
    rows = trip_crud.register_loadings(db, trip_id, payload.loadings)
    return LoadingsResult(
        trip_id=trip_id, loadings=[_loading_schema(r) for r in rows]
    )


@app.post(
    "/trips/{trip_id}/readings",
    response_model=OnboardReadingOut,
    status_code=201,
    tags=["车次与车载读数"],
    responses={
        200: {"model": OnboardReadingReplay, "description": "同编号同内容重传"},
        404: {"description": "车次不存在"},
        409: {"description": "读数编号异内容冲突 / 同车次同刻重复"},
    },
)
def create_onboard_reading(
    trip_id: str, payload: OnboardReadingCreate, db: Session = Depends(get_db)
):
    """写入车载原始读数（允许晚到）。

    编号同内容重传幂等（200 replayed），异内容冲突（409 reading_id_conflict）；
    同车次同一时刻至多一笔读数。读数只对读数时刻仍在车上的货物生效，
    同刻若货物有自有测温则自有值优先（原始读数仍可经车次接口查询）。
    """
    # 路径车次与请求体车次必须一致
    if payload.trip_id != trip_id:
        raise crud.ApiError(
            422,
            "bad_reading_shape",
            f"路径车次 {trip_id} 与请求体车次 {payload.trip_id} 不一致",
        )
    payload.read_at = ensure_utc(payload.read_at)
    result = trip_crud.create_reading(db, payload)
    body = _reading_schema(result.reading)
    if result.replayed:
        return JSONResponse(
            status_code=200,
            content=OnboardReadingReplay(
                replayed=True,
                reading=body,
                message="相同内容的车载读数已存在，重传被忽略（幂等）",
            ).model_dump(mode="json"),
        )
    return body


@app.get(
    "/trips/{trip_id}/readings",
    response_model=list[OnboardReadingOut],
    tags=["车次与车载读数"],
    responses={404: {"description": "车次不存在"}},
)
def list_onboard_readings(
    trip_id: str, db: Session = Depends(get_db)
) -> list[OnboardReadingOut]:
    """查询车次的全部车载原始读数（按读数时刻升序；不论是否被货物采用）。"""
    if db.get(Trip, trip_id) is None:
        raise crud.ApiError(404, "trip_not_found", f"车次 {trip_id} 不存在")
    rows = list(
        db.scalars(
            select(OnboardReading)
            .where(OnboardReading.trip_id == trip_id)
            .order_by(OnboardReading.read_at, OnboardReading.reading_id)
        ).all()
    )
    return [_reading_schema(r) for r in rows]


@app.get(
    "/cargo/{cargo_id}/readings",
    response_model=CargoReadings,
    tags=["车次与车载读数"],
    responses={404: {"description": "货物不存在"}},
)
def cargo_readings(cargo_id: str, db: Session = Depends(get_db)) -> CargoReadings:
    """查询货物采用的读数时间线及各读数来源。

    合并货物自有测温与相关车次车载读数：标注每条读数的来源（own/onboard）、
    是否被追责时间线采用（adopted）及未采用原因；同刻自有测温优先。
    """
    cargo = _load_cargo_for_resp(db, cargo_id)
    merged = timeline.build_cargo_timeline(db, cargo)
    return CargoReadings(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        readings=[
            CargoReading(
                occurred_at=rv.occurred_at,
                temperature=rv.temperature,
                source=rv.source,
                adopted=rv.adopted,
                event_id=rv.event_id,
                reading_id=rv.reading_id,
                trip_id=rv.trip_id,
                not_adopted_reason=rv.not_adopted_reason,
            )
            for rv in merged.readings
        ],
    )


# ---------------------------------------------------------------- 追责查询


@app.get(
    "/cargo/{cargo_id}/report",
    response_model=ChainReport,
    tags=["追责查询"],
)
def cargo_report(cargo_id: str, db: Session = Depends(get_db)) -> ChainReport:
    """按时间返回越温区间（起止）与各责任方片段；未闭合区间标记未结束。

    越温判定基于**合并后的追责时间线**：货物自有测温与读数时刻仍在车上、
    且同刻无自有测温的车载读数合并后，按货物各自温度上限重建。
    """
    cargo = _load_cargo_for_resp(db, cargo_id)

    result, _ = timeline.rebuild_cargo_chain(db, cargo)

    excursions = [
        Excursion(
            start=ex.start,
            end=ex.end,
            is_open=ex.is_open,
            segments=[
                ResponsibilitySegment(
                    start=seg.start, end=seg.end, responsible_party=seg.party
                )
                for seg in ex.segments
            ],
            interval_id=ex.start_event_id,
            start_source=ex.start_source,
            end_source=ex.end_source,
            start_event_id=ex.start_point_id,
            end_event_id=ex.end_point_id,
            start_trip_id=ex.start_trip_id,
            end_trip_id=ex.end_trip_id,
        )
        for ex in result.excursions
    ]
    return ChainReport(
        cargo_id=cargo.cargo_id,
        temp_upper_limit=Decimal(cargo.temp_upper_limit),
        current_holder=result.current_holder,
        excursions=excursions,
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
        interval_id=view.start_event_id,
        start_event_id=view.start_point_id,
        end_event_id=view.end_point_id,
        start_source=view.start_source,
        end_source=view.end_source,
        start_trip_id=view.start_trip_id,
        end_trip_id=view.end_trip_id,
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
    """重建合并时间线并为每个已闭合区间装配责任确认视图（按开始时间升序）。"""
    merged = timeline.build_cargo_timeline(db, cargo)
    result, closed = responsibility.rebuild(cargo, timeline=list(merged.timeline))
    views = []
    for ex in sorted(closed.values(), key=lambda e: e.start):
        rows = list(
            db.scalars(
                select(Stance).where(
                    Stance.cargo_id == cargo.cargo_id,
                    Stance.start_event_id == ex.start_event_id,
                )
            ).all()
        )
        views.append(
            responsibility.build_interval_view(cargo.cargo_id, ex, rows)
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
    """按区间标识（开启测温事件ID）查询单个已闭合区间的责任确认视图。"""
    cargo = _load_cargo_for_resp(db, cargo_id)

    _, ex = stance_crud._resolve_interval(db, cargo, interval_id)
    rows = list(
        db.scalars(
            select(Stance).where(
                Stance.cargo_id == cargo_id,
                Stance.start_event_id == interval_id,
            )
        ).all()
    )
    return _interval_schema(
        responsibility.build_interval_view(cargo_id, ex, rows)
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
        interval_id=stance.start_event_id,
        start_event_id=stance.start_event_id,
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
                Stance.start_event_id == interval_id,
            )
            .order_by(Stance.created_at, Stance.stance_id)
        ).all()
    )

    # 当前区间状态（不因此报 404/422：历史要始终可查）
    merged = timeline.build_cargo_timeline(db, cargo)
    chain_result, closed = responsibility.rebuild(
        cargo, timeline=list(merged.timeline)
    )
    ex = closed.get(interval_id)
    open_ex = next(
        (e for e in chain_result.excursions if e.start_event_id == interval_id), None
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
            interval_id=s.start_event_id,
            start_event_id=s.start_event_id,
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


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}
