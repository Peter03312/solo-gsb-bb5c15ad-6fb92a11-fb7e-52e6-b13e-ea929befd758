-- 冷链追责服务 数据库初始化脚本
-- 由 PostgreSQL 官方镜像在数据卷首次创建时自动执行（/docker-entrypoint-initdb.d）。
-- 应用启动时也会执行 CREATE TABLE IF NOT EXISTS 作为兜底。

CREATE TYPE event_type AS ENUM ('measurement', 'handover');
CREATE TYPE stance_kind AS ENUM ('confirm', 'dispute');

CREATE TABLE IF NOT EXISTS cargo (
    cargo_id          VARCHAR(64)   PRIMARY KEY,
    started_at        TIMESTAMPTZ   NOT NULL,
    temp_upper_limit  NUMERIC(10,3) NOT NULL,
    initial_holder    VARCHAR(128)  NOT NULL,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS event (
    event_id     VARCHAR(64)   PRIMARY KEY,
    cargo_id     VARCHAR(64)   NOT NULL
                     REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    event_type   event_type    NOT NULL,
    occurred_at  TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3),
    giver        VARCHAR(128),
    receiver     VARCHAR(128),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 事件形状约束：测温只有温度；交接只有交出方/接收方且双方不同
    CONSTRAINT chk_event_shape CHECK (
        (event_type = 'measurement'
            AND temperature IS NOT NULL
            AND giver IS NULL
            AND receiver IS NULL)
        OR
        (event_type = 'handover'
            AND temperature IS NULL
            AND giver IS NOT NULL
            AND receiver IS NOT NULL
            AND giver <> receiver)
    )
);

CREATE INDEX IF NOT EXISTS ix_event_cargo_time
    ON event (cargo_id, occurred_at);

-- 同一货物同一时刻至多一笔测温
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_measurement
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'measurement';

-- 同一货物同一时刻至多一笔交接
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_handover
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'handover';

-- 责任确认表态（只追加：改口产生新编号，历次表态永久保留）
CREATE TABLE IF NOT EXISTS responsibility_stance (
    stance_id          VARCHAR(64)   PRIMARY KEY,
    cargo_id           VARCHAR(64)   NOT NULL
                         REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    -- 区间稳定标识：开启区间的自有测温事件ID，或 onboard:<车载读数ID>。
    -- 不对 event 建外键：开启测温也可能来自车载读数。长度容纳 onboard: 前缀。
    start_event_id     VARCHAR(128)  NOT NULL,
    responsible_party  VARCHAR(128)  NOT NULL,
    kind               stance_kind   NOT NULL,
    reason             VARCHAR(512),
    -- 提交时校验通过的快照标识（sha256:<hex>）；区间/片段变化后与当前快照不再一致
    snapshot_id        VARCHAR(71)   NOT NULL,
    -- 表态时该责任方承担的片段坐标 JSON（[["起","止","责任方"],...]），
    -- 晚到事件重建后逐片段判定表态是否仍有效
    attested_segments  VARCHAR(2000) NOT NULL,
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 表态形状：确认不得带理由；异议必须带非空理由
    CONSTRAINT chk_stance_shape CHECK (
        (kind = 'confirm' AND reason IS NULL)
        OR
        (kind = 'dispute' AND reason IS NOT NULL AND char_length(reason) > 0)
    )
);

CREATE INDEX IF NOT EXISTS ix_stance_cargo_interval
    ON responsibility_stance (cargo_id, start_event_id);

-- 冷链车次
CREATE TABLE IF NOT EXISTS trip (
    trip_id      VARCHAR(64)   PRIMARY KEY,
    vehicle_no   VARCHAR(64)   NOT NULL,
    note         VARCHAR(256),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 货物在某车次上的装载时段 [loaded_at, unloaded_at)（左闭右开）
CREATE TABLE IF NOT EXISTS cargo_loading (
    id           BIGSERIAL     PRIMARY KEY,
    trip_id      VARCHAR(64)   NOT NULL
                   REFERENCES trip(trip_id) ON DELETE RESTRICT,
    cargo_id     VARCHAR(64)   NOT NULL
                   REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    loaded_at    TIMESTAMPTZ   NOT NULL,
    unloaded_at  TIMESTAMPTZ   NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 时段必须左闭右开：起点严格早于终点
    CONSTRAINT chk_loading_window CHECK (loaded_at < unloaded_at),
    -- 同一车次同一货物至多一条装载时段
    CONSTRAINT ux_loading_trip_cargo UNIQUE (trip_id, cargo_id)
);

CREATE INDEX IF NOT EXISTS ix_loading_cargo_window
    ON cargo_loading (cargo_id, loaded_at, unloaded_at);

-- 车载测温设备在某车次上的原始读数（始终保留；是否被某货物采用在查询时合并判定）
CREATE TABLE IF NOT EXISTS onboard_reading (
    reading_id   VARCHAR(64)   PRIMARY KEY,
    trip_id      VARCHAR(64)   NOT NULL
                   REFERENCES trip(trip_id) ON DELETE RESTRICT,
    read_at      TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3) NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 同一车次同一时刻至多一笔车载原始读数
CREATE UNIQUE INDEX IF NOT EXISTS ux_onboard_trip_time
    ON onboard_reading (trip_id, read_at);

CREATE INDEX IF NOT EXISTS ix_onboard_trip
    ON onboard_reading (trip_id, read_at);
