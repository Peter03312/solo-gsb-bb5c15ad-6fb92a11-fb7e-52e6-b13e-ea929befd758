"""车次 / 装载时段 / 车载读数 API 集成测试。

覆盖：
* 车次登记（一票/多票货物、装卸时段左闭右开、仍在车上）；
* 装载时段非法、货物/车次不存在、同货物不得同时属于两个车次（整体拒绝不留部分记录）；
* 车载读数仅作用于读数时刻仍在车上的货物，按各自温度上限判越温；
* 同刻货物自有测温优先采用，车载原始读数仍可查；
* 交接仍只由货物自有事件改变保管方；
* 车载读数晚到合并重建；编号同内容幂等、异内容冲突；同车次同刻冲突；
* 车次读数查询（各货物的读数来源/采用情况）与货物测温时间线；
* 车载读数开启区间的稳定标识 vehicle:<reading_id>、责任表态、晚到重建失效。
"""
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Trip, TripCargo, VehicleReading

T0 = "2026-10-01T00:{:02d}:00+00:00"


def make_cargo(client, cid, limit="4", holder="A仓库", start="2026-10-01T00:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cid,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def register_trip(client, tid, cargos, truck_no=None):
    body = {"trip_id": tid, "cargos": cargos}
    if truck_no is not None:
        body["truck_no"] = truck_no
    return client.post("/trips", json=body)


def reading(client, rid, tid, minute, temp, hour=0):
    ts = f"2026-10-01T{hour:02d}:{minute:02d}:00+00:00"
    return client.post(
        "/vehicle-readings",
        json={"reading_id": rid, "trip_id": tid, "occurred_at": ts, "temperature": str(temp)},
    )


def meas(client, eid, cid, minute, temp, hour=0):
    return client.post(
        "/events",
        json={
            "event_id": eid, "cargo_id": cid,
            "occurred_at": f"2026-10-01T{hour:02d}:{minute:02d}:00+00:00",
            "temperature": str(temp),
        },
    )


def handover(client, eid, cid, minute, giver, receiver, hour=0):
    return client.post(
        "/events",
        json={
            "event_id": eid, "cargo_id": cid,
            "occurred_at": f"2026-10-01T{hour:02d}:{minute:02d}:00+00:00",
            "giver": giver, "receiver": receiver,
        },
    )


def db_rows():
    db = SessionLocal()
    try:
        return (
            db.query(Trip).count(),
            db.query(TripCargo).count(),
            db.query(VehicleReading).count(),
        )
    finally:
        db.close()


# ----------------------------------------------------------- 车次登记


def test_register_trip_ok_and_get(client: TestClient):
    make_cargo(client, "G1")
    make_cargo(client, "G2")
    r = register_trip(
        client,
        "V1",
        [
            {"cargo_id": "G1", "loaded_at": T0.format(0), "unloaded_at": T0.format(30)},
            {"cargo_id": "G2", "loaded_at": T0.format(5)},
        ],
        truck_no="京A123",
    )
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["trip_id"] == "V1"
    assert out["truck_no"] == "京A123"
    assert {c["cargo_id"] for c in out["cargos"]} == {"G1", "G2"}
    g2 = next(c for c in out["cargos"] if c["cargo_id"] == "G2")
    assert g2["unloaded_at"] is None

    g = client.get("/trips/V1").json()
    assert len(g["cargos"]) == 2
    assert len(client.get("/trips").json()) == 1


def test_register_trip_idempotent_same_content_conflict_different(client: TestClient):
    make_cargo(client, "G1")
    body = {
        "trip_id": "V1",
        "cargos": [{"cargo_id": "G1", "loaded_at": T0.format(0), "unloaded_at": T0.format(30)}],
    }
    assert client.post("/trips", json=body).status_code == 201
    r2 = client.post("/trips", json=body)
    assert r2.status_code == 200  # 同内容重传

    body["cargos"][0]["unloaded_at"] = T0.format(40)
    r3 = client.post("/trips", json=body)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "trip_exists"


def test_invalid_loading_window_rejected_and_atomic(client: TestClient):
    make_cargo(client, "G1")
    make_cargo(client, "G2")
    before = db_rows()
    # 起点不早于终点（左闭右开）
    r = register_trip(
        client,
        "VBAD",
        [
            {"cargo_id": "G1", "loaded_at": T0.format(10), "unloaded_at": T0.format(10)},
            {"cargo_id": "G2", "loaded_at": T0.format(0)},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
    assert db_rows() == before  # 不留车次 / 窗口部分记录

    # 货物不存在 -> 404 且整体不留记录
    r = register_trip(
        client,
        "VBAD2",
        [{"cargo_id": "NOPE", "loaded_at": T0.format(0)}],
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "cargo_not_found"
    assert db_rows() == before

    # 批内同一货物出现两次
    r = register_trip(
        client,
        "VBAD3",
        [
            {"cargo_id": "G1", "loaded_at": T0.format(0), "unloaded_at": T0.format(10)},
            {"cargo_id": "G1", "loaded_at": T0.format(20), "unloaded_at": T0.format(30)},
        ],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "duplicate_cargo_in_batch"
    assert db_rows() == before


def test_loading_before_cargo_start_rejected(client: TestClient):
    make_cargo(client, "G1", start="2026-10-01T01:00:00+00:00")
    r = register_trip(
        client,
        "V1",
        [{"cargo_id": "G1", "loaded_at": T0.format(0), "unloaded_at": T0.format(30)}],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "loading_before_cargo_start"


def test_cargo_cannot_belong_to_two_trips_same_time(client: TestClient):
    make_cargo(client, "G1")
    register_trip(
        client, "V1",
        [{"cargo_id": "G1", "loaded_at": T0.format(0), "unloaded_at": T0.format(30)}],
    )
    before = db_rows()
    # 与 [00:00,00:30) 重叠 -> 拒绝
    r = register_trip(
        client, "V2",
        [{"cargo_id": "G1", "loaded_at": T0.format(20), "unloaded_at": T0.format(40)}],
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "cargo_already_on_trip"
    assert db_rows() == before  # V2 未留下

    # 相邻时段（前一窗右端 = 后一窗左端）不算重叠：允许
    r = register_trip(
        client, "V3",
        [{"cargo_id": "G1", "loaded_at": T0.format(30), "unloaded_at": T0.format(40)}],
    )
    assert r.status_code == 201, r.text


def test_add_and_unload_cargo(client: TestClient):
    make_cargo(client, "G1")
    r = client.post(
        "/trips",
        json={"trip_id": "V1", "cargos": [
            {"cargo_id": "G1", "loaded_at": T0.format(0)}]},
    )
    assert r.status_code == 201, r.text

    # 向不存在的车次补录
    r2 = client.post(
        "/trips/NOPE/cargos",
        json={"cargo_id": "G1", "loaded_at": T0.format(0)},
    )
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "trip_not_found"

    # 登记卸车
    r3 = client.post(
        "/trips/V1/cargos/G1/unload", json={"unloaded_at": T0.format(40)}
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["unloaded_at"].startswith("2026-10-01T00:40:00")

    # 重复卸车 -> 409
    r4 = client.post(
        "/trips/V1/cargos/G1/unload", json={"unloaded_at": T0.format(45)}
    )
    assert r4.status_code == 409
    assert r4.json()["error"]["code"] == "already_unloaded"

    # 卸车时刻早于装载 -> 422
    make_cargo(client, "G2")
    client.post(
        "/trips/V1/cargos",
        json={"cargo_id": "G2", "loaded_at": T0.format(50)},
    )
    r5 = client.post(
        "/trips/V1/cargos/G2/unload", json={"unloaded_at": T0.format(49)}
    )
    assert r5.status_code == 422
    assert r5.json()["error"]["code"] == "invalid_loading_window"


# ----------------------------------------------------------- 车载读数作用域


def test_vehicle_reading_only_applies_while_on_board(client: TestClient):
    # G1: [00:00, 00:30)；G2: [00:20, ∞)。同一读数 8℃（上限均为 4）
    make_cargo(client, "G1")
    make_cargo(client, "G2")
    register_trip(
        client, "V1",
        [
            {"cargo_id": "G1", "loaded_at": T0.format(0), "unloaded_at": T0.format(30)},
            {"cargo_id": "G2", "loaded_at": T0.format(20)},
        ],
    )
    assert reading(client, "r1", "V1", 25, 8).status_code == 201

    rpt = client.get("/trips/V1/readings").json()
    applies = {a["cargo_id"]: a for a in rpt["readings"][0]["applies_to"]}
    assert set(applies) == {"G1", "G2"}
    assert all(a["is_over_limit"] is True for a in applies.values())
    assert all(a["adopted"] is True for a in applies.values())

    # 00:30 的读数：G1 已在 00:30 卸车（右开），不作用于 G1
    assert reading(client, "r2", "V1", 30, 9).status_code == 201
    rpt = client.get("/trips/V1/readings").json()
    last = rpt["readings"][1]
    assert [a["cargo_id"] for a in last["applies_to"]] == ["G2"]

    # 00:00 恰好装载（左闭）作用于 G1；G2 00:20 才装，不作用
    assert reading(client, "r0", "V1", 0, 7).status_code == 201
    rpt = client.get("/trips/V1/readings").json()
    first = rpt["readings"][0]
    assert [a["cargo_id"] for a in first["applies_to"]] == ["G1"]


def test_per_cargo_temp_limit(client: TestClient):
    # 同一读数 4.5℃：G1 上限 4 -> 越温；G2 上限 6 -> 不越温
    make_cargo(client, "G1", limit="4")
    make_cargo(client, "G2", limit="6")
    register_trip(
        client, "V1",
        [
            {"cargo_id": "G1", "loaded_at": T0.format(0)},
            {"cargo_id": "G2", "loaded_at": T0.format(0)},
        ],
    )
    reading(client, "r1", "V1", 5, "4.5")
    applies = {
        a["cargo_id"]: a
        for a in client.get("/trips/V1/readings").json()["readings"][0]["applies_to"]
    }
    assert applies["G1"]["is_over_limit"] is True
    assert applies["G2"]["is_over_limit"] is False

    rep1 = client.get("/cargo/G1/report").json()
    rep2 = client.get("/cargo/G2/report").json()
    assert len(rep1["excursions"]) == 1 and rep1["excursions"][0]["is_open"] is True
    assert rep2["excursions"] == []


def test_own_measurement_takes_precedence_but_raw_reading_queryable(client: TestClient):
    make_cargo(client, "G1")
    register_trip(
        client, "V1", [{"cargo_id": "G1", "loaded_at": T0.format(0)}]
    )
    # 同刻：车载 9℃ 越温 vs 自有 3℃ 不越温 -> 采用自有 3℃，不开启区间
    reading(client, "r1", "V1", 5, 9)
    assert meas(client, "m1", "G1", 5, 3).status_code == 201

    # 货物事件列表只列自有事件
    evs = client.get("/cargo/G1/events").json()
    assert [e["event_id"] for e in evs] == ["m1"]

    # 合并时间线只含自有值，来源标记 own
    tl = client.get("/cargo/G1/temperatures").json()["readings"]
    assert [(p["source"], p["event_id"], str(p["temperature"])) for p in tl] == [
        ("own", "m1", "3.000")
    ]
    assert client.get("/cargo/G1/report").json()["excursions"] == []

    # 车载原始读数仍可查：applies_to 标记未采用并给出遮蔽它的自有事件
    item = client.get("/trips/V1/readings").json()["readings"][0]
    a = item["applies_to"][0]
    assert a["adopted"] is False
    assert a["own_event_id"] == "m1"
    assert a["is_over_limit"] is True  # 仍按原始读数对该货物上限判越温（供溯源）


def test_handover_only_changed_by_own_events(client: TestClient):
    # 车载读数开启越温，随后自有交接改变保管方并按交接时刻拆分责任
    make_cargo(client, "G1", holder="A仓库")
    register_trip(
        client, "V1", [{"cargo_id": "G1", "loaded_at": T0.format(0)}]
    )
    reading(client, "r1", "V1", 5, 8)          # 车载开启越温：A 担责
    assert handover(client, "h1", "G1", 20, "A仓库", "B仓库").status_code == 201
    assert meas(client, "m1", "G1", 40, 2).status_code == 201  # 自有测温闭合

    ex = client.get("/cargo/G1/report").json()["excursions"][0]
    assert ex["opener_source"] == "vehicle"
    assert ex["start_reading_id"] == "r1"
    assert ex["interval_id"] == "vehicle:r1"
    assert ex["closer_source"] == "own"
    assert ex["end_event_id"] == "m1"
    assert [(s["responsible_party"]) for s in ex["segments"]] == ["A仓库", "B仓库"]


def test_vehicle_reading_late_arrival_rebuilds(client: TestClient):
    # 先有自有闭合链，后到的车载读数插到中间开启新区间
    make_cargo(client, "G1")
    assert meas(client, "m1", "G1", 40, 2).status_code == 201
    assert client.get("/cargo/G1/report").json()["excursions"] == []

    register_trip(
        client, "V1", [{"cargo_id": "G1", "loaded_at": T0.format(0)}]
    )
    assert reading(client, "r-late", "V1", 10, 9).status_code == 201

    tl = client.get("/cargo/G1/temperatures").json()["readings"]
    assert [
        (p.get("reading_id") or p.get("event_id"))
        for p in tl
    ] == ["r-late", "m1"]
    ex = client.get("/cargo/G1/report").json()["excursions"][0]
    assert ex["start_reading_id"] == "r-late"
    assert ex["end_event_id"] == "m1"


def test_reading_idempotency_conflict_and_same_time(client: TestClient):
    make_cargo(client, "G1")
    register_trip(client, "V1", [{"cargo_id": "G1", "loaded_at": T0.format(0)}])

    r1 = reading(client, "r1", "V1", 5, 8)
    assert r1.status_code == 201
    # 同编号同内容重传 -> 200 replayed
    r2 = reading(client, "r1", "V1", 5, 8)
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    # 同编号异内容 -> 409
    r3 = reading(client, "r1", "V1", 6, 8)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "reading_id_conflict"
    # 同车次同刻不同编号 -> 409
    r4 = reading(client, "r2", "V1", 5, 8)
    assert r4.status_code == 409
    assert r4.json()["error"]["code"] == "same_timestamp_conflict"

    # 车次不存在 -> 404，不留读数
    before = db_rows()
    r5 = reading(client, "r9", "NOPE", 5, 8)
    assert r5.status_code == 404
    assert r5.json()["error"]["code"] == "trip_not_found"
    assert db_rows() == before


# ----------------------------------------------------------- 责任确认（车载区间）


def _vehicle_closed_interval(client, cargo="G1"):
    make_cargo(client, cargo, holder="A仓库")
    register_trip(
        client, "V1", [{"cargo_id": cargo, "loaded_at": T0.format(0)}]
    )
    reading(client, "r1", "V1", 10, 8)
    handover(client, "h1", cargo, 20, "A仓库", "B仓库")
    reading(client, "r2", "V1", 40, 2)  # 车载读数闭合
    iv = client.get(f"/cargo/{cargo}/responsibilities").json()["intervals"][0]
    return iv


def test_vehicle_opened_interval_responsibility_shape(client: TestClient):
    iv = _vehicle_closed_interval(client)
    assert iv["interval_id"] == "vehicle:r1"
    assert iv["opener_source"] == "vehicle"
    assert iv["closer_source"] == "vehicle"
    assert iv["start_reading_id"] == "r1"
    assert iv["end_reading_id"] == "r2"
    assert iv["start_trip_id"] == "V1"
    assert iv["start_event_id"] is None
    assert [(s["responsible_party"]) for s in iv["segments"]] == ["A仓库", "B仓库"]


def test_stance_on_vehicle_interval_and_late_rebuild_invalidation(client: TestClient):
    iv = _vehicle_closed_interval(client)
    iid, snap = iv["interval_id"], iv["snapshot_id"]

    # B 确认（车载区间上 B 的片段 [00:20,00:40)）
    r = client.post(
        f"/cargo/G1/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap, "responsible_party": "B仓库",
            "stance": "confirm", "stance_id": "st-b1",
        },
    )
    assert r.status_code == 201, r.text

    # 晚到自有交接（00:30，B仓库->C仓库）重建：B 的片段被切短 -> 旧表态失效
    handover(client, "h-late", "G1", 30, "B仓库", "C仓库")
    iv2 = client.get(f"/cargo/G1/responsibilities/{iid}")
    assert iv2.status_code == 200
    assert iv2.json()["snapshot_id"] != snap
    parties = [s["responsible_party"] for s in iv2.json()["segments"]]
    assert parties == ["A仓库", "B仓库", "C仓库"]

    # 用旧快照再表态 -> 409 stale_snapshot
    r2 = client.post(
        f"/cargo/G1/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap, "responsible_party": "C仓库",
            "stance": "confirm", "stance_id": "st-c1",
        },
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "stale_snapshot"

    # 历史仍可查，B 的旧表态所在片段已变化 -> is_current=false
    hist = client.get(f"/cargo/G1/responsibilities/{iid}/stances").json()
    assert hist["interval_exists"] is True
    item = hist["history"][0]
    assert item["stance_id"] == "st-b1"
    assert item["is_current"] is False

    # 旧表态同编号同内容重传仍幂等返回原记录（即使快照已失效）
    r3 = client.post(
        f"/cargo/G1/responsibilities/{iid}/stances",
        json={
            "snapshot_id": snap, "responsible_party": "B仓库",
            "stance": "confirm", "stance_id": "st-b1",
        },
    )
    assert r3.status_code == 200 and r3.json()["replayed"] is True


def test_vehicle_interval_open_not_listed(client: TestClient):
    make_cargo(client, "G1")
    register_trip(client, "V1", [{"cargo_id": "G1", "loaded_at": T0.format(0)}])
    reading(client, "r1", "V1", 10, 8)  # 一直越温，开放区间
    assert client.get("/cargo/G1/responsibilities").json()["intervals"] == []
    r = client.get("/cargo/G1/responsibilities/vehicle:r1")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "interval_open"
