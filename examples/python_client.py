"""冷链追责服务 Python 调用示例（仅依赖 httpx）。

运行：python examples/python_client.py
默认地址 http://127.0.0.1:8000，可用环境变量 BASE_URL 覆盖。
"""
import os

import httpx

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000")
CARGO = "COLD-DEMO-2"


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n== {title} [{resp.status_code}] ==")
    print(resp.text)


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as api:
        show(
            "创建货物",
            api.post(
                "/cargo",
                json={
                    "cargo_id": CARGO,
                    "started_at": "2026-09-30T08:00:00+08:00",
                    "temp_upper_limit": "4.0",
                    "initial_holder": "北京冷库",
                },
            ),
        )

        # 测温：温度高于上限 -> 开启越温区间
        show(
            "超限测温",
            api.post(
                "/events",
                json={
                    "event_id": "p-t1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T10:00:00+08:00",
                    "temperature": "8.2",
                },
            ),
        )

        # 交接：必须带交出方与接收方；交出方须为当时保管方
        show(
            "合法交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "p-h1",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T12:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 错误交出方 -> 422，整体拒绝
        show(
            "非法交接（交出方不是保管方，拒绝）",
            api.post(
                "/events",
                json={
                    "event_id": "p-bad",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "广州冷库",
                },
            ),
        )

        # 不超限测温 -> 闭合区间
        show(
            "恢复测温（闭合区间）",
            api.post(
                "/events",
                json={
                    "event_id": "p-t2",
                    "cargo_id": CARGO,
                    "occurred_at": "2026-09-30T15:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        show("追责报告", api.get(f"/cargo/{CARGO}/report"))
        show("事件列表（按时间）", api.get(f"/cargo/{CARGO}/events"))

        # ----- 责任确认（仅针对已闭合越温区间）-----
        resp = api.get(f"/cargo/{CARGO}/responsibilities")
        show("责任确认：已闭合区间列表", resp)
        intervals = resp.json().get("intervals", [])
        if not intervals:
            print("（没有已闭合越温区间，跳过责任确认演示）")
            return

        interval = intervals[0]
        iid = interval["interval_id"]       # 区间标识 = 开启测温事件ID
        snap = interval["snapshot_id"]      # 当前快照标识（区间起止 + 责任片段）
        print(f"区间标识={iid} 结束测温事件={interval['end_event_id']} "
              f"快照={snap} 初始汇总={interval['summary']}")

        # 起始保管方确认担责
        show(
            "北京冷库 确认",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 异议必须带理由；忘记理由 -> 422 dispute_reason_required
        show(
            "上海冷库 异议但未带理由（拒绝示例）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "stance_id": "py-stance-bad",
                },
            ),
        )

        # 合法异议
        show(
            "上海冷库 异议（带理由）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "dispute",
                    "reason": "温控参数由交出方远程设定，接收方未实际接管",
                    "stance_id": "py-stance-2",
                },
            ),
        )

        show(
            "区间责任确认视图（此时汇总 has_dispute）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # 同编号同内容重传 -> 200 replayed，返回原记录
        show(
            "表态幂等重传",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "北京冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-1",
                },
            ),
        )

        # 改口：新编号，历次表态保留
        show(
            "上海冷库 改口为确认（新编号）",
            api.post(
                f"/cargo/{CARGO}/responsibilities/{iid}/stances",
                json={
                    "snapshot_id": snap,
                    "responsible_party": "上海冷库",
                    "stance": "confirm",
                    "stance_id": "py-stance-3",
                },
            ),
        )
        show(
            "表态历史（历次表态，含 superseded/is_current 标记）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}/stances"),
        )
        show(
            "最终区间视图（汇总 all_confirmed）",
            api.get(f"/cargo/{CARGO}/responsibilities/{iid}"),
        )

        # ============== 本轮新增：车次 / 多票货物 / 车载读数 ==============
        c2, c3 = "COLD-TRIP-A", "COLD-TRIP-B"
        for cid, limit, holder in [
            (c2, "4.0", "北京冷库"),
            (c3, "6.0", "天津冷库"),
        ]:
            show(
                f"创建货物 {cid}（上限 {limit}℃）",
                api.post(
                    "/cargo",
                    json={
                        "cargo_id": cid,
                        "started_at": "2026-10-01T08:00:00+08:00",
                        "temp_upper_limit": limit,
                        "initial_holder": holder,
                    },
                ),
            )

        # 一次登记车次 + 每票货物的装卸时段（左闭右开；c3 暂未卸车）
        show(
            "登记车次（A 09:00-18:00；B 09:30 起仍在车上）",
            api.post(
                "/trips",
                json={
                    "trip_id": "TRIP-PY-1",
                    "truck_no": "京A-CC01",
                    "cargos": [
                        {
                            "cargo_id": c2,
                            "loaded_at": "2026-10-01T09:00:00+08:00",
                            "unloaded_at": "2026-10-01T18:00:00+08:00",
                        },
                        {"cargo_id": c3, "loaded_at": "2026-10-01T09:30:00+08:00"},
                    ],
                },
            ),
        )

        # 车载读数 4.5℃：A 上限 4 -> 越温；B 上限 6 -> 不越温（各自上限）
        show(
            "车载读数 10:00 4.5℃",
            api.post(
                "/vehicle-readings",
                json={
                    "reading_id": "rd-py-1",
                    "trip_id": "TRIP-PY-1",
                    "occurred_at": "2026-10-01T10:00:00+08:00",
                    "temperature": "4.5",
                },
            ),
        )

        # 同刻 A 有自有测温 3.0℃：A 采用自有值，车载读数被遮蔽（原始仍可查）
        show(
            "A 同刻自有测温 3.0℃（自有优先）",
            api.post(
                "/events",
                json={
                    "event_id": "a-own",
                    "cargo_id": c2,
                    "occurred_at": "2026-10-01T10:00:00+08:00",
                    "temperature": "3.0",
                },
            ),
        )

        # 12:00 车载 7.0℃：A、B 都在车上且越限，各自开启越温区间
        show(
            "车载读数 12:00 7.0℃（开启区间）",
            api.post(
                "/vehicle-readings",
                json={
                    "reading_id": "rd-py-2",
                    "trip_id": "TRIP-PY-1",
                    "occurred_at": "2026-10-01T12:00:00+08:00",
                    "temperature": "7.0",
                },
            ),
        )

        # A 的交接（保管方只由货物自有事件改变），责任在 13:00 拆分
        show(
            "A 交接 北京->上海",
            api.post(
                "/events",
                json={
                    "event_id": "a-hand",
                    "cargo_id": c2,
                    "occurred_at": "2026-10-01T13:00:00+08:00",
                    "giver": "北京冷库",
                    "receiver": "上海冷库",
                },
            ),
        )

        # 15:00 车载 2.0℃ 闭合区间（车载读数允许晚到：换个更早时刻补录也会重算）
        show(
            "车载读数 15:00 2.0℃（闭合区间）",
            api.post(
                "/vehicle-readings",
                json={
                    "reading_id": "rd-py-3",
                    "trip_id": "TRIP-PY-1",
                    "occurred_at": "2026-10-01T15:00:00+08:00",
                    "temperature": "2.0",
                },
            ),
        )

        show("车次读数（原始读数 + 每票货物采用/越限来源）",
             api.get("/trips/TRIP-PY-1/readings"))
        show("A 的采用测温时间线（自有优先）", api.get(f"/cargo/{c2}/temperatures"))
        show("A 的追责报告（区间标识 vehicle:rd-py-2）", api.get(f"/cargo/{c2}/report"))
        show("B 的追责报告（上限 6，rd-py-1 不越温）", api.get(f"/cargo/{c3}/report"))

        # 对车载读数开启的区间表态：区间稳定标识 vehicle:<reading_id>
        resp = api.get(f"/cargo/{c2}/responsibilities")
        intervals = resp.json().get("intervals", [])
        if intervals:
            iv = intervals[0]
            iid, snap = iv["interval_id"], iv["snapshot_id"]
            print(f"\n车载区间标识={iid} 开启来源={iv['opener_source']} 快照={snap}")
            show(
                "北京冷库对车载区间确认",
                api.post(
                    f"/cargo/{c2}/responsibilities/{iid}/stances",
                    json={
                        "snapshot_id": snap,
                        "responsible_party": "北京冷库",
                        "stance": "confirm",
                        "stance_id": "py-vehicle-stance-1",
                    },
                ),
            )

        # 非法：重叠在车时段 -> 409 cargo_already_on_trip（整体拒绝不留记录）
        show(
            "重叠在车时段登记另一车次（拒绝）",
            api.post(
                "/trips",
                json={
                    "trip_id": "TRIP-PY-BAD",
                    "cargos": [
                        {
                            "cargo_id": c2,
                            "loaded_at": "2026-10-01T10:00:00+08:00",
                            "unloaded_at": "2026-10-01T11:00:00+08:00",
                        }
                    ],
                },
            ),
        )
        # 非法：装载时段起点不早于终点 -> 422 invalid_loading_window
        show(
            "空装载时段（拒绝）",
            api.post(
                "/trips",
                json={
                    "trip_id": "TRIP-PY-BAD2",
                    "cargos": [
                        {
                            "cargo_id": c3,
                            "loaded_at": "2026-10-01T09:00:00+08:00",
                            "unloaded_at": "2026-10-01T09:00:00+08:00",
                        }
                    ],
                },
            ),
        )
        # 幂等：车载读数同编号同内容重传
        show(
            "车载读数幂等重传",
            api.post(
                "/vehicle-readings",
                json={
                    "reading_id": "rd-py-2",
                    "trip_id": "TRIP-PY-1",
                    "occurred_at": "2026-10-01T12:00:00+08:00",
                    "temperature": "7.0",
                },
            ),
        )


if __name__ == "__main__":
    main()
