"""并发写入测试：同一货物的多笔并发交接必须串行化，保管链不得损坏。

本用例需要“文件型 SQLite（每线程独立连接）”的引擎，运行方式：

    DATABASE_URL="sqlite:////tmp/coldchain_conc.db" pytest tests/test_concurrency.py

（默认的内存 StaticPool 配置下本用例自动跳过。）
"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app

pytestmark = pytest.mark.skipif(
    engine.pool.__class__.__name__ == "StaticPool",
    reason="StaticPool（内存库）不支持多线程并发；"
    "请用文件型 sqlite 单独运行："
    "DATABASE_URL='sqlite:////tmp/x.db' pytest tests/test_concurrency.py",
)


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c
    Base.metadata.drop_all(bind=engine)


def test_concurrent_handovers_preserve_chain(client):
    client.post(
        "/cargo",
        json={
            "cargo_id": "CC",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )

    def submit(i):
        # 所有线程都提交“同一时刻、A->Pi”的交接：货物行锁使其串行执行，
        # 只有第一笔成功；其余要么交出方失配（保管方已变更），
        # 要么触发同一时刻交接唯一约束
        return client.post(
            "/events",
            json={
                "event_id": f"hc{i}",
                "cargo_id": "CC",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "giver": "A",
                "receiver": f"P{i}",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409, 422) for c in codes if c != 201)
    report = client.get("/cargo/CC/report").json()
    # 保管链只前进一次，当前保管方是唯一成功那笔的接收方
    assert report["current_holder"].startswith("P")


def test_concurrent_stance_submits_serialize_with_snapshot(client):
    """表态并发提交：同责任方相同内容只有第一笔 201，其余按幂等返回 200。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CS",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post("/events", json={
        "event_id": "m1", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:10:00+00:00", "temperature": "8"})
    client.post("/events", json={
        "event_id": "m2", "cargo_id": "CS",
        "occurred_at": "2026-01-01T10:20:00+00:00", "temperature": "4"})
    snap = client.get("/cargo/CS/responsibilities").json()["intervals"][0]["snapshot_id"]

    def submit(i):
        return client.post(
            "/cargo/CS/responsibilities/m1/stances",
            json={
                "snapshot_id": snap,
                "responsible_party": "A",
                "stance": "confirm",
                # 所有线程同一编号同一内容：行锁串行后表现为 1 次写入 + 其余幂等
                "stance_id": "same-stance",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert codes.count(200) == 7
    hist = client.get("/cargo/CS/responsibilities/m1/stances").json()
    assert len(hist["history"]) == 1


def test_concurrent_overlapping_trip_registration_serializes(client):
    """同一货物并发登记两个重叠在车时段的车次：货物行锁串行，至多一笔成功。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CT",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )

    def submit(i):
        # 两个车次都想在 11:00~12:00 装载同一货物 CT：时段重叠
        return client.post(
            "/trips",
            json={
                "trip_id": f"tv{i}",
                "cargos": [
                    {
                        "cargo_id": "CT",
                        "loaded_at": "2026-01-01T11:00:00+00:00",
                        "unloaded_at": "2026-01-01T12:00:00+00:00",
                    }
                ],
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409, 422) for c in codes if c != 201)
    windows = client.get("/cargo/CT/temperatures")  # 仅确认服务可用
    assert windows.status_code == 200
    trips = client.get("/trips").json()
    assert len(trips) == 1  # 失败的登记整体回滚，不留车次


def test_concurrent_vehicle_readings_same_timestamp(client):
    """同车次同一时刻的并发车载读数：车次锁 + 唯一索引，至多一笔成功。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CR",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    r = client.post(
        "/trips",
        json={
            "trip_id": "TR1",
            "cargos": [
                {"cargo_id": "CR", "loaded_at": "2026-01-01T10:00:00+00:00"}
            ],
        },
    )
    assert r.status_code == 201

    def submit(i):
        return client.post(
            "/vehicle-readings",
            json={
                "reading_id": f"rv{i}",
                "trip_id": "TR1",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "8",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(submit, range(8)))

    assert codes.count(201) == 1
    assert all(c in (409,) for c in codes if c != 201)
    readings = client.get("/trips/TR1/readings").json()["readings"]
    assert len(readings) == 1


def test_concurrent_reading_and_event_serialize(client):
    """车载读数与货物自有事件并发写同刻：货物行锁串行，结果一致可重建。"""
    client.post(
        "/cargo",
        json={
            "cargo_id": "CX",
            "started_at": "2026-01-01T10:00:00+00:00",
            "temp_upper_limit": "5",
            "initial_holder": "A",
        },
    )
    client.post(
        "/trips",
        json={
            "trip_id": "TX1",
            "cargos": [
                {"cargo_id": "CX", "loaded_at": "2026-01-01T10:00:00+00:00"}
            ],
        },
    )

    def submit_reading():
        return client.post(
            "/vehicle-readings",
            json={
                "reading_id": "rx",
                "trip_id": "TX1",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "9",
            },
        ).status_code

    def submit_event():
        return client.post(
            "/events",
            json={
                "event_id": "mx",
                "cargo_id": "CX",
                "occurred_at": "2026-01-01T11:00:00+00:00",
                "temperature": "2",
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda f: f(), [submit_reading, submit_event]))

    # 两笔都成功（读数与自有事件不互斥），但合并时间线只采用自有值
    assert sorted(codes) == [201, 201]
    tl = client.get("/cargo/CX/temperatures").json()["readings"]
    assert len(tl) == 1
    assert tl[0]["source"] == "own"
    # 车载原始读数仍可查，标记为未采用
    item = client.get("/trips/TX1/readings").json()["readings"][0]
    assert item["applies_to"][0]["adopted"] is False
