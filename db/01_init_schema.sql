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

COMMIT;
