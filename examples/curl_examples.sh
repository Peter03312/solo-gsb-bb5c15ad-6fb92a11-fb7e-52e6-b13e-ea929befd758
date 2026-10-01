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
