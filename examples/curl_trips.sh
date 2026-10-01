#!/usr/bin/env bash
# 冷链追责服务 —— 同一冷链车多票货物 + 车载测温 调用示例（curl）。
#
# 演示：
#   车次登记、多票货物装卸时段（左闭右开）、车载原始读数（允许晚到）、
#   各货物按各自温度上限判越温、同刻自有测温优先（原始读数仍可查）、
#   查询车次读数与各货物读数来源、车载读数开启区间的责任表态。
#
# 用法：BASE=http://127.0.0.1:8000 bash examples/curl_trips.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
T="TRUCK-DEMO-1"
C1="MULTI-CARGO-1"   # 上限 4℃
C2="MULTI-CARGO-2"   # 上限 7℃

echo "1) 登记两票货物（不同温度上限、不同起始保管方）"
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C1\",
  \"started_at\": \"2026-10-01T08:00:00+08:00\",
  \"temp_upper_limit\": \"4.0\",
  \"initial_holder\": \"北京冷库\"
}"; echo
curl -sS -X POST "$BASE/cargo" -H 'Content-Type: application/json' -d "{
  \"cargo_id\": \"$C2\",
  \"started_at\": \"2026-10-01T08:00:00+08:00\",
  \"temp_upper_limit\": \"7.0\",
  \"initial_holder\": \"天津冷库\"
}"; echo

echo "2) 登记车次（车号 + 备注）"
curl -sS -X POST "$BASE/trips" -H 'Content-Type: application/json' -d "{
  \"trip_id\": \"$T\",
  \"vehicle_no\": \"京A·F0001\",
  \"note\": \"10月1日 京沪冷链班次\"
}"; echo

echo "3) 批量登记两票货物的装卸时段（左闭右开；起点必须严格早于终点；整体校验/整体写入）"
echo "   C1 [10:00,18:00)；C2 [11:00,17:00)"
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"$C1\",
     \"loaded_at\": \"2026-10-01T10:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T18:00:00+08:00\"},
    {\"cargo_id\": \"$C2\",
     \"loaded_at\": \"2026-10-01T11:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T17:00:00+08:00\"}
  ]
}"; echo

echo "4) 车载读数 12:00 = 8.0℃（两票货都在车上；按各自上限：C1 越温，C2 也越温）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T12:00:00+08:00\", \"temperature\": \"8.0\"
}"; echo

echo "5) C1 在 13:00 有货物自有测温 9.0℃（自有事件；交接也只能由自有事件改变保管方）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"own-m-1\", \"cargo_id\": \"$C1\",
  \"occurred_at\": \"2026-10-01T13:00:00+08:00\", \"temperature\": \"9.0\"
}"; echo

echo "6) C1 在 14:00 交接 北京冷库 -> 济南冷库（仅自有交接改变保管方，区间在此拆分）"
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"own-h-1\", \"cargo_id\": \"$C1\",
  \"occurred_at\": \"2026-10-01T14:00:00+08:00\",
  \"giver\": \"北京冷库\", \"receiver\": \"济南冷库\"
}"; echo

echo "7) 车载读数 16:00 = 3.0℃（两票货仍在车上；闭合各自越温区间）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-2\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T16:00:00+08:00\", \"temperature\": \"3.0\"
}"; echo

echo "8) 车载读数 17:30 = 9.0℃（此时 C2 已 17:00 卸载；C1 18:00 才卸载）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-3\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T17:30:00+08:00\", \"temperature\": \"9.0\"
}"; echo

echo "9) 同刻自有优先演示：车载读数 11:30 = 8.0（两票货都在车上），再给 C2 补同刻自有测温 6.0（<=7 不越温）"
echo "   同一条 rd-0：对 C2 因同刻自有测温而不采用（自有值 6 不越温）；对 C1 无同刻自有测温，照常采用并开启其区间"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-0\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T11:30:00+08:00\", \"temperature\": \"8.0\"
}"; echo
curl -sS -X POST "$BASE/events" -H 'Content-Type: application/json' -d "{
  \"event_id\": \"own-m-0\", \"cargo_id\": \"$C2\",
  \"occurred_at\": \"2026-10-01T11:30:00+08:00\", \"temperature\": \"6.0\"
}"; echo

echo "10) 车载读数幂等重传（200 replayed，不重复写入）"
curl -sS -X POST "$BASE/trips/$T/readings" -H 'Content-Type: application/json' -d "{
  \"reading_id\": \"rd-1\", \"trip_id\": \"$T\",
  \"read_at\": \"2026-10-01T12:00:00+08:00\", \"temperature\": \"8.0\"
}"; echo

echo "11) 车次详情：装卸时段 + 全部车载原始读数（始终可查，含未被采用的）"
curl -sS "$BASE/trips/$T" | python3 -m json.tool; echo

echo "12) C1 的读数时间线（source=own/onboard，adopted 与未采用原因）"
curl -sS "$BASE/cargo/$C1/readings" | python3 -m json.tool; echo

echo "13) C1 追责报告（11:30 车载 onboard:rd-0 开启、14:00 交接拆分、16:00 车载闭合；17:30 车载再开开放区间）"
curl -sS "$BASE/cargo/$C1/report" | python3 -m json.tool; echo

echo "14) C2 追责报告（11:30 自有值优先不越温；12:00 车载开启、16:00 车载闭合；17:30 已卸载不受影响）"
curl -sS "$BASE/cargo/$C2/report" | python3 -m json.tool; echo

# 取 C1 首个已闭合（车载开启）区间的稳定标识与快照
read -r IID SNAP < <(python3 - "$BASE" "$C1" <<'PY'
import json, sys, urllib.request
base, cargo = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"{base}/cargo/{cargo}/responsibilities") as r:
    iv = json.load(r)["intervals"][0]
print(iv["interval_id"], iv["snapshot_id"])
PY
)
echo "15) C1 车载区间标识=$IID（onboard:<读数ID>），快照=$SNAP"

echo "16) 济南冷库对该车载区间确认担责（稳定标识可查询、可表态，规则与自有测温区间一致）"
curl -sS -X POST "$BASE/cargo/$C1/responsibilities/$IID/stances" \
  -H 'Content-Type: application/json' -d "{
    \"snapshot_id\": \"$SNAP\",
    \"responsible_party\": \"济南冷库\",
    \"stance\": \"confirm\",
    \"stance_id\": \"stance-jn-1\"
  }"; echo

echo "17) C1 区间责任确认视图（start_source/end_source=onboard，责任片段含交接拆分）"
curl -sS "$BASE/cargo/$C1/responsibilities/$IID" | python3 -m json.tool; echo

echo "18) 非法示例：起点不早于终点的装载时段 -> 422 invalid_loading_window（不留部分记录）"
curl -sS -X POST "$BASE/trips/$T/loadings" -H 'Content-Type: application/json' -d "{
  \"loadings\": [
    {\"cargo_id\": \"$C1\",
     \"loaded_at\": \"2026-10-01T19:00:00+08:00\",
     \"unloaded_at\": \"2026-10-01T18:30:00+08:00\"}
  ]
}"; echo
