#!/usr/bin/env bash
# 冷链追责服务 调用示例（curl）。
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_examples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
C="COLD-DEMO-1"

echo "1) 创建货物：编号 / 起始时间 / 温度上限 / 起始保管方"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C\",
  \"started_at\": \"2026-09-30T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo

echo "2) 测温（10:00，8.2℃ > 上限 4℃，开启越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "3) 交接（12:00，北京冷库 -> 上海冷库），区间在此刻拆分"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T12:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}"; echo

echo "4) 交接（14:00，上海冷库 -> 广州冷库）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-hand-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T14:00:00+08:00\",
  \"giver\": \"上海冷库\", \"receiver\": \"广州冷库\"
}"; echo

echo "5) 测温（16:00，3.1℃ <= 上限，闭合越温区间）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-2\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T16:00:00+08:00\", \"temperature\": \"3.1\"
}"; echo

echo "6) 幂等重传第 2) 步（相同内容返回 200 replayed，不重复写入）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-temp-1\", \"cargo_id\": \"$C\",
  \"occurred_at\": \"2026-09-30T10:00:00+08:00\", \"temperature\": \"8.2\"
}"; echo

echo "7) 追责报告：越温起止 + 各责任方片段（按时间）"
curl -sS "$BASE/cargo/$C/report"; echo

echo "8) 责任确认查询：已闭合区间（区间标识=开启测温事件ID）、结束事件ID、责任片段、当前快照标识、最新表态、汇总状态"
curl -sS "$BASE/cargo/$C/responsibilities"; echo

# 取首个已闭合区间的区间标识与当前快照标识（需系统带 python3；也可手工从上一步复制）
read -r IID SNAP < <(python3 - "$BASE" "$C" <<'PY'
import json, sys, urllib.request
base, cargo = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{base}/cargo/{cargo}/responsibilities") as r:
    iv = json.load(r)["intervals"][0]
print(iv["interval_id"], iv["snapshot_id"])
PY
)
echo "区间标识=$IID 快照标识=$SNAP"

echo "9) 北京冷库 确认担责"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-bj-1\"
  }"; echo

echo "10) 上海冷库 提出异议（异议必须带理由 reason）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"上海冷库\",
    \"stance\": \"dispute\",
    \"reason\": \"该时段冷机由北京冷库远程设定，上海冷库未接管温控\",
    \"stance_id\": \"stance-sh-1\"
  }"; echo

echo "11) 再次查询：汇总状态变为 has_dispute，各责任方返回最新一条有效表态"
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "12) 表态历史：改口用新编号，历次表态（含已改口/已失效）均可查"
curl -sS "$BASE/cargo/$C/responsibilities/$IID/stances"; echo

echo "13) 幂等：同编号同内容重传返回原记录（200 replayed，即使快照随后失效也如此）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-bj-1\"
  }"; echo

echo "14) 改口：上海冷库改为确认（必须使用新表态编号，携带新编号提交）"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"上海冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-sh-2\"
  }"; echo

echo "15) 最终查询：北京/上海均已确认；广州冷库尚未表态 -> 汇总仍为 pending（待确认）"
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "16) 广州冷库也确认 -> 汇总 all_confirmed"
curl -sS -X POST "$BASE/cargo/$C/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"广州冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-gz-1\"
  }" >/dev/null
curl -sS "$BASE/cargo/$C/responsibilities/$IID"; echo

echo "========================================================"
echo "本轮新增：同一冷链车承载多票货物 + 车载测温"
echo "========================================================"
V="TRUCK-DEMO-1"
C2="COLD-DEMO-A"
C3="COLD-DEMO-B"

echo "17) 创建两票货物（温度上限不同：A=4℃，B=6℃）"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C2\",
  \"started_at\": \"2026-10-01T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}" >/dev/null
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C3\",
  \"started_at\": \"2026-10-01T08:00:00+08:00\",
  \"temp_upper_limit\": \"6.0\",
  \"initial_holder\": \"天津冷库\"
}" >/dev/null
echo "created $C2 / $C3"

echo "18) 登记车次：两票货物各自的装卸时段（左闭右开；C3 暂未卸车 unloaded_at=null）"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$V\",
  \"truck_no\": \"京A-CC01\",
  \"cargos\": [
    {\"cargo_id\": \"$C2\", \"loaded_at\": \"2026-10-01T09:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T18:00:00+08:00\"},
    {\"cargo_id\": \"$C3\", \"loaded_at\": \"2026-10-01T09:30:00+08:00\"}
  ]
}"; echo

echo "19) 车载读数（10:00，4.5℃）：对 A（上限4）越温，对 B（上限6）不越温"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1\", \"trip_id\": \"$V\",
  \"occurred_at\": \"2026-10-01T10:00:00+08:00\", \"temperature\": \"4.5\"
}"; echo

echo "20) 同刻 A 有货物自有测温 3.0℃：A 采用自有值（车载读数被遮蔽但原始可查）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-a-own\", \"cargo_id\": \"$C2\",
  \"occurred_at\": \"2026-10-01T10:00:00+08:00\", \"temperature\": \"3.0\"
}" >/dev/null
echo "ok"

echo "21) 车载读数（12:00，7.0℃，晚到也允许）：A、B 均在车上且都越限"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-2\", \"trip_id\": \"$V\",
  \"occurred_at\": \"2026-10-01T12:00:00+08:00\", \"temperature\": \"7.0\"
}" >/dev/null
echo "ok"

echo "22) A 的交接（只由货物自有事件改变保管方）：13:00 北京冷库 -> 上海冷库"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"evt-a-hand\", \"cargo_id\": \"$C2\",
  \"occurred_at\": \"2026-10-01T13:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"上海冷库\"
}" >/dev/null
echo "ok"

echo "23) 车载读数（15:00，2.0℃）闭合 A/B 上由车载读数开启的越温"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-3\", \"trip_id\": \"$V\",
  \"occurred_at\": \"2026-10-01T15:00:00+08:00\", \"temperature\": \"2.0\"
}" >/dev/null
echo "ok"

echo "24) 车次读数查询：原始读数 + 各货物是否采用 / 是否越限（rd-1 对 A adopted=false）"
curl -sS "$BASE/trips/$V/readings" | python3 -m json.tool; echo

echo "25) A 的测温时间线（自有优先合并）；货物自有事件列表仍只有自有事件"
curl -sS "$BASE/cargo/$C2/temperatures" | python3 -m json.tool
curl -sS "$BASE/cargo/$C2/events" | python3 -m json.tool

echo "26) A 的追责报告：车载区间标识 vehicle:rd-2，按交接 13:00 拆分责任"
curl -sS "$BASE/cargo/$C2/report" | python3 -m json.tool

echo "27) 对车载读数开启的区间查询责任确认并表态（稳定标识 vehicle:rd-2）"
read -r IID2 SNAP2 < <(python3 - "$BASE" "$C2" <<'PY'
import json, sys, urllib.request
base, cargo = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{base}/cargo/{cargo}/responsibilities") as r:
    iv = json.load(r)["intervals"][0]
print(iv["interval_id"], iv["snapshot_id"])
PY
)
echo "车载区间标识=$IID2 快照=$SNAP2"
curl -sS "$BASE/cargo/$C2/responsibilities"; echo
curl -sS -X POST "$BASE/cargo/$C2/responsibilities/$IID2/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP2\",
    \"responsible_party\": \"北京冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-a-bj-1\"
  }"; echo

echo "28) 非法示例：重叠在车时段登记另一车次 -> 409 cargo_already_on_trip（整体不留记录）"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"TRUCK-BAD\",
  \"cargos\": [
    {\"cargo_id\": \"$C2\",
     \"loaded_at\": \"2026-10-01T10:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T11:00:00+08:00\"}
  ]
}"; echo

echo "29) 幂等：rd-2 同编号同内容重传 -> 200 replayed"
curl -sS -X POST "$BASE/vehicle-readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-2\", \"trip_id\": \"$V\",
  \"occurred_at\": \"2026-10-01T12:00:00+08:00\", \"temperature\": \"7.0\"
}"; echo
