# 冷链货物追责服务

面向冷链货物追责的后端服务：录入货物与测温 / 交接事件，**按发生时间重建保管链**，自动识别越温区间并在交接时刻拆分责任。支持**同一冷链车承载多票货物**：登记车次、每票货物的装卸时段与车载测温记录，车载读数按装载时段归属到各货物、按各自温度上限判越温，同刻货物自有测温优先。技术栈 **Python 3.11 + FastAPI + SQLAlchemy 2 + PostgreSQL**。

## 一、一键启动（Docker Compose，含数据库初始化）

```bash
docker compose up --build
```

启动后：

| 组件 | 地址 |
| --- | --- |
| API 服务 | http://localhost:8000 |
| 交互式接口文档 Swagger UI | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | localhost:5432（用户/库/密码默认均为 `coldchain`） |

首次启动时，`db/init.sql` 由 PostgreSQL 官方镜像自动执行（建枚举类型、表、CHECK 约束、部分唯一索引）。应用启动时也会执行 `CREATE TABLE IF NOT EXISTS` 作为兜底。API 容器通过 `depends_on: service_healthy` 等待数据库就绪。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据库数据卷（下次重新初始化）
```

## 二、本地开发（不走 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 需要一个可连接的 PostgreSQL，并先执行 db/init.sql
export DATABASE_URL="postgresql+psycopg2://coldchain:coldchain@localhost:5432/coldchain"
uvicorn app.main:app --reload --port 8000
```

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg2://coldchain:coldchain@db:5432/coldchain` | SQLAlchemy 数据库连接串（容器内主机名为 `db`） |
| `POSTGRES_USER` | `coldchain` | 数据库用户（compose） |
| `POSTGRES_PASSWORD` | `coldchain` | 数据库密码（compose） |
| `POSTGRES_DB` | `coldchain` | 数据库名（compose） |
| `API_PORT` | `8000` | 宿主机映射的 API 端口 |
| `DB_PORT` | `5432` | 宿主机映射的数据库端口 |

也可在项目根目录放 `.env` 文件供 compose 读取。时间字段统一按 UTC 存储；请求时间建议带时区偏移（如 `+08:00`），无时区时间按 UTC 解释。

## 四、HTTP API

基址：`http://localhost:8000`。所有请求/响应均为 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/cargo` | 创建货物 |
| `GET` | `/cargo` | 货物列表 |
| `GET` | `/cargo/{cargo_id}` | 货物详情 |
| `POST` | `/events` | 写入货物**自有**测温 / 交接事件 |
| `GET` | `/cargo/{cargo_id}/events` | 货物自有事件列表（仅列自有事件，不含车载读数） |
| `GET` | `/cargo/{cargo_id}/report` | **追责报告**：合并车载读数后的越温区间 + 责任片段 |
| `GET` | `/cargo/{cargo_id}/readings` | **读数时间线**：各读数来源（自有 / 车载）与是否采用 |
| `GET` | `/cargo/{cargo_id}/responsibilities` | **责任确认**：全部已闭合区间的确认状态 |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}` | 单个已闭合区间的责任确认视图 |
| `POST` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 提交责任方表态（确认 / 异议） |
| `GET` | `/cargo/{cargo_id}/responsibilities/{interval_id}/stances` | 区间历次表态历史（含已改口 / 已失效） |
| `POST` | `/trips` | 登记车次 |
| `GET` | `/trips` | 车次列表 |
| `GET` | `/trips/{trip_id}` | 车次详情（装卸时段 + 全部车载原始读数） |
| `POST` | `/trips/{trip_id}/loadings` | 登记一车多票货物的装卸时段（整体校验 / 写入） |
| `POST` | `/trips/{trip_id}/readings` | 写入车载原始读数（允许晚到，幂等） |
| `GET` | `/trips/{trip_id}/readings` | 车次车载原始读数列表 |
| `GET` | `/health` | 健康检查 |

### 1. 创建货物 `POST /cargo`

```json
{
  "cargo_id": "COLD-001",
  "started_at": "2026-09-30T08:00:00+08:00",
  "temp_upper_limit": "4.0",
  "initial_holder": "北京冷库"
}
```

字段：货物编号、起始时间、温度上限（℃）、起始保管方。编号重复返回 `409 cargo_exists`。

### 2. 写入事件 `POST /events`

两种事件由请求字段区分（互斥，不能同时出现）：

测温（含 `temperature`）：

```json
{ "event_id": "evt-1", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T10:00:00+08:00", "temperature": "8.2" }
```

交接（含 `giver` 与 `receiver`）：

```json
{ "event_id": "evt-2", "cargo_id": "COLD-001",
  "occurred_at": "2026-09-30T12:00:00+08:00",
  "giver": "北京冷库", "receiver": "上海冷库" }
```

每个事件都带 **事件ID、货物编号、发生时间**。

### 3. 追责报告 `GET /cargo/{cargo_id}/report`

```json
{
  "cargo_id": "COLD-001",
  "temp_upper_limit": "4.000",
  "current_holder": "广州冷库",
  "excursions": [
    {
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "is_open": false,
      "segments": [
        { "start": "2026-09-30T02:00:00Z", "end": "2026-09-30T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "2026-09-30T04:00:00Z", "end": "2026-09-30T06:00:00Z", "responsible_party": "上海冷库" },
        { "start": "2026-09-30T06:00:00Z", "end": "2026-09-30T08:00:00Z", "responsible_party": "广州冷库" }
      ]
    }
  ]
}
```

未闭合区间：`end` 为 `null`、`is_open` 为 `true`，最后一个责任片段的 `end` 也是 `null`（未结束）。区间与片段均按时间升序返回。

### 4. 责任确认 `GET /cargo/{cargo_id}/responsibilities[/{interval_id}]`

围绕**已闭合**越温区间开展责任确认（开放区间不参与）。区间以**开启测温事件ID**作为区间标识（`interval_id` / `start_event_id`），响应同时给出结束测温事件ID、责任片段、**当前快照标识**、各责任方最新一条有效表态与汇总状态。

```json
{
  "cargo_id": "COLD-001",
  "intervals": [
    {
      "interval_id": "evt-temp-1",
      "start_event_id": "evt-temp-1",
      "end_event_id": "evt-temp-2",
      "start": "2026-09-30T02:00:00Z",
      "end": "2026-09-30T08:00:00Z",
      "snapshot_id": "sha256:836e6b3a…e797e4",
      "segments": [
        { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" },
        { "start": "…T04:00:00Z", "end": "…T08:00:00Z", "responsible_party": "广州冷库" }
      ],
      "latest_stances": [
        {
          "stance_id": "stance-bj-1",
          "responsible_party": "北京冷库",
          "stance": "confirm",
          "reason": null,
          "created_at": "2026-09-30T09:00:00Z",
          "segments": [ { "start": "…T02:00:00Z", "end": "…T04:00:00Z", "responsible_party": "北京冷库" } ]
        }
      ],
      "summary": "pending"
    }
  ]
}
```

* **快照标识 `snapshot_id`**：`sha256:` 前缀，对「区间起止 + 责任片段序列」的规范化 JSON 取 SHA-256；边界或责任片段一变，快照标识即变。
* **汇总状态 `summary`**：`all_confirmed` 全确认（当前片段中每个责任方最新有效表态均为确认）／`has_dispute` 有异议（最新有效表态中存在异议）／`pending` 待确认（尚有责任方未表态且无异议）。
* 列表接口只返回已闭合区间；直查单个区间时，区间不存在返回 `404 interval_not_found`，区间仍开放返回 `422 interval_open`。

### 5. 提交表态 `POST /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

请求体（货物编号与区间标识在路径上）：

```json
{
  "snapshot_id": "sha256:836e6b3a…e797e4",
  "responsible_party": "上海冷库",
  "stance": "dispute",
  "reason": "该时段温控由交出方远程设定，接收方未实际接管",
  "stance_id": "stance-sh-1"
}
```

* `stance`：`confirm` 确认担责；`dispute` 异议——**异议必须同时提供非空 `reason`**，确认不得带理由。
* `responsible_party` **必须出现在当前责任片段中**，否则 `422 party_not_in_segment`。
* `snapshot_id` 必须等于提交时的当前快照标识；晚到事件导致区间边界 / 责任片段变化后旧快照被拒：`409 stale_snapshot`。
* `stance_id` 为客户端指定的表态编号：
  - **同编号 + 同内容重传**：返回 `200 {"replayed": true, "stance": …原记录…}`（**即使该表态所依据的快照已失效也返回原记录**）；
  - **同编号 + 异内容**：`409 stance_id_conflict`，原记录不变；
  - **改口**（确认⇄异议）必须使用**新表态编号**，产生一条新记录；历次表态全部保留。
* 区间不存在 `404 interval_not_found`、区间仍开放 `422 interval_open`，同样拒绝且不落库。
* 表态提交与同货物的事件写入在事务中共用货物行锁（`SELECT ... FOR UPDATE`）**串行化**：校验用的快照一定来自持锁期间重建出的最新事件链。

### 6. 表态历史 `GET /cargo/{cargo_id}/responsibilities/{interval_id}/stances`

返回区间的历次表态（按提交时间升序），即使区间当前已不存在或重新开放也可查。每条记录附：

* `is_current`：对**当前**快照是否仍有效——晚到事件重建后，所在责任片段边界 / 责任方发生变化的旧表态为 `false`，片段未变化的继续有效；区间消失 / 重新开放时全部为 `false`；
* `superseded`：是否已被同责任方更新的有效表态改口（新编号表态）。

头部还给出 `interval_exists`、`is_open`、`current_snapshot_id`。

### 7. 车次、装卸时段与车载读数（同一冷链车多票货物）

#### 7.1 登记车次 `POST /trips`

```json
{ "trip_id": "TRIP-001", "vehicle_no": "京A·F0001", "note": "京沪班次（可选）" }
```

车次编号重复返回 `409 trip_exists`。`GET /trips` 列表、`GET /trips/{trip_id}` 详情（含该车次全部装卸时段与车载原始读数）。

#### 7.2 登记装卸时段 `POST /trips/{trip_id}/loadings`

一次登记该车次承载的**一票或多票货物**的装卸时段，**左闭右开** `[loaded_at, unloaded_at)`，且起点必须严格早于终点：

```json
{ "loadings": [
  { "cargo_id": "COLD-001",
    "loaded_at":   "2026-10-01T10:00:00+08:00",
    "unloaded_at": "2026-10-01T18:00:00+08:00" },
  { "cargo_id": "COLD-002",
    "loaded_at":   "2026-10-01T11:00:00+08:00",
    "unloaded_at": "2026-10-01T17:00:00+08:00" }
] }
```

* 车次或任一货物不存在、任一时段 `loaded_at >= unloaded_at`、批次为空 → 整体拒绝，**不留部分记录**；
* **同货物不得同时属于两个车次**：与该货物的既有装载时段（跨全部车次）相交即 `409 loading_overlap`；首尾相接允许（上一段 `unloaded_at` == 下一段 `loaded_at`）；
* 同一车次同一货物不能重复登记。

#### 7.3 写入车载读数 `POST /trips/{trip_id}/readings`

```json
{ "reading_id": "rd-1", "trip_id": "TRIP-001",
  "read_at": "2026-10-01T12:00:00+08:00", "temperature": "8.0" }
```

* 路径车次必须与请求体一致；车次不存在 `404 trip_not_found`。
* **允许晚到**：读数何时写入都可以，追责时间线在查询 / 表态时整体重建。
* **幂等**：`reading_id` 同内容（时间按同一时刻归一化）重传返回 `200 {"replayed": true}`；异内容 `409 reading_id_conflict`。
* 同车次同一时刻至多一笔读数：`409 same_timestamp_conflict`。
* 车载读数是**原始记录，始终可查**（`GET /trips/{trip_id}/readings` 与车次详情），与它是否被某货物采用无关。

#### 7.4 读数如何作用于货物

* 车载读数只作用于**读数时刻仍在车上**的货物：`loaded_at <= read_at < unloaded_at`（右开——卸车时刻的读数不再作用于该货物）。
* 每票货物按**各自温度上限**独立判越温；同一条读数可能同时被多票货物采用，结论可以不同。
* **同刻货物自有测温优先**：读数时刻该货物有自有测温事件时，追责时间线采用自有值、不采用车载值；车载原始读数仍可查，并在 `GET /cargo/{cargo_id}/readings` 中标注 `adopted=false`、`not_adopted_reason=own_measurement_priority`。其他未采用原因：`not_on_board`（读数时刻不在车上）、`before_cargo_start`（早于货物起始时间）。
* **保管方只由货物自有交接事件改变**：车载读数永不产生交接。

#### 7.5 查询车次读数与货物读数来源

`GET /cargo/{cargo_id}/readings` 返回该货物合并视角的全部测温候选（按时间）：

```json
{ "cargo_id": "COLD-001", "temp_upper_limit": "4.000", "readings": [
  { "occurred_at": "2026-10-01T04:00:00Z", "temperature": "8.000",
    "source": "onboard", "adopted": true,
    "event_id": null, "reading_id": "rd-1", "trip_id": "TRIP-001",
    "not_adopted_reason": null },
  { "occurred_at": "2026-10-01T06:00:00Z", "temperature": "5.000",
    "source": "own", "adopted": true,
    "event_id": "evt-9", "reading_id": null, "trip_id": null,
    "not_adopted_reason": null }
] }
```

追责报告 `GET /cargo/{cargo_id}/report` 的每个越温区间新增来源字段：`interval_id`（稳定标识）、`start_source` / `end_source`（`own` / `onboard`）、`start_event_id` / `end_event_id`（原始测温标识）、`start_trip_id` / `end_trip_id`。

#### 7.6 车载区间的稳定标识与责任表态

车载读数开启的越温区间，其**区间稳定标识**为 `onboard:<reading_id>`（自有测温开启的仍是测温事件ID），可直接用于责任确认查询与表态：

```bash
curl -X POST "$BASE/cargo/COLD-001/responsibilities/onboard:rd-1/stances" \
  -H 'Content-Type: application/json' -d '{
    "snapshot_id": "sha256:…", "responsible_party": "济南冷库",
    "stance": "confirm", "stance_id": "stance-jn-1" }'
```

车载区间与自有测温区间共用同一套规则：快照校验、确认 / 异议、汇总状态、表态幂等与改口、晚到数据后**逐片段有效性**与历史可查。晚到的自有测温顶替了车载开启点后，旧 `onboard:…` 区间消失，其旧表态全部失效（`is_current=false`）但历史仍可查。

## 五、业务规则

1. **时间合法性**：事件发生时间早于货物起始时间 → `422 before_start`，拒绝。
2. **越温区间**：
   - 测温 **高于** 温度上限（`temp > limit`，等于上限不算越温）开启区间；
   - 区间内首次 **不超温**（`temp <= limit`）的测温在其测温时刻闭合区间；
   - 区间内重复超限测温不另开区间；区间外的不超限测温无影响。
3. **责任拆分**：区间跨越交接时，按交接时刻切分——`[区间起, 交接时刻)` 归交出方，**交接时刻起归接收方**；不产生零长度片段。
4. **晚到记录**：事件允许乱序写入。每次写入都把候选事件并入既有事件，按发生时间整体重算受影响区间；因此交接、测温可事后补录。
5. **交接合法性（整体拒绝）**：任何一笔交接的交出方必须等于其发生时刻的保管方；晚到插入若导致此前已存在的交接不再满足该条件，本次写入**整体回滚拒绝，既有结果保持不变**（`422 giver_mismatch`）。
6. **同刻冲突**：同一货物同一时刻至多一笔测温、至多一笔交接（测温与交接可同刻发生，且测温先于交接处理），冲突 → `409 same_timestamp_conflict`。
7. **幂等**：同一 `event_id` 重传：内容完全一致（时间按同一时刻归一化比较）返回 `200 {"replayed": true}` 且不重复写入；内容不一致返回 `409 event_id_conflict`，原记录不变。
8. **并发**：同一货物的写入在事务中以 `SELECT ... FOR UPDATE` 锁定货物行串行化，并由部分唯一索引 / CHECK 约束在数据库层兜底。**责任表态提交与事件写入共用同一把货物行锁**，二者对同一货物严格串行。
9. **责任确认（仅已闭合区间）**：
   - 区间标识 = 开启区间的测温稳定标识（自有测温事件ID，或车载读数的 `onboard:<读数ID>`）；快照标识由区间起止与责任片段序列的 SHA-256 决定；
   - 表态为只追加记录：同编号同内容重传返回原记录（快照失效亦然），同编号异内容冲突，改口必须用新编号且历次表态保留；
   - 新表态以当前快照校验（过期拒绝），责任方必须在当前片段中；区间不存在 / 开放均拒绝且不落库；
   - 晚到事件重建后：边界或责任片段变化的旧表态失效、未变化片段上的表态继续有效，全部历史仍可查。
10. **车次 / 装载 / 车载读数**：
    - 装卸时段左闭右开且起点严格早于终点；车次或货物不存在、时段无效 → 整批拒绝不留部分记录；
    - 同货物的装载时段两两不得相交（首尾相接允许），即同一货物不得同时属于两个车次；同车次同货物唯一；
    - 车载读数仅作用于读数时刻仍在车上（`loaded_at <= read_at < unloaded_at`）的货物，按各货物自身温度上限判越温；读数时刻早于货物起始时间不采用；
    - 同刻有货物自有测温时采用自有值，车载读数不并入但原始记录仍可查；交接只由货物自有事件改变保管方；
    - 车载读数允许晚到：同编号同内容重传幂等、异内容 `reading_id_conflict`，同车次同刻重复 `same_timestamp_conflict`；
    - 车载读数写入先锁车次行、再按固定顺序锁受影响货物行；与受影响货物的事件写入、装载时段写入、责任表态**串行一致**。

错误响应统一格式：`{"error": {"code": "...", "message": "..."}}`。

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cargo_not_found` / `interval_not_found` / `trip_not_found` | 货物、已闭合区间或车次不存在 |
| 409 | `cargo_exists` / `trip_exists` / `event_id_conflict` / `reading_id_conflict` / `same_timestamp_conflict` / `loading_overlap` / `integrity_conflict` / `stance_id_conflict` / `stale_snapshot` | 编号或同刻冲突、装载时段相交、表态编号冲突、快照过期 |
| 422 | `before_start` / `giver_mismatch` / `bad_event_shape` / `interval_open` / `party_not_in_segment` / `bad_stance_shape` / `dispute_reason_required` / `invalid_loading_window` / `bad_reading_shape` | 业务校验失败（整体拒绝、不落库） |
| 200 | 响应体 `replayed=true` | 事件 / 表态 / 车载读数同 ID 同内容幂等重传 |

## 六、调用示例

```bash
# curl 脚本 1：货物自有测温 / 交接、追责报告、责任确认完整链路
bash examples/curl_examples.sh

# curl 脚本 2：同一冷链车多票货物、装卸时段、车载读数、读数来源、车载区间表态
bash examples/curl_trips.sh

# Python 示例（httpx）
python examples/python_client.py
```

或直接打开 http://localhost:8000/docs 使用 Swagger UI 调用。

## 七、测试

```bash
pip install pytest httpx

pytest -q                                          # 79 个用例（内存库），并发用例自动跳过
DATABASE_URL="sqlite:////tmp/cc.db" \
  pytest tests/test_concurrency.py -q              # 3 个并发串行化用例（交接 / 表态 / 车载读数，需文件型库）
```

测试覆盖：越温开闭、跨交接责任拆分、同刻测温+交接、未闭合区间、晚到重算、交出方整体拒绝、同刻冲突、幂等重传、并发写入串行化，以及责任确认的快照校验、确认/异议、汇总状态、表态幂等与改口、晚到事件后表态逐片段失效与历史可查；**车次 / 多票装载**覆盖装卸时段左闭右开与整体拒绝、同货物不得同时属于两个车次（相交拒绝、首尾相接允许）、车载读数按装载时段归属与各货物独立上限、右开卸载边界、同刻自有优先且原始读数可查、读数晚到与装载晚到重算、车载区间 `onboard:` 稳定标识的查询与表态及晚到自有测温顶替后旧表态失效。

## 八、目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由与异常映射
│   ├── crud.py            # 货物自有事件写入事务、幂等、整链校验
│   ├── trip_crud.py       # 车次 / 装载时段 / 车载读数写入事务（行锁串行化）
│   ├── stance_crud.py     # 责任表态写入事务（与事件/车载写入共用货物行锁）
│   ├── chain.py           # 保管链重建 / 越温责任拆分（纯函数，支持自有+车载测温点）
│   ├── timeline.py        # 按装载时段合并自有事件与车载读数（来源归属 / 自有优先）
│   ├── responsibility.py  # 快照标识、表态有效性、确认汇总（纯逻辑）
│   ├── models.py          # SQLAlchemy 模型（货物 / 事件 / 车次 / 装载 / 车载读数 / 表态）
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── database.py        # 引擎、Session（含 SQLite 兼容）
│   └── timeutil.py        # UTC 归一化
├── db/init.sql            # PostgreSQL 初始化（枚举、表、约束、索引）
├── examples/
│   ├── curl_examples.sh   # 货物自有测温 / 交接 / 责任确认链路
│   ├── curl_trips.sh      # 一车多票 / 装卸时段 / 车载读数 / 读数来源 / 车载区间表态
│   └── python_client.py   # Python（httpx）调用示例
├── tests/
│   ├── test_trips.py            # 车次 / 装载 / 车载读数 / 车载区间表态
│   ├── test_api.py              # 自有事件 API、校验、幂等、晚到重算
│   ├── test_chain.py            # 保管链 / 责任拆分纯函数
│   ├── test_responsibility.py   # 责任确认 API
│   ├── test_responsibility_unit.py
│   └── test_concurrency.py      # 并发串行化（交接 / 表态 / 车载读数）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
