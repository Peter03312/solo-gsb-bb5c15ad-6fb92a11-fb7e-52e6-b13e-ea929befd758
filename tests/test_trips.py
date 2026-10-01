"""车次 / 多票货物装载 / 车载读数 API 集成测试。

覆盖：
* 车次登记、车次详情（原始读数始终可查）；
* 装卸时段左闭右开、起点必须早于终点、车次/货物不存在整体拒绝不留部分记录；
* 同货物不得同时属于两个车次（相交拒绝、首尾相接允许）；
* 车载读数仅作用于读数时刻仍在车上的货物、按各自温度上限独立判越温；
* 同刻货物自有测温优先（车载原始读数仍可查、追责时间线不采用）；
* 交接仍只由货物自有事件改变保管方；
* 读数允许晚到；编号同内容重传幂等、异内容冲突、同车次同刻冲突；
* 车载读数开启的区间有稳定标识 onboard:<reading_id>，可查询可表态，
  旧表态沿用逐片段有效性与过期快照规则（晚到自有测温顶替开启点后旧表态失效）。
"""
from tests.conftest import event_ids_for


def make_cargo(client, cargo_id, limit="4", holder="A仓库",
               start="2026-10-01T00:00:00+00:00"):
    r = client.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def make_trip(client, trip_id="T1", vehicle="京A-0001", note=None):
    r = client.post(
        "/trips",
        json={"trip_id": trip_id, "vehicle_no": vehicle, "note": note},
    )
    assert r.status_code == 201, r.text
    return r.json()


def loadings(client, trip_id, entries):
    return client.post(
        f"/trips/{trip_id}/loadings", json={"loadings": entries}
    )

def loading(cid, lh, uh, day="2026-10-01"):
    return {
        "cargo_id": cid,
        "loaded_at": f"{day}T{lh:02d}:00:00+00:00",
        "unloaded_at": f"{day}T{uh:02d}:00:00+00:00",
    }


def reading(client, rid, trip_id, hhmm, temp, day="2026-10-01"):
    h, m = hhmm
    return client.post(
        f"/trips/{trip_id}/readings",
        json={
            "reading_id": rid,
            "trip_id": trip_id,
            "read_at": f"{day}T{h:02d}:{m:02d}:00+00:00",
            "temperature": str(temp),
        },
    )


def own_meas(client, eid, cid, hhmm, temp, day="2026-10-01"):
    h, m = hhmm
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cid,
            "occurred_at": f"{day}T{h:02d}:{m:02d}:00+00:00",
            "temperature": str(temp),
        },
    )


def own_hand(client, eid, cid, hhmm, giver, receiver, day="2026-10-01"):
    h, m = hhmm
    return client.post(
        "/events",
        json={
            "event_id": eid,
            "cargo_id": cid,
            "occurred_at": f"{day}T{h:02d}:{m:02d}:00+00:00",
            "giver": giver,
            "receiver": receiver,
        },
    )


# ----------------------------------------------------------- 车次


def test_create_and_get_trip(client):
    make_trip(client, "T1", note="京沪线")
    r = client.get("/trips/T1")
    assert r.status_code == 200
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["vehicle_no"] == "京A-0001"
    assert body["note"] == "京沪线"
    assert body["loadings"] == []
    assert body["readings"] == []
    assert [t["trip_id"] for t in client.get("/trips").json()] == ["T1"]


def test_duplicate_trip_rejected(client):
    make_trip(client, "T1")
    r = client.post("/trips", json={"trip_id": "T1", "vehicle_no": "x"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "trip_exists"


def test_get_unknown_trip_404(client):
    assert client.get("/trips/NOPE").status_code == 404
    assert client.get("/trips/NOPE/readings").status_code == 404


# ----------------------------------------------------------- 装载时段校验


def test_loading_batch_success_multi_cargo(client):
    make_cargo(client, "G1")
    make_cargo(client, "G2")
    make_trip(client, "T1")
    r = loadings(client, "T1", [loading("G1", 2, 10), loading("G2", 3, 9)])
    assert r.status_code == 201, r.text
    assert [l["cargo_id"] for l in r.json()["loadings"]] == ["G1", "G2"]


def test_loading_window_must_be_left_closed_right_open_with_start_before_end(client):
    make_cargo(client, "G1")
    make_trip(client, "T1")
    # 起点等于终点
    r = loadings(client, "T1", [loading("G1", 5, 5)])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_loading_window"
    # 起点晚于终点
    r2 = loadings(client, "T1", [
        {"cargo_id": "G1",
         "loaded_at": "2026-10-01T06:00:00Z",
         "unloaded_at": "2026-10-01T05:00:00Z"}])
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "invalid_loading_window"
    # 不留任何记录
    assert client.get("/trips/T1").json()["loadings"] == []


def test_loading_unknown_trip_or_cargo_rejected_entire_batch(client):
    make_cargo(client, "G1")
    make_cargo(client, "G2")
    make_trip(client, "T1")
    # 未知车次
    r = loadings(client, "NOPE", [loading("G1", 2, 3)])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"
    # 批次中一票货物不存在 -> 整批拒绝，不留部分记录
    r2 = loadings(client, "T1", [loading("G1", 2, 3), loading("NOPE", 2, 3)])
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "cargo_not_found"
    assert client.get("/trips/T1").json()["loadings"] == []


def test_empty_batch_rejected(client):
    make_trip(client, "T1")
    r = client.post("/trips/T1/loadings", json={"loadings": []})
    # min_length=1 在请求模型层拒绝（422）
    assert r.status_code == 422


def test_same_cargo_cannot_belong_to_two_trips_at_once(client):
    make_cargo(client, "G1")
    make_trip(client, "T1")
    make_trip(client, "T2")
    assert loadings(client, "T1", [loading("G1", 2, 10)]).status_code == 201
    # 与 T1 时段相交
    r = loadings(client, "T2", [loading("G1", 9, 11)])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "loading_overlap"
    # 同车次同货物重复登记
    r2 = loadings(client, "T1", [loading("G1", 11, 12)])
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "loading_overlap"
    # 首尾相接允许（上一段终点 10 == 下一段起点 10）：读数时刻 10 已不在 T1
    r3 = loadings(client, "T2", [loading("G1", 10, 12)])
    assert r3.status_code == 201


def test_batch_partial_invalid_leaves_no_partial_records(client):
    make_cargo(client, "G1")
    make_cargo(client, "G2")
    make_trip(client, "T1")
    assert loadings(client, "T1", [loading("G1", 2, 10)]).status_code == 201
    # G2 合法但 G1 与既有重叠 -> 整批回滚，G2 也不留记录
    r = loadings(client, "T1", [
        loading("G2", 2, 3),
        loading("G1", 5, 6),
    ])
    assert r.status_code == 409
    detail = client.get("/trips/T1").json()
    assert [l["cargo_id"] for l in detail["loadings"]] == ["G1"]


# ----------------------------------------------------------- 读数作用范围


def _setup_two_cargo_trip(client, lim1="4", lim2="7"):
    make_cargo(client, "G1", limit=lim1, holder="A")
    make_cargo(client, "G2", limit=lim2, holder="X")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10), loading("G2", 3, 9)])


def test_onboard_reading_applies_only_to_cargo_still_on_board(client):
    _setup_two_cargo_trip(client)
    # 01:00 无人上车（读数仍保存为原始数据）
    assert reading(client, "r0", "T1", (1, 0), 9).status_code == 201
    # 04:00 G1/G2 都在车上
    assert reading(client, "r1", "T1", (4, 0), 8).status_code == 201
    # 08:00 恢复
    assert reading(client, "r2", "T1", (8, 0), 3).status_code == 201
    # 09:30 G2 已在 09:00 卸载，G1 仍在车上
    assert reading(client, "r3", "T1", (9, 30), 9).status_code == 201

    r1 = client.get("/cargo/G1/readings").json()["readings"]
    by_id = {r["reading_id"]: r for r in r1}
    assert by_id["r0"]["adopted"] is False
    assert by_id["r0"]["not_adopted_reason"] == "not_on_board"
    assert by_id["r1"]["adopted"] is True
    assert by_id["r3"]["adopted"] is True  # G1 10:00 才卸载

    r2 = {r["reading_id"]: r for r in client.get("/cargo/G2/readings").json()["readings"]}
    assert r2["r1"]["adopted"] is True
    assert r2["r3"]["adopted"] is False
    assert r2["r3"]["not_adopted_reason"] == "not_on_board"

    # 原始读数始终可查（车次接口），无论是否被采用
    raw = [r["reading_id"] for r in client.get("/trips/T1/readings").json()]
    assert raw == ["r0", "r1", "r2", "r3"]


def test_each_cargo_judged_by_its_own_limit(client):
    # 读数 6℃：G1 上限 4 -> 越温；G2 上限 7 -> 不越温
    _setup_two_cargo_trip(client, lim1="4", lim2="7")
    reading(client, "r1", "T1", (4, 0), 6)
    reading(client, "r2", "T1", (8, 0), 2)
    ex1 = client.get("/cargo/G1/report").json()["excursions"]
    ex2 = client.get("/cargo/G2/report").json()["excursions"]
    assert len(ex1) == 1 and ex1[0]["interval_id"] == "onboard:r1"
    assert ex2 == []


def test_onboard_interval_boundary_is_half_open_at_unload(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    # 09:59 超限 -> 开放区间
    reading(client, "r1", "T1", (9, 59), 9)
    ex = client.get("/cargo/G1/report").json()["excursions"][0]
    assert ex["is_open"] is True
    # 10:00 的读数（G1 已卸载，右开）不闭合 G1 的区间，也不作用于 G1
    reading(client, "r2", "T1", (10, 0), 2)
    g1 = {r["reading_id"]: r for r in client.get("/cargo/G1/readings").json()["readings"]}
    assert g1["r2"]["adopted"] is False
    assert g1["r2"]["not_adopted_reason"] == "not_on_board"
    ex2 = client.get("/cargo/G1/report").json()["excursions"][0]
    assert ex2["is_open"] is True


def test_own_measurement_takes_priority_at_same_instant(client):
    # G1 上限 5：同刻车载 8℃ 越温，但自有测温 5℃（恰为上限）不越温 -> 区间不开启
    make_cargo(client, "G1", limit="5", holder="A")
    make_cargo(client, "G2", limit="7", holder="X")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10), loading("G2", 3, 9)])
    reading(client, "r1", "T1", (4, 0), 8)
    own_meas(client, "e1", "G1", (4, 0), 5)
    reading(client, "r2", "T1", (8, 0), 2)

    g1 = {(r.get("event_id") or r.get("reading_id")): r
          for r in client.get("/cargo/G1/readings").json()["readings"]}
    assert g1["e1"]["source"] == "own" and g1["e1"]["adopted"] is True
    assert g1["r1"]["source"] == "onboard" and g1["r1"]["adopted"] is False
    assert g1["r1"]["not_adopted_reason"] == "own_measurement_priority"

    # 自有值优先：5℃ == 上限不越温，G1 无越温区间（若误用车载 8℃ 则会开启）
    assert client.get("/cargo/G1/report").json()["excursions"] == []
    # 车载原始读数仍可查
    assert [r["reading_id"] for r in client.get("/trips/T1/readings").json()] == ["r1", "r2"]

    # G2 无同刻自有测温，r1 被采用，8℃ > 上限 7 开启车载区间
    g2 = {r["reading_id"]: r for r in client.get("/cargo/G2/readings").json()["readings"]}
    assert g2["r1"]["adopted"] is True
    ex2 = client.get("/cargo/G2/report").json()["excursions"]
    assert [x["interval_id"] for x in ex2] == ["onboard:r1"]


def test_handovers_only_change_holder_via_own_events(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    reading(client, "r1", "T1", (4, 0), 8)
    own_hand(client, "h1", "G1", (5, 0), "A", "B")
    reading(client, "r2", "T1", (8, 0), 2)
    rep = client.get("/cargo/G1/report").json()
    assert rep["current_holder"] == "B"
    seg = rep["excursions"][0]["segments"]
    assert [s["responsible_party"] for s in seg] == ["A", "B"]


def test_own_events_list_only_lists_own_events(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    reading(client, "r1", "T1", (4, 0), 8)
    own_meas(client, "e1", "G1", (6, 0), 9)
    ids = [e["event_id"] for e in client.get("/cargo/G1/events").json()]
    assert ids == ["e1"]  # 车载读数不出现在货物自有事件列表


# ----------------------------------------------------------- 读数幂等 / 冲突


def test_reading_idempotent_replay_and_conflict(client):
    make_cargo(client, "G1")
    make_trip(client, "T1")
    body = {"reading_id": "r1", "trip_id": "T1",
            "read_at": "2026-10-01T04:00:00Z", "temperature": "8"}
    assert client.post("/trips/T1/readings", json=body).status_code == 201
    r = client.post("/trips/T1/readings", json=body)
    assert r.status_code == 200 and r.json()["replayed"] is True
    # 同时刻不同时区写法仍视为同内容
    body2 = dict(body, read_at="2026-10-01T12:00:00+08:00")
    assert client.post("/trips/T1/readings", json=body2).status_code == 200
    assert len(client.get("/trips/T1/readings").json()) == 1
    # 异内容冲突
    body3 = dict(body, temperature="9")
    r3 = client.post("/trips/T1/readings", json=body3)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "reading_id_conflict"


def test_same_trip_same_instant_reading_conflict(client):
    make_cargo(client, "G1")
    make_trip(client, "T1")
    reading(client, "r1", "T1", (4, 0), 8)
    r = reading(client, "r2", "T1", (4, 0), 7)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "same_timestamp_conflict"


def test_reading_unknown_trip_404(client):
    r = client.post(
        "/trips/NOPE/readings",
        json={"reading_id": "r1", "trip_id": "NOPE",
              "read_at": "2026-10-01T04:00:00Z", "temperature": "8"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "trip_not_found"


def test_path_trip_must_match_body(client):
    make_trip(client, "T1")
    r = client.post(
        "/trips/T1/readings",
        json={"reading_id": "r1", "trip_id": "T2",
              "read_at": "2026-10-01T04:00:00Z", "temperature": "8"},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_reading_shape"


def test_late_reading_recomputes_timeline(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    # 先写恢复读数，再晚到开启读数 -> 区间重算出现
    reading(client, "r2", "T1", (8, 0), 2)
    assert client.get("/cargo/G1/report").json()["excursions"] == []
    reading(client, "r1", "T1", (4, 0), 9)
    rep = client.get("/cargo/G1/report").json()
    assert len(rep["excursions"]) == 1
    assert rep["excursions"][0]["interval_id"] == "onboard:r1"


def test_late_loading_makes_reading_apply(client):
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    reading(client, "r1", "T1", (4, 0), 9)
    # 尚未登记装载：读数不作用于任何货物
    assert client.get("/cargo/G1/readings").json()["readings"] == []
    # 补登记装载时段覆盖 04:00（晚到）-> 读数开始作用
    assert loadings(client, "T1", [loading("G1", 2, 10)]).status_code == 201
    rep = client.get("/cargo/G1/report").json()
    assert rep["excursions"][0]["interval_id"] == "onboard:r1"


# ------------------------------------------- 车载区间的责任确认 / 表态


def _onboard_excursion(client, holder=("A", "B"), split=(5, 0)):
    make_cargo(client, "G1", holder=holder[0])
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    reading(client, "r1", "T1", (4, 0), 8)
    own_hand(client, "h1", "G1", split, holder[0], holder[1])
    reading(client, "r2", "T1", (8, 0), 2)
    return client.get("/cargo/G1/responsibilities").json()["intervals"][0]


def test_onboard_opened_interval_has_stable_id_and_source(client):
    iv = _onboard_excursion(client)
    assert iv["interval_id"] == "onboard:r1"
    assert iv["start_event_id"] == "r1"
    assert iv["end_event_id"] == "r2"
    assert iv["start_source"] == "onboard"
    assert iv["end_source"] == "onboard"
    assert iv["start_trip_id"] == "T1" and iv["end_trip_id"] == "T1"
    assert [s["responsible_party"] for s in iv["segments"]] == ["A", "B"]
    assert iv["snapshot_id"].startswith("sha256:")


def test_stance_on_onboard_interval_and_history(client):
    iv = _onboard_excursion(client)
    iid, snap = iv["interval_id"], iv["snapshot_id"]
    r = client.post(
        f"/cargo/G1/responsibilities/{iid}/stances",
        json={"snapshot_id": snap, "responsible_party": "B",
              "stance": "confirm", "stance_id": "s1"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["interval_id"] == "onboard:r1"
    # 直查
    assert client.get(f"/cargo/G1/responsibilities/{iid}").status_code == 200
    hist = client.get(f"/cargo/G1/responsibilities/{iid}/stances").json()
    assert hist["interval_exists"] is True
    assert hist["history"][0]["is_current"] is True
    # 汇总
    iv2 = client.get("/cargo/G1/responsibilities").json()["intervals"][0]
    assert iv2["summary"] == "pending"


def test_stale_snapshot_on_onboard_interval_rejected(client):
    iv = _onboard_excursion(client)
    stale = iv["snapshot_id"]
    # 晚到一笔合法交接 B->C 改变片段
    own_hand(client, "h2", "G1", (6, 0), "B", "C")
    r = client.post(
        "/cargo/G1/responsibilities/onboard:r1/stances",
        json={"snapshot_id": stale, "responsible_party": "A",
              "stance": "confirm", "stance_id": "s1"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stale_snapshot"


def test_late_own_opener_replaces_onboard_interval_and_invalidates_stance(client):
    iv = _onboard_excursion(client)
    iid, snap = iv["interval_id"], iv["snapshot_id"]
    client.post(
        f"/cargo/G1/responsibilities/{iid}/stances",
        json={"snapshot_id": snap, "responsible_party": "B",
              "stance": "confirm", "stance_id": "s1"},
    )
    # 晚到一笔更早（03:00）的自有超限测温 -> 开启点变为自有事件 e0
    own_meas(client, "e0", "G1", (3, 0), 9)
    intervals = client.get("/cargo/G1/responsibilities").json()["intervals"]
    assert [x["interval_id"] for x in intervals] == ["e0"]
    assert intervals[0]["start_source"] == "own"
    # 旧车载区间标识不再存在；旧表态失效但历史可查
    assert client.get("/cargo/G1/responsibilities/onboard:r1").status_code == 404
    hist = client.get("/cargo/G1/responsibilities/onboard:r1/stances").json()
    assert hist["interval_exists"] is False
    assert hist["current_snapshot_id"] is None
    assert hist["history"][0]["is_current"] is False


def test_mixed_source_open_own_close_onboard(client):
    # 自有测温开启、车载读数闭合：end_source=onboard
    make_cargo(client, "G1", holder="A")
    make_trip(client, "T1")
    loadings(client, "T1", [loading("G1", 2, 10)])
    own_meas(client, "e1", "G1", (4, 0), 9)
    reading(client, "r2", "T1", (8, 0), 2)
    iv = client.get("/cargo/G1/report").json()["excursions"][0]
    assert iv["interval_id"] == "e1"
    assert iv["start_source"] == "own"
    assert iv["end_source"] == "onboard"
    assert iv["end_event_id"] == "r2"
    # 责任确认可按自有事件ID表态
    r = client.get("/cargo/G1/responsibilities").json()["intervals"][0]
    assert r["interval_id"] == "e1" and r["end_trip_id"] == "T1"


def test_reading_before_cargo_start_not_adopted(client):
    make_cargo(client, "G1", start="2026-10-01T05:00:00+00:00")
    make_trip(client, "T1")
    loadings(client, "T1", [
        {"cargo_id": "G1",
         "loaded_at": "2026-10-01T02:00:00Z",
         "unloaded_at": "2026-10-01T10:00:00Z"}])
    reading(client, "r0", "T1", (4, 0), 9)
    rv = {r["reading_id"]: r for r in client.get("/cargo/G1/readings").json()["readings"]}
    assert rv["r0"]["adopted"] is False
    assert rv["r0"]["not_adopted_reason"] == "before_cargo_start"
    assert client.get("/cargo/G1/report").json()["excursions"] == []
