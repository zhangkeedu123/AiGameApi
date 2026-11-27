-- 金缮夜馆应用库表结构。以应用账号在目标库执行：
--   psql -X -h 127.0.0.1 -U jinyao_app -d jinyao -v ON_ERROR_STOP=1 -f db/01_init_schema.sql
-- 全部语句幂等，重复执行保留已存在的数据。
\set ON_ERROR_STOP on
BEGIN;

-- 关卡：草稿与已发布共用一张表，发布时写入顺序号。
CREATE TABLE IF NOT EXISTS levels (
    id            uuid PRIMARY KEY,
    name          varchar(120) NOT NULL,
    storage_key   text         NOT NULL,
    config        jsonb        NOT NULL,
    status        varchar(16)  NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'published')),
    sort_order    integer,
    created_at    timestamptz  NOT NULL DEFAULT now(),
    published_at  timestamptz,
    CONSTRAINT levels_sort_order_key UNIQUE (sort_order),
    CONSTRAINT levels_publish_check CHECK (
        (status = 'draft' AND sort_order IS NULL AND published_at IS NULL)
        OR (status = 'published' AND sort_order IS NOT NULL AND published_at IS NOT NULL)
    )
);

COMMENT ON TABLE levels IS '小游戏关卡：草稿与已发布版本共用一行';
COMMENT ON COLUMN levels.config IS '关卡几何与文案配置 JSONB';
COMMENT ON COLUMN levels.storage_key IS '图集 PNG 在挂载目录中的文件名';
COMMENT ON COLUMN levels.sort_order IS '发布顺序，从 1 开始，草稿为空';

CREATE INDEX IF NOT EXISTS levels_status_order_idx ON levels (status, sort_order);

-- 玩家：登录统计与平台资料，来自微信或抖音。
CREATE TABLE IF NOT EXISTS users (
    id                uuid PRIMARY KEY,
    nickname          varchar(80),
    active            boolean      NOT NULL DEFAULT true,
    created_at        timestamptz  NOT NULL DEFAULT now(),
    source            varchar(16)  CHECK (source IN ('wechat', 'douyin')),
    openid            varchar(255),
    avatar_url        varchar(2048),
    login_count       integer      NOT NULL DEFAULT 0 CHECK (login_count >= 0),
    last_login_at     timestamptz,
    current_level_id  uuid REFERENCES levels (id) ON DELETE SET NULL,
    CONSTRAINT users_openid_source_check CHECK (openid IS NULL OR source IS NOT NULL)
);

COMMENT ON TABLE users IS '小游戏玩家，联调令牌确定身份';
COMMENT ON COLUMN users.source IS '平台来源：wechat 或 douyin';
COMMENT ON COLUMN users.login_count IS '累计成功登录次数，历史玩家从 0 开始';

CREATE UNIQUE INDEX IF NOT EXISTS users_source_openid_key
    ON users (source, openid) WHERE openid IS NOT NULL;

-- 联调令牌：只存摘要，撤销保留历史行。
CREATE TABLE IF NOT EXISTS user_tokens (
    id            uuid PRIMARY KEY,
    user_id       uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_digest  bytea       NOT NULL,
    issued_at     timestamptz NOT NULL DEFAULT now(),
    revoked_at    timestamptz,
    CONSTRAINT user_tokens_digest_key UNIQUE (token_digest)
);

COMMENT ON TABLE user_tokens IS '玩家联调令牌，仅保存 SHA-256 摘要';
COMMENT ON COLUMN user_tokens.revoked_at IS '为空表示当前有效；每名玩家至多一条有效令牌';

CREATE INDEX IF NOT EXISTS user_tokens_active_idx
    ON user_tokens (user_id) WHERE revoked_at IS NULL;

-- 玩家进度：按关卡保存完整快照，revision 做乐观锁。
CREATE TABLE IF NOT EXISTS user_level_progress (
    user_id            uuid        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    level_id           uuid        NOT NULL REFERENCES levels (id) ON DELETE CASCADE,
    snapshot           jsonb       NOT NULL,
    revision           bigint      NOT NULL DEFAULT 0 CHECK (revision >= 0),
    first_completed_at timestamptz,
    saved_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, level_id)
);

COMMENT ON TABLE user_level_progress IS '玩家每关一条存档快照';
COMMENT ON COLUMN user_level_progress.revision IS '乐观锁修订号，首存为 1';
COMMENT ON COLUMN user_level_progress.first_completed_at IS '首次通关时间，重开不清空';

-- 管理员：只用于后台会话登录。
CREATE TABLE IF NOT EXISTS admins (
    id             uuid PRIMARY KEY,
    username       varchar(120) NOT NULL UNIQUE,
    password_hash  text         NOT NULL,
    active         boolean      NOT NULL DEFAULT true,
    created_at     timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON TABLE admins IS '后台管理员，密码以 bcrypt 摘要保存';

-- 已有库重跑脚本时，旧关卡归入普通分类。
ALTER TABLE levels ADD COLUMN IF NOT EXISTS category varchar(16) NOT NULL DEFAULT 'normal';
ALTER TABLE levels DROP CONSTRAINT IF EXISTS levels_category_check;
ALTER TABLE levels ADD CONSTRAINT levels_category_check
    CHECK (category IN ('normal', 'hard', 'extreme', 'daily'));

CREATE TABLE IF NOT EXISTS daily_editions (
    day date PRIMARY KEY,
    level_id uuid NOT NULL REFERENCES levels (id)
);

CREATE TABLE IF NOT EXISTS daily_attempts (
    user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    day date NOT NULL REFERENCES daily_editions (day),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    elapsed_ms integer CHECK (elapsed_ms BETWEEN 0 AND 86400000),
    porcelain_name varchar(120),
    PRIMARY KEY (user_id, day),
    CHECK ((completed_at IS NULL AND elapsed_ms IS NULL AND porcelain_name IS NULL)
        OR (completed_at IS NOT NULL AND elapsed_ms IS NOT NULL AND porcelain_name IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS porcelain_redemptions (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    request_id varchar(100) NOT NULL,
    recipient varchar(60) NOT NULL,
    phone varchar(20) NOT NULL,
    address varchar(240) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'shipped', 'delivered')),
    carrier varchar(80),
    tracking_number varchar(100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, request_id),
    CHECK (status NOT IN ('shipped', 'delivered') OR (carrier IS NOT NULL AND tracking_number IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS porcelain_redemptions_status_created_idx
    ON porcelain_redemptions (status, created_at DESC);
CREATE INDEX IF NOT EXISTS porcelain_redemptions_created_idx
    ON porcelain_redemptions (created_at DESC);

ALTER TABLE levels ADD COLUMN IF NOT EXISTS level_number integer CHECK (level_number > 0);
ALTER TABLE levels ADD COLUMN IF NOT EXISTS atlas_version integer NOT NULL DEFAULT 1;
ALTER TABLE levels ADD COLUMN IF NOT EXISTS original_key text;

UPDATE levels SET level_number = sort_order WHERE level_number IS NULL AND status = 'published';
CREATE UNIQUE INDEX IF NOT EXISTS levels_category_number_key ON levels (category, level_number);

CREATE TABLE IF NOT EXISTS level_versions (
    level_id uuid NOT NULL REFERENCES levels (id) ON DELETE CASCADE,
    version integer NOT NULL,
    storage_key text NOT NULL,
    config jsonb NOT NULL,
    PRIMARY KEY (level_id, version)
);
ALTER TABLE daily_editions ADD COLUMN IF NOT EXISTS atlas_version integer NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS campaign_attempts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    level_id uuid NOT NULL REFERENCES levels (id),
    category varchar(16) NOT NULL,
    level_number integer NOT NULL,
    atlas_version integer NOT NULL,
    piece_count integer NOT NULL CHECK (piece_count > 0),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    elapsed_ms integer CHECK (elapsed_ms > 0),
    CHECK ((completed_at IS NULL) = (elapsed_ms IS NULL))
);
CREATE INDEX IF NOT EXISTS campaign_attempts_player_idx ON campaign_attempts (user_id, level_id)
    WHERE completed_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS campaign_attempts_completed_idx ON campaign_attempts (completed_at)
    WHERE completed_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS campaign_pending_attempt_key
    ON campaign_attempts (user_id, level_id, atlas_version) WHERE completed_at IS NULL;

CREATE TABLE IF NOT EXISTS monthly_porcelain_awards (
    month date PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users (id),
    category varchar(16) NOT NULL,
    level_number integer NOT NULL,
    elapsed_ms integer NOT NULL,
    awarded_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE porcelain_redemptions ADD COLUMN IF NOT EXISTS reward_month date
    REFERENCES monthly_porcelain_awards (month);
CREATE UNIQUE INDEX IF NOT EXISTS porcelain_redemptions_month_key
    ON porcelain_redemptions (reward_month) WHERE reward_month IS NOT NULL;

COMMIT;
